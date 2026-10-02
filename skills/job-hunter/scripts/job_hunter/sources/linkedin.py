"""LinkedIn: authenticated via the `li_at` session cookie.

The cookie is read from `os.environ["LINKEDIN_LI_AT"]` after the user's
`personal.env` has been loaded into the process. NEVER LOGGED.

LinkedIn's job-search UI is a SPA; cards are filled in by JavaScript after
the initial HTML loads. A plain `httpx` request, even with a valid cookie,
sees only the SPA shell — zero cards. So we render through a local
Playwright Chromium instance with the cookie injected. Set
`LINKEDIN_USE_HTTP=1` to force the (mostly-broken) httpx path for testing.

Hard rate limit: 12-25s jittered between requests (enforced via the shared
RateLimiter, so concurrent terminals share the budget).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..paths import resolve as resolve_paths
from .base import (
    JobPosting,
    RateLimitConfig,
    SearchQuery,
    SourceError,
)


@dataclass
class LinkedInSource:
    name: str = "linkedin"
    base_url: str = "https://www.linkedin.com"
    rate_limit: RateLimitConfig | None = None

    def __post_init__(self) -> None:
        if self.rate_limit is None:
            self.rate_limit = RateLimitConfig("linkedin.com", 12.0, 25.0)

    def _cookie(self) -> str:
        v = os.environ.get("LINKEDIN_LI_AT")
        if not v:
            raise SourceError(
                f"LINKEDIN_LI_AT not set. Add it to {resolve_paths().secrets_env} "
                "(see references/sources/linkedin.md for how to capture)."
            )
        return v

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> str:
        """Default path: Playwright (handles JS-rendered cards).

        Set `LINKEDIN_USE_HTTP=1` to fall back to plain httpx (useful for
        contract tests; will see no cards in production).
        """
        if os.environ.get("LINKEDIN_USE_HTTP") == "1":
            return await self._fetch_via_httpx(client, url)
        return await self._fetch_via_playwright(url)

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=2, max=30),
        reraise=True,
    )
    async def _fetch_via_httpx(self, client: httpx.AsyncClient, url: str) -> str:
        cookies = {"li_at": self._cookie()}
        resp = await client.get(url, cookies=cookies)
        if resp.status_code in (403, 429, 999):
            raise SourceError(
                f"LinkedIn returned {resp.status_code} — likely flagged/limited. "
                "Refresh cookie or pause runs."
            )
        resp.raise_for_status()
        return resp.text

    async def _fetch_via_playwright(self, url: str) -> str:
        """Render LinkedIn with cookie. Returns the post-render HTML.

        Uses a **persistent** Chromium profile at
        `$XDG_DATA_HOME/job-hunter/chrome-profiles/linkedin/` so the same
        fingerprint + cookies survive between runs. LinkedIn aggressively
        invalidates `li_at` when each request comes from a fresh fingerprint;
        the persistent profile fixes this.

        First-run bootstrap: if the profile has no `li_at`, we inject from the
        env var and save it. On every run after, the profile's stored cookies
        are used (and LinkedIn's session-extension cookies — JSESSIONID,
        bcookie — accumulate naturally).
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise SourceError(
                "Playwright not installed. `playwright install chromium` then retry."
            ) from e

        from ..paths import resolve

        paths = resolve()
        profile_dir = paths.chrome_profile_linkedin
        profile_dir.mkdir(parents=True, exist_ok=True)
        first_run = not (profile_dir / "Default" / "Cookies").exists()

        cookie_value = self._cookie() if first_run else None

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            try:
                if first_run and cookie_value:
                    await context.add_cookies(
                        [
                            {
                                "name": "li_at",
                                "value": cookie_value,
                                "domain": ".linkedin.com",
                                "path": "/",
                                "httpOnly": True,
                                "secure": True,
                                "sameSite": "None",
                            }
                        ]
                    )

                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    raise SourceError(f"LinkedIn navigation failed: {e}") from e

                if "/login" in page.url or "/authwall" in page.url:
                    raise SourceError(
                        "LinkedIn redirected to login/authwall. Cookie invalid. "
                        "Refresh LINKEDIN_LI_AT, then delete "
                        f"{profile_dir} so the next run re-seeds it. "
                        "Or run `job-hunter linkedin-login` to authenticate interactively."
                    )

                # Wait for cards to materialize. If timeout, parser returns [].
                import contextlib

                with contextlib.suppress(Exception):
                    await page.wait_for_selector("[data-occludable-job-id]", timeout=15000)

                html: str = await page.content()
                return html
            finally:
                await context.close()

    async def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]:
        if not query.roles:
            return
        for role in query.roles:
            for location in query.locations or ["Brazil"]:
                kw = quote_plus(role)
                loc = quote_plus(location)
                url = f"{self.base_url}/jobs/search/?keywords={kw}" f"&location={loc}&f_TPR=r604800"
                try:
                    html = await self._fetch(client, url)
                except SourceError:
                    raise
                except httpx.HTTPError:
                    continue
                for posting in parse_search_results(html, self.base_url):
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
        body = tree.css_first(".show-more-less-html__markup, .description__text")
        if body is not None:
            posting.description = body.text(separator="\n").strip()
        return posting


def canonical_job_url(external_id: str) -> str:
    """Return the canonical `www.linkedin.com/jobs/view/<id>/` URL.

    LinkedIn serves the same posting from regional subdomains (in., br., uk.,
    mx., …). Normalizing avoids per-domain permission churn in
    claude-in-chrome and ensures dedup by URL works across regions.
    """
    return f"https://www.linkedin.com/jobs/view/{external_id}/"


def _dedup_title(raw: str) -> str:
    """LinkedIn's artdeco lockup doubles the title (visible + sr-only twin).
    Collapse 'X X' to 'X' when the halves match after whitespace normalize.
    """
    cleaned = " ".join(raw.split())
    half = len(cleaned) // 2
    if len(cleaned) > 40:
        left = cleaned[:half].strip()
        right = cleaned[-half:].strip() if half else ""
        if left and left == right:
            return left
    cleaned = cleaned.removesuffix(" with verification").strip()
    return cleaned


def normalize_job_url(url: str) -> str:
    """Rewrite any `<region>.linkedin.com/jobs/view/<id>` to canonical form.

    Public so other call sites (discover orchestrator's upsert path,
    test fixtures, manual URL massaging) can use it.
    """
    import re

    m = re.search(r"linkedin\.com/jobs/view/(\d+)", url)
    if not m:
        return url
    return canonical_job_url(m.group(1))


def parse_search_results(html: str, base_url: str) -> list[JobPosting]:
    """Parse a LinkedIn jobs search result list.

    LinkedIn serves TWO different layouts:

    1. **Authenticated** (when `li_at` is valid): SPA-style. Cards are
       `[data-occludable-job-id]` containing `.artdeco-entity-lockup__title` /
       `__subtitle` / `__caption`. The `data-occludable-job-id` attribute is
       the authoritative external_id.

    2. **Anonymous** (no cookie / cookie rejected): server-rendered. Cards are
       `.base-card` under `ul.jobs-search__results-list`. External_id comes
       from `/jobs/view/<id>` in the link href.

    We try the authenticated layout first; fall back to the anonymous one.
    """
    tree = HTMLParser(html)
    out: list[JobPosting] = []
    from selectolax.parser import Node

    # ── authenticated layout ────────────────────────────────────────────────
    auth_cards = tree.css("[data-occludable-job-id]")
    seen_ids: set[str] = set()
    for card in auth_cards:
        ext_id = card.attributes.get("data-occludable-job-id") or card.attributes.get("data-job-id")
        if not ext_id or ext_id in seen_ids:
            continue
        seen_ids.add(ext_id)
        title_el = card.css_first(".artdeco-entity-lockup__title")
        company_el = card.css_first(".artdeco-entity-lockup__subtitle")
        location_el = card.css_first(".artdeco-entity-lockup__caption")
        link_el = card.css_first("a.job-card-container__link, a[href*='/jobs/view/']")
        if not title_el:
            continue
        # Canonical URL: always `www.linkedin.com` regardless of regional subdomain
        # the user landed on (in.linkedin.com, br.linkedin.com, etc.). De-dups
        # the same posting across LinkedIn regions and avoids per-domain
        # permission prompts in claude-in-chrome.
        url = canonical_job_url(ext_id)
        # Dedup title text — LinkedIn's accessibility layer often doubles the
        # title in the artdeco lockup (visible text + sr-only twin).
        out.append(
            JobPosting(
                source="linkedin",
                external_id=ext_id,
                url=url,
                title=_dedup_title(title_el.text(strip=True)),
                company=company_el.text(strip=True) if company_el else "Unknown",
                location=location_el.text(strip=True) if location_el else None,
                raw_payload={"layout": "authenticated"},
            )
        )

    if out:
        return out

    # ── anonymous layout (fallback) ─────────────────────────────────────────
    seen_nodes: set[int] = set()
    cards: list[Node] = []
    for sel in ("ul.jobs-search__results-list li", ".jobs-search-results__list-item"):
        for node in tree.css(sel):
            key = id(node)
            if key in seen_nodes:
                continue
            seen_nodes.add(key)
            cards.append(node)
    for card in cards:
        title_el = card.css_first("h3.base-search-card__title, .base-search-card__title")
        company_el = card.css_first(
            "h4.base-search-card__subtitle, .base-search-card__subtitle, .hidden-nested-link"
        )
        location_el = card.css_first(".job-search-card__location, .base-search-card__metadata")
        link_el = card.css_first("a.base-card__full-link[href], a[href*='/jobs/view/']")
        if not (title_el and company_el and link_el):
            continue
        href = link_el.attributes.get("href") or ""
        url = href if href.startswith("http") else f"{base_url}{href}"
        external_id = ""
        if "/jobs/view/" in url:
            tail = url.split("/jobs/view/", 1)[1]
            external_id = tail.split("?", 1)[0].rstrip("/").split("/")[-1]
        external_id = external_id or url
        # Canonical URL (region-normalized) if we have a real numeric id.
        canonical = canonical_job_url(external_id) if external_id.isdigit() else url.split("?")[0]
        out.append(
            JobPosting(
                source="linkedin",
                external_id=external_id,
                url=canonical,
                title=_dedup_title(title_el.text(strip=True)),
                company=company_el.text(strip=True),
                location=location_el.text(strip=True) if location_el else None,
                raw_payload={"layout": "anonymous", "href": href},
            )
        )
    return out


SOURCE: LinkedInSource = LinkedInSource()
