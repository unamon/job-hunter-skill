"""Gupy portal search: mapping + per-role discover with dedupe and title filter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from job_hunter import paths as paths_mod
from job_hunter.sources.base import SearchQuery
from job_hunter.sources.gupy import GupySource, posting_from_portal


def _row(job_id: int, name: str, workplace: str = "remote", **extra: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "careerPageName": "Acme",
        "careerPageUrl": "",  # empty in real portal responses
        "jobUrl": f"https://acme.gupy.io/job/b64token{job_id}?jobBoardSource=gupy_portal",
        "workplaceType": workplace,
        "city": extra.get("city", ""),
        "state": extra.get("state", ""),
        "publishedDate": "2026-09-30T12:00:00.000Z",
        "description": "desc",
    }


def test_posting_from_portal_uses_canonical_url() -> None:
    p = posting_from_portal(_row(42, "Dev .NET Pleno", "hybrid", city="São Paulo", state="SP"))
    assert p is not None
    assert p.url == "https://acme.gupy.io/jobs/42"
    assert p.external_id == "42"
    assert p.company == "Acme"
    assert p.location == "São Paulo, SP"
    assert p.remote is False
    assert p.posted_at is not None and p.posted_at.utcoffset() is not None


def test_posting_from_portal_remote_and_incomplete() -> None:
    p = posting_from_portal(_row(1, "Dev C#"))
    assert p is not None and p.remote is True and p.location == "Remote"
    assert posting_from_portal({"id": 1, "name": "x"}) is None


async def test_discover_searches_each_role_dedupes_and_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    results = {
        ".NET": [_row(1, "Dev .NET Pleno"), _row(2, "Dev .NET Júnior"), _row(3, "Analista RH")],
        "C#": [_row(1, "Dev .NET Pleno"), _row(4, "Backend C# Sênior")],
    }
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        term = request.url.params["jobName"]
        asked.append(term)
        rows = results.get(term, [])
        return httpx.Response(200, json={"data": rows, "pagination": {"total": len(rows)}})

    query = SearchQuery(roles=[".NET", "C#"], exclude_keywords=["júnior"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = [p async for p in GupySource().discover(query, client)]

    assert asked == [".NET", "C#"]
    assert [p.external_id for p in found] == ["1", "4"]
