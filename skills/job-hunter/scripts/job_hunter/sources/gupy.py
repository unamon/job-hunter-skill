"""Gupy: search every company through the public job portal.

`portal.gupy.io` exposes the JSON search its own UI uses; we query it once per
profile role. `gupy_companies.yaml` in the user's config still adds a per-company
HTML scrape for career pages that don't publish to the portal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..paths import resolve
from .base import JobPosting, RateLimitConfig, SearchQuery

PORTAL_SEARCH = "https://portal.gupy.io/api/job-search/jobs"
PORTAL_PAGE_SIZE = 100


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

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        reraise=True,
    )
    async def _search(self, client: httpx.AsyncClient, term: str, offset: int) -> dict[str, Any]:
        resp = await client.get(
            PORTAL_SEARCH,
            params={"jobName": term, "limit": PORTAL_PAGE_SIZE, "offset": offset},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def _companies(self) -> list[str]:
        """Extra subdomains to scrape directly; empty unless the user lists some."""
        cfg = resolve().config_dir / "gupy_companies.yaml"
        if not cfg.exists():
            return []
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return []
        companies = data.get("companies") if isinstance(data, dict) else None
        return [str(c) for c in companies] if isinstance(companies, list) else []

    async def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]:
        seen: set[str] = set()
        for term in query.roles or [""]:
            for page in range(query.max_pages):
                data = await self._search(client, term, page * PORTAL_PAGE_SIZE)
                rows = data.get("data") or []
                for row in rows:
                    posting = posting_from_portal(row)
                    if posting is None or posting.external_id in seen:
                        continue
                    seen.add(posting.external_id)
                    # The portal search is fuzzy; keep only real title matches.
                    if query.matches_role(posting.title):
                        yield posting
                total = (data.get("pagination") or {}).get("total", 0)
                if len(rows) < PORTAL_PAGE_SIZE or (page + 1) * PORTAL_PAGE_SIZE >= total:
                    break
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


def posting_from_portal(row: dict[str, Any]) -> JobPosting | None:
    """Map one portal search result. Uses the canonical `<co>.gupy.io/jobs/<id>`
    URL so the bundled gupy apply adapter matches it."""
    job_id = row.get("id")
    title = (row.get("name") or "").strip()
    # careerPageUrl is usually empty in practice; jobUrl always carries the host.
    host = urlparse(row.get("jobUrl") or row.get("careerPageUrl") or "").netloc
    if not (job_id and host and title):
        return None
    career = f"https://{host}"
    workplace = (row.get("workplaceType") or "").lower()
    place = ", ".join(p for p in (row.get("city"), row.get("state")) if p)
    posted_at = None
    if row.get("publishedDate"):
        try:
            posted_at = datetime.fromisoformat(str(row["publishedDate"]).replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
    return JobPosting(
        source="gupy",
        external_id=str(job_id),
        url=f"{career}/jobs/{job_id}",
        title=title,
        company=(row.get("careerPageName") or "").strip() or host.split(".")[0],
        location=place or ("Remote" if workplace == "remote" else None),
        remote=workplace == "remote" if workplace else None,
        posted_at=posted_at,
        description=row.get("description") or None,
        raw_payload={k: row.get(k) for k in ("id", "jobUrl", "workplaceType", "type")},
        tags=[workplace] if workplace else [],
    )


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
