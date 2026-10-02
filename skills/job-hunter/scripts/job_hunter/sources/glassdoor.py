"""Glassdoor: cookie-authenticated, fragile HTML scrape.

Glassdoor actively blocks scraping (Cloudflare, JS rendering, login wall).
This source ships a best-effort HTML path that requires capturing the
`gdId` + `_uac` session cookies after a real browser login. Expect it to
fail more often than the other sources; the failure modes surface clearly
via `SourceError`.

For salary aggregation, prefer `job-hunter salary` which uses the
salary_min/salary_max already in the DB from Indeed + RemoteOK + Job na
Gringa listings — much more reliable than scraping Glassdoor's salary tool.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser, Node
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .base import JobPosting, RateLimitConfig, SearchQuery, SourceError
from .indeed import _is_remote, _looks_like_captcha, _parse_salary


@dataclass
class GlassdoorSource:
    name: str = "glassdoor"
    base_url: str = "https://www.glassdoor.com"
    rate_limit: RateLimitConfig | None = None

    def __post_init__(self) -> None:
        if self.rate_limit is None:
            # Conservative — Glassdoor is touchy.
            self.rate_limit = RateLimitConfig("glassdoor.com", 15.0, 30.0)

    def _cookies(self) -> dict[str, str]:
        gd_id = os.environ.get("GLASSDOOR_GD_ID")
        uac = os.environ.get("GLASSDOOR_UAC")
        if not (gd_id and uac):
            raise SourceError(
                "Glassdoor requires GLASSDOOR_GD_ID and GLASSDOOR_UAC in "
                "~/.config/job-hunter/secrets/personal.env. Capture them after "
                "logging in at glassdoor.com — see references/sources/glassdoor.md."
            )
        return {"gdId": gd_id, "_uac": uac}

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=3, max=30),
        reraise=True,
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> str:
        # Route through Firecrawl when FIRECRAWL_ENDPOINT is set. Glassdoor is
        # exactly the captcha-heavy case Firecrawl is for. Cookies are NOT sent
        # via Firecrawl (it has its own session) — users must rely on Firecrawl's
        # anti-bot bypass rather than authenticated cookies in that path.
        from ..firecrawl_client import FirecrawlClient, FirecrawlError

        fc = FirecrawlClient.from_env()
        if fc is not None:
            try:
                return await fc.scrape_html(url, client=client)
            except FirecrawlError as e:
                raise SourceError(f"Firecrawl error fetching {url}: {e}") from e

        cookies = self._cookies()
        resp = await client.get(url, cookies=cookies)
        if resp.status_code in (403, 429):
            raise SourceError(
                f"Glassdoor returned {resp.status_code}. Likely Cloudflare or "
                "expired cookies. Refresh GLASSDOOR_GD_ID + GLASSDOOR_UAC."
            )
        location_header = (resp.headers.get("location") or "").lower()
        if resp.status_code in (301, 302) and "login" in location_header:
            raise SourceError(
                "Glassdoor redirected to login. Refresh GLASSDOOR_GD_ID + GLASSDOOR_UAC."
            )
        resp.raise_for_status()
        body = resp.text
        if _looks_like_captcha(body):
            raise SourceError(
                "Glassdoor served a captcha. Use a browser session for a few minutes, "
                "then refresh cookies."
            )
        if "Sign In" in body[:5000] and "salary_main_card" not in body[:50000]:
            raise SourceError("Glassdoor returned the login wall. Cookies likely expired.")
        return body

    async def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]:
        roles = query.roles or ["Software Engineer"]
        for role in roles:
            for location in query.locations or ["Brazil"]:
                url = (
                    f"{self.base_url}/Job/jobs.htm?"
                    f"sc.keyword={_qs(role)}&locT=C&locName={_qs(location)}"
                )
                try:
                    html = await self._fetch(client, url)
                except SourceError:
                    raise
                except httpx.HTTPError:
                    continue
                for posting in parse_listing(html, self.base_url):
                    if not query.matches_role(posting.title):
                        continue
                    yield posting

    async def fetch_detail(self, posting: JobPosting, client: httpx.AsyncClient) -> JobPosting:
        return posting

    async def fetch_salary_estimate(
        self,
        client: httpx.AsyncClient,
        *,
        role: str,
        location: str,
    ) -> SalaryEstimate | None:
        """Pull salary tool data for a role/location. Best-effort.

        Returns None on any failure rather than raising — callers can fall back
        to the DB-aggregation path (`job-hunter salary` without --source).
        """
        url = (
            f"{self.base_url}/Salaries/{_slug(location)}-{_slug(role)}-salary-SRCH"
            f"_IL.0,{len(location)}_KO{len(location) + 1},{len(location) + len(role) + 1}.htm"
        )
        try:
            html = await self._fetch(client, url)
        except (SourceError, httpx.HTTPError):
            return None
        return parse_salary_page(html, role=role, location=location)


@dataclass(frozen=True)
class SalaryEstimate:
    role: str
    location: str
    currency: str | None
    p25: int | None
    median: int | None
    p75: int | None
    sample_size: int | None
    source_url: str


def parse_listing(html: str, base_url: str) -> list[JobPosting]:
    tree = HTMLParser(html)
    seen: set[int] = set()
    cards: list[Node] = []
    for sel in ("li[data-test='jobListing']", "li.react-job-listing", ".JobsList_jobListItem__"):
        for node in tree.css(sel):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            cards.append(node)

    postings: list[JobPosting] = []
    for card in cards:
        link = card.css_first("a[data-test='job-link'], a[href*='/job-listing/']")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        url = href if href.startswith("http") else f"{base_url}{href}"
        external_id = _id_from_href(href) or href
        title_el = card.css_first("[data-test='job-title'], .jobLink")
        company_el = card.css_first("[data-test='employer-name'], .employerName")
        location_el = card.css_first("[data-test='job-location'], .loc")
        salary_el = card.css_first("[data-test='detailSalary'], .salaryEstimate")
        title = title_el.text(strip=True) if title_el else "Unknown"
        company = company_el.text(strip=True) if company_el else "Unknown"
        location = location_el.text(strip=True) if location_el else None
        currency, smin, smax = _parse_salary(salary_el.text(strip=True) if salary_el else "")
        postings.append(
            JobPosting(
                source="glassdoor",
                external_id=external_id,
                url=url.split("?")[0],
                title=title,
                company=company,
                location=location,
                remote=_is_remote(location),
                salary_min=smin,
                salary_max=smax,
                currency=currency,
                raw_payload={"href": href},
            )
        )
    return postings


def parse_salary_page(html: str, *, role: str, location: str) -> SalaryEstimate | None:
    tree = HTMLParser(html)
    p25 = _read_money(tree.css_first("[data-test='p25-salary'], [data-test='salary-p25']"))
    median = _read_money(
        tree.css_first("[data-test='median-salary'], [data-test='base-pay-median']")
    )
    p75 = _read_money(tree.css_first("[data-test='p75-salary'], [data-test='salary-p75']"))
    sample_el = tree.css_first("[data-test='sample-size']")
    sample = None
    if sample_el is not None:
        m = re.search(r"(\d[\d.,]*)", sample_el.text())
        if m:
            try:
                sample = int(m.group(1).replace(",", "").replace(".", ""))
            except ValueError:
                sample = None
    currency: str | None = None
    if median or p25 or p75:
        currency = "USD"  # Glassdoor's salary pages localize via subdomain
    return SalaryEstimate(
        role=role,
        location=location,
        currency=currency,
        p25=p25,
        median=median,
        p75=p75,
        sample_size=sample,
        source_url="(glassdoor salary page)",
    )


def _read_money(node: Node | None) -> int | None:
    if node is None:
        return None
    _, lo, _ = _parse_salary(node.text(strip=True))
    return lo


def _qs(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _slug(value: str) -> str:
    return value.lower().replace(" ", "-")


def _id_from_href(href: str) -> str:
    m = re.search(r"jobListingId=(\d+)", href)
    return m.group(1) if m else ""


SOURCE: GlassdoorSource = GlassdoorSource()
