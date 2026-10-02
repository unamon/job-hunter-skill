"""Firecrawl client.

Opt-in transport for Indeed + Glassdoor only. Self-hosted via Docker. Set:

    FIRECRAWL_ENDPOINT=http://localhost:3002

in the secrets file (`job-hunter info` shows where). Presence of this env var is
the opt-in signal; no additional config flag.

PII boundary: this client is read-only. It scrapes job listings. It MUST NOT
be used for form-fill (apply.py asserts this). See references/firecrawl.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


class FirecrawlError(Exception):
    """Firecrawl returned non-success or unreachable."""


@dataclass(frozen=True)
class FirecrawlClient:
    endpoint: str
    api_key: str | None = None
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> FirecrawlClient | None:
        endpoint = os.environ.get("FIRECRAWL_ENDPOINT") or os.environ.get(
            "JOB_HUNTER_FIRECRAWL_ENDPOINT"
        )
        if not endpoint:
            return None
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        return cls(endpoint=endpoint.rstrip("/"), api_key=api_key or None)

    async def scrape_html(self, url: str, *, client: httpx.AsyncClient) -> str:
        """POST /v1/scrape, return the HTML field. Raises FirecrawlError on failure."""
        payload: dict[str, object] = {
            "url": url,
            "formats": ["html"],
            "onlyMainContent": False,
            "timeout": int(self.timeout * 1000),
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = await client.post(
                f"{self.endpoint}/v1/scrape",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise FirecrawlError(f"Firecrawl unreachable at {self.endpoint}: {e}") from e
        if resp.status_code >= 400:
            raise FirecrawlError(f"Firecrawl returned {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as e:
            raise FirecrawlError(f"Firecrawl returned non-JSON: {e}") from e
        if not body.get("success", False):
            raise FirecrawlError(f"Firecrawl scrape failed: {body.get('error', body)}")
        data = body.get("data") or {}
        html = data.get("html") or data.get("rawHtml")
        if not html:
            keys = sorted(data.keys())
            raise FirecrawlError(f"Firecrawl response missing html field. Keys: {keys}")
        return str(html)


def assert_apply_path_safe() -> None:
    """Defense in depth. apply.py calls this; raises if Firecrawl is enabled.

    Apply touches PII (filled form values). Routing through Firecrawl would leak.
    """
    if FirecrawlClient.from_env() is not None:
        raise RuntimeError(
            "FIRECRAWL_ENDPOINT is set. apply.py refuses to run with Firecrawl "
            "configured — form-fill traffic would expose PII to the scraping backend. "
            "Unset FIRECRAWL_ENDPOINT for this terminal, or use --dry-run."
        )
