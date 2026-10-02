"""Job na Gringa: curated international remote roles for BR devs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .base import JobPosting, RateLimitConfig, SearchQuery


@dataclass
class JobNaGringaSource:
    name: str = "job_na_gringa"
    base_url: str = "https://jobnagringa.com.br"
    rate_limit: RateLimitConfig | None = None

    def __post_init__(self) -> None:
        if self.rate_limit is None:
            self.rate_limit = RateLimitConfig("jobnagringa.com.br", 1.0, 3.0)

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        reraise=True,
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    async def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]:
        # General listing page; profile roles filter titles below.
        url = f"{self.base_url}/jobs"
        html = await self._fetch(client, url)
        for posting in parse_listing(html, self.base_url):
            if not query.matches_role(posting.title):
                continue
            yield posting

    async def fetch_detail(self, posting: JobPosting, client: httpx.AsyncClient) -> JobPosting:
        if posting.description:
            return posting
        try:
            html = await self._fetch(client, posting.url)
        except httpx.HTTPError:
            return posting
        tree = HTMLParser(html)
        body = tree.css_first("article, main, .job-detail")
        if body is not None:
            posting.description = body.text(separator="\n").strip()
        return posting


def parse_listing(html: str, base_url: str) -> list[JobPosting]:
    """Parse Job na Gringa listing HTML into postings. Hand-tolerant of selector drift."""
    tree = HTMLParser(html)
    out: list[JobPosting] = []
    from selectolax.parser import Node

    seen: set[int] = set()
    cards: list[Node] = []
    for sel in ("article.job-card", "[data-job-card]"):
        for node in tree.css(sel):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            cards.append(node)
    for card in cards:
        title_el = card.css_first(".job-card__title, h2, h3")
        company_el = card.css_first(".job-card__company, .company")
        link_el = card.css_first(".job-card__apply-link[href], a[href]")
        if not (title_el and company_el and link_el):
            continue
        href = link_el.attributes.get("href") or ""
        url = href if href.startswith("http") else f"{base_url.rstrip('/')}/{href.lstrip('/')}"
        external_id = href.rsplit("/", 1)[-1] or f"jng-{title_el.text(strip=True)}"
        salary_el = card.css_first(".job-card__salary")
        currency, smin, smax = _parse_salary(salary_el.text(strip=True) if salary_el else "")
        out.append(
            JobPosting(
                source="job_na_gringa",
                external_id=external_id,
                url=url,
                title=title_el.text(strip=True),
                company=company_el.text(strip=True),
                salary_min=smin,
                salary_max=smax,
                currency=currency,
                remote=True,
                raw_payload={"href": href},
            )
        )
    return out


def _parse_salary(text: str) -> tuple[str | None, int | None, int | None]:
    """Best-effort parse: 'USD 80k - 120k', '$80,000-$120,000', etc."""
    import re

    if not text:
        return None, None, None
    cur_match = re.search(r"\$|USD|EUR|GBP|BRL|R\$", text, re.IGNORECASE)
    currency: str | None = None
    if cur_match:
        token = cur_match.group(0).upper()
        currency = {"$": "USD", "R$": "BRL"}.get(token, token)
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*(k|K)?", text)
    if not nums:
        return currency, None, None
    parsed: list[int] = []
    for raw, k in nums[:2]:
        v = float(raw) * 1000 if k else float(raw)
        parsed.append(int(v))
    if len(parsed) == 1:
        return currency, parsed[0], None
    return currency, parsed[0], parsed[1]


SOURCE: JobNaGringaSource = JobNaGringaSource()
