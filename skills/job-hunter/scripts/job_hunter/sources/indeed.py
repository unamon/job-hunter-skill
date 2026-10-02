"""Indeed: HTML scraping with selectolax.

Caveats:
- Indeed serves Cloudflare/captcha challenges aggressively for non-browser
  traffic. We detect them and surface SourceError with a clear message
  rather than parsing empty/broken pages.
- BR and US endpoints differ slightly. Defaults to `br.indeed.com`.
- No auth required for listing pages (full descriptions sometimes need
  a session, but the listing has enough for discovery).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser, Node
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .base import JobPosting, RateLimitConfig, SearchQuery, SourceError

DEFAULT_HOST = "br.indeed.com"
SALARY_RE = re.compile(
    r"(R\$|US\$|\$|USD|BRL|EUR|GBP)\s*([\d.,]+)\s*[-–—a–to]+\s*(R\$|US\$|\$|USD|BRL|EUR|GBP)?\s*([\d.,]+)?",
    re.IGNORECASE,
)
CAPTCHA_HINTS = (
    "captcha",
    "cf-browser-verification",
    "verify you are a human",
    "checking your browser",
    "we just need to make sure",
)


@dataclass
class IndeedSource:
    name: str = "indeed"
    base_url: str = f"https://{DEFAULT_HOST}"
    rate_limit: RateLimitConfig | None = None

    def __post_init__(self) -> None:
        if self.rate_limit is None:
            # Polite. Indeed will still occasionally captcha us.
            self.rate_limit = RateLimitConfig("indeed.com", 6.0, 12.0)

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=2, max=20),
        reraise=True,
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> str:
        # Route through Firecrawl when FIRECRAWL_ENDPOINT is set (see
        # references/firecrawl.md). Discovery only — never apply.
        from ..firecrawl_client import FirecrawlClient, FirecrawlError

        fc = FirecrawlClient.from_env()
        if fc is not None:
            try:
                return await fc.scrape_html(url, client=client)
            except FirecrawlError as e:
                raise SourceError(f"Firecrawl error fetching {url}: {e}") from e

        resp = await client.get(url)
        if resp.status_code in (403, 429):
            raise SourceError(
                f"Indeed returned {resp.status_code}. "
                "Likely Cloudflare / rate limit. Pause runs for an hour, then retry. "
                "Consider enabling Firecrawl — see references/firecrawl.md."
            )
        resp.raise_for_status()
        body = resp.text
        if _looks_like_captcha(body):
            raise SourceError(
                "Indeed served a captcha challenge. Open https://br.indeed.com in "
                "your browser, solve the challenge, then retry. Or enable Firecrawl "
                "for JS-rendering + anti-bot bypass — see references/firecrawl.md."
            )
        return body

    async def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]:
        roles = query.roles or ["Software Engineer"]
        for role in roles:
            for location in query.locations or ["Brasil"]:
                url = f"{self.base_url}/jobs?q={_qs(role)}" f"&l={_qs(location)}&fromage=7"
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
        if posting.description:
            return posting
        try:
            html = await self._fetch(client, posting.url)
        except (SourceError, httpx.HTTPError):
            return posting
        tree = HTMLParser(html)
        body = tree.css_first("#jobDescriptionText, [data-testid='job-description']")
        if body is not None:
            posting.description = body.text(separator="\n").strip()
        return posting


def _qs(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _looks_like_captcha(body: str) -> bool:
    lowered = body[:5000].lower()
    return any(hint in lowered for hint in CAPTCHA_HINTS)


def parse_listing(html: str, base_url: str) -> list[JobPosting]:
    tree = HTMLParser(html)
    seen: set[int] = set()
    cards: list[Node] = []
    for sel in ("div[data-jk]", "td.resultContent", "a.tapItem"):
        for node in tree.css(sel):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            cards.append(node)

    postings: list[JobPosting] = []
    for card in cards:
        external_id = card.attributes.get("data-jk") or ""
        if not external_id:
            link = card.css_first("a[data-jk], a[href*='clk?jk=']")
            if link is not None:
                external_id = link.attributes.get("data-jk") or _jk_from_href(
                    link.attributes.get("href") or ""
                )
        if not external_id:
            continue

        title_el = card.css_first("h2.jobTitle span[title], h2 a span[title], .jobTitle a span")
        if title_el is None:
            title_el = card.css_first("h2 a, h2 span")
        title = (title_el.attributes.get("title") or title_el.text(strip=True)) if title_el else ""

        company_el = card.css_first("[data-testid='company-name'], span.companyName, .companyName")
        company = company_el.text(strip=True) if company_el else "Unknown"

        location_el = card.css_first(
            "[data-testid='text-location'], div.companyLocation, .companyLocation"
        )
        location = location_el.text(strip=True) if location_el else None

        salary_el = card.css_first(
            "[data-testid='attribute_snippet_testid'], .salary-snippet, .estimated-salary"
        )
        currency, smin, smax = _parse_salary(salary_el.text(strip=True) if salary_el else "")

        url = f"{base_url.rstrip('/')}/viewjob?jk={external_id}"
        postings.append(
            JobPosting(
                source="indeed",
                external_id=external_id,
                url=url,
                title=title or "Unknown",
                company=company,
                location=location,
                remote=_is_remote(location),
                salary_min=smin,
                salary_max=smax,
                currency=currency,
                raw_payload={"data_jk": external_id},
            )
        )
    return postings


def _is_remote(location: str | None) -> bool:
    if not location:
        return False
    lowered = location.lower()
    return any(tok in lowered for tok in ("remote", "remoto", "worldwide", "anywhere"))


def _jk_from_href(href: str) -> str:
    m = re.search(r"jk=([0-9a-fA-F]+)", href)
    return m.group(1) if m else ""


def _parse_salary(text: str) -> tuple[str | None, int | None, int | None]:
    if not text:
        return None, None, None
    cleaned = text.replace(" ", " ").replace(" ", " ")
    cur: str | None = None
    if re.search(r"R\$|BRL|reais?", cleaned, re.IGNORECASE):
        cur = "BRL"
    elif re.search(r"USD|US\$", cleaned, re.IGNORECASE) or "$" in cleaned:
        cur = "USD"
    elif re.search(r"EUR|€", cleaned):
        cur = "EUR"

    # Strip currency markers so they don't pollute number extraction.
    stripped = re.sub(
        r"R\$|US\$|\$|USD|BRL|EUR|GBP|€|reais?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Pull numeric values: 12.500, 12,500, 12000, 120k
    nums = re.findall(r"([\d.,]+)\s*(k|K|mil)?", stripped)
    parsed: list[int] = []
    for raw, suffix in nums[:2]:
        normalized = raw.strip(".,")
        if not re.search(r"\d", normalized):
            continue
        # Thousands grouping detector: a separator followed by exactly 3 digits
        # (BR `12.000` or US `12,000`) → strip ALL separators. Otherwise treat
        # remaining `,` as decimal-comma (BR `12,5` style).
        if re.search(r"[.,]\d{3}(\D|$)", normalized):
            normalized = normalized.replace(".", "").replace(",", "")
        else:
            normalized = normalized.replace(",", ".")
        try:
            value = float(normalized)
        except ValueError:
            continue
        if suffix and suffix.lower() in {"k", "mil"}:
            value *= 1000
        # Filter out implausibly tiny matches (e.g. matched a stray digit).
        if value < 100:
            continue
        parsed.append(int(value))
    if not parsed:
        return cur, None, None
    if len(parsed) == 1:
        return cur, parsed[0], None
    return cur, parsed[0], parsed[1]


SOURCE: IndeedSource = IndeedSource()
