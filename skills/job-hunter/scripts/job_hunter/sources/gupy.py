"""Gupy: iterate a configured list of company subdomains.

`gupy_companies.yaml` in the user's XDG config lists target subdomains.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import yaml
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..paths import resolve
from .base import JobPosting, RateLimitConfig, SearchQuery

DEFAULT_COMPANIES = [
    "nubank",
    "stark",
    "inter",
    "itau",
    "bradesco",
    "rappi",
    "ifood",
]


@dataclass
class GupySource:
    name: str = "gupy"
    base_url: str = "https://gupy.io"
    rate_limit: RateLimitConfig | None = None

    def __post_init__(self) -> None:
        if self.rate_limit is None:
            self.rate_limit = RateLimitConfig("gupy.io", 2.0, 4.0)

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

    def _companies(self) -> list[str]:
        paths = resolve()
        cfg = paths.config_dir / "gupy_companies.yaml"
        if not cfg.exists():
            return list(DEFAULT_COMPANIES)
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return list(DEFAULT_COMPANIES)
        companies = data.get("companies") if isinstance(data, dict) else None
        if isinstance(companies, list) and companies:
            return [str(c) for c in companies]
        return list(DEFAULT_COMPANIES)

    async def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]:
        for company in self._companies():
            url = f"https://{company}.gupy.io/jobs"
            try:
                html = await self._fetch(client, url)
            except httpx.HTTPError:
                continue
            for posting in parse_listing(html, company):
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
        body = tree.css_first("[data-testid='job-description'], main, article")
        if body is not None:
            posting.description = body.text(separator="\n").strip()
        return posting


def parse_listing(html: str, company_subdomain: str) -> list[JobPosting]:
    tree = HTMLParser(html)
    out: list[JobPosting] = []
    from selectolax.parser import Node

    seen: set[int] = set()
    cards: list[Node] = []
    for sel in ("[data-testid='job-card']", "article.job-card"):
        for node in tree.css(sel):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            cards.append(node)
    for card in cards:
        title_el = card.css_first("h3, h2, [data-testid='job-card-title']")
        link_el = card.css_first("a[href]")
        if not (title_el and link_el):
            continue
        href = link_el.attributes.get("href") or ""
        url = href if href.startswith("http") else f"https://{company_subdomain}.gupy.io{href}"
        external_id = (
            href.rstrip("/").rsplit("/", 1)[-1]
            or f"gupy-{company_subdomain}-{title_el.text(strip=True)}"
        )
        loc_el = card.css_first("[data-testid='job-card-location'], .location")
        location = loc_el.text(strip=True) if loc_el else None
        out.append(
            JobPosting(
                source="gupy",
                external_id=external_id,
                url=url,
                title=title_el.text(strip=True),
                company=company_subdomain.capitalize(),
                location=location,
                raw_payload={"company_subdomain": company_subdomain, "href": href},
            )
        )
    return out


SOURCE: GupySource = GupySource()
