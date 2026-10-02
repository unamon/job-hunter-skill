"""FirecrawlClient + Indeed/Glassdoor routing + apply-path defense."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from job_hunter import paths as paths_mod
from job_hunter.firecrawl_client import (
    FirecrawlClient,
    FirecrawlError,
    assert_apply_path_safe,
)
from job_hunter.sources.glassdoor import GlassdoorSource
from job_hunter.sources.indeed import IndeedSource


def _fc_response(html: str) -> dict[str, object]:
    return {"success": True, "data": {"html": html, "metadata": {}}}


def test_from_env_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_ENDPOINT", raising=False)
    monkeypatch.delenv("JOB_HUNTER_FIRECRAWL_ENDPOINT", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert FirecrawlClient.from_env() is None


def test_from_env_picks_up_endpoint_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_ENDPOINT", "http://localhost:3002/")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    fc = FirecrawlClient.from_env()
    assert fc is not None
    assert fc.endpoint == "http://localhost:3002"  # trailing slash stripped
    assert fc.api_key == "fc-test"


def test_scrape_html_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fc = FirecrawlClient(endpoint="http://fc.test")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["url"] == "https://example.com/foo"
        assert "html" in body["formats"]
        return httpx.Response(200, json=_fc_response("<html><body>ok</body></html>"))

    transport = httpx.MockTransport(handler)

    async def go() -> str:
        async with httpx.AsyncClient(transport=transport) as client:
            return await fc.scrape_html("https://example.com/foo", client=client)

    assert "ok" in asyncio.run(go())


def test_scrape_html_raises_on_error_status() -> None:
    fc = FirecrawlClient(endpoint="http://fc.test")
    transport = httpx.MockTransport(lambda _: httpx.Response(500, text="boom"))

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await fc.scrape_html("https://example.com", client=client)

    with pytest.raises(FirecrawlError):
        asyncio.run(go())


def test_scrape_html_raises_when_success_false() -> None:
    fc = FirecrawlClient(endpoint="http://fc.test")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"success": False, "error": "blocked"})
    )

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await fc.scrape_html("https://example.com", client=client)

    with pytest.raises(FirecrawlError) as exc:
        asyncio.run(go())
    assert "blocked" in str(exc.value)


FIXTURES = Path(__file__).parent / "fixtures"


def test_indeed_routes_through_firecrawl_when_endpoint_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_ENDPOINT", "http://fc.test")

    indeed_html = (FIXTURES / "indeed.html").read_text(encoding="utf-8")
    routes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.append(str(request.url))
        if "fc.test" in str(request.url):
            return httpx.Response(200, json=_fc_response(indeed_html))
        return httpx.Response(500, text="should have used firecrawl")

    transport = httpx.MockTransport(handler)
    src = IndeedSource()

    async def go() -> str:
        async with httpx.AsyncClient(transport=transport) as client:
            return await src._fetch(client, "https://br.indeed.com/jobs?q=android")

    body = asyncio.run(go())
    assert "Nubank" in body
    # Confirm we hit Firecrawl, not the direct site
    assert any("fc.test" in r for r in routes)
    assert not any("br.indeed.com" in r for r in routes)


def test_indeed_direct_fetch_when_no_firecrawl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_ENDPOINT", raising=False)
    monkeypatch.delenv("JOB_HUNTER_FIRECRAWL_ENDPOINT", raising=False)

    indeed_html = (FIXTURES / "indeed.html").read_text(encoding="utf-8")
    routes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.append(str(request.url))
        return httpx.Response(200, text=indeed_html)

    transport = httpx.MockTransport(handler)
    src = IndeedSource()

    async def go() -> str:
        async with httpx.AsyncClient(transport=transport) as client:
            return await src._fetch(client, "https://br.indeed.com/jobs?q=android")

    asyncio.run(go())
    assert all("br.indeed.com" in r for r in routes)


def test_glassdoor_routes_through_firecrawl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_ENDPOINT", "http://fc.test")
    # Cookies should NOT be checked when Firecrawl is enabled.
    monkeypatch.delenv("GLASSDOOR_GD_ID", raising=False)
    monkeypatch.delenv("GLASSDOOR_UAC", raising=False)

    gd_html = (FIXTURES / "glassdoor_listing.html").read_text(encoding="utf-8")

    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_fc_response(gd_html)))
    src = GlassdoorSource()

    async def go() -> str:
        async with httpx.AsyncClient(transport=transport) as client:
            return await src._fetch(
                client,
                "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=android",
            )

    body = asyncio.run(go())
    assert "Senior Android" in body


def test_apply_path_blocked_when_firecrawl_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_ENDPOINT", "http://fc.test")
    with pytest.raises(RuntimeError) as exc:
        assert_apply_path_safe()
    assert "FIRECRAWL_ENDPOINT" in str(exc.value)


def test_apply_path_safe_when_firecrawl_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_ENDPOINT", raising=False)
    monkeypatch.delenv("JOB_HUNTER_FIRECRAWL_ENDPOINT", raising=False)
    # No raise.
    assert_apply_path_safe()


def test_plan_for_url_blocks_when_firecrawl_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    monkeypatch.setenv("FIRECRAWL_ENDPOINT", "http://fc.test")
    p = paths_mod.resolve()
    p.ensure()
    from job_hunter.apply import ApplyInputs, plan_for_url
    from job_hunter.models import FillMode

    inputs = ApplyInputs(
        paths=p,
        application_id=1,
        url="https://nubank.gupy.io/jobs/123",
        locale_hint="pt-BR",
        mode=FillMode.SHADOW,
        i_understand=False,
    )
    with pytest.raises(RuntimeError):
        plan_for_url(inputs)
