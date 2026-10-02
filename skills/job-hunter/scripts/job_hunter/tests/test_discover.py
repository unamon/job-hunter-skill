"""End-to-end discovery: source mocked → DB upsert."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from job_hunter import paths as paths_mod
from job_hunter.db import get_engine, run_migrations
from job_hunter.discover import run_discover, upsert_posting
from job_hunter.models import Application, Job, Stage
from job_hunter.paths import Paths
from job_hunter.sources.base import JobPosting, SearchQuery
from job_hunter.sources.remoteok import RemoteOKSource

FIXTURES = Path(__file__).parent / "fixtures"
Isolated = tuple[Paths, Engine]


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Isolated:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    p = paths_mod.resolve()
    p.ensure()
    run_migrations(p)
    return p, get_engine(p)


def _fixture_items() -> list[dict[str, object]]:
    data = json.loads((FIXTURES / "remoteok.json").read_text(encoding="utf-8"))
    return [item for item in data[1:] if isinstance(item, dict) and item.get("id")]


def test_remoteok_discover_inserts_jobs_and_apps(isolated: Isolated) -> None:
    paths, eng = isolated
    src = RemoteOKSource()

    async def fake_fetch(
        self: RemoteOKSource,
        client: httpx.AsyncClient,  # noqa: ARG001
    ) -> list[dict[str, object]]:
        return _fixture_items()

    RemoteOKSource._fetch = fake_fetch  # type: ignore[method-assign]
    try:
        with Session(eng) as sess:
            report, _ = asyncio.run(
                run_discover(
                    src,
                    SearchQuery(roles=["Android", "Kotlin"], exclude_keywords=["junior"]),
                    paths,
                    sess,
                )
            )
    finally:
        del RemoteOKSource._fetch

    assert report.failed == 0
    # 3 items in fixture; one is "Junior Android" excluded.
    assert report.discovered == 2
    assert report.new == 2

    with Session(eng) as sess:
        jobs = sess.exec(select(Job)).all()
        apps = sess.exec(select(Application)).all()
        assert len(jobs) == 2
        assert all(a.current_stage == Stage.DISCOVERED.value for a in apps)
        assert len(apps) == 2


def test_re_discover_updates_existing_row(isolated: Isolated) -> None:
    _, eng = isolated
    posting = JobPosting(
        source="remoteok",
        external_id="abc123",
        url="https://example.com/abc",
        title="Senior Android Engineer",
        company="Nubank",
    )
    with Session(eng) as sess:
        jid_1, new_1 = upsert_posting(sess, posting)
    assert new_1 is True

    posting.title = "Senior Android Engineer (updated)"
    with Session(eng) as sess:
        jid_2, new_2 = upsert_posting(sess, posting)
    assert new_2 is False
    assert jid_1 == jid_2

    with Session(eng) as sess:
        job = sess.get(Job, jid_2)
        assert job is not None
        assert job.title == "Senior Android Engineer (updated)"
        apps = sess.exec(select(Application)).all()
        assert len(apps) == 1


def test_run_dir_and_report_written(isolated: Isolated) -> None:
    paths, eng = isolated
    src = RemoteOKSource()

    async def fake_fetch(
        self: RemoteOKSource,
        client: httpx.AsyncClient,  # noqa: ARG001
    ) -> list[dict[str, object]]:
        return _fixture_items()

    RemoteOKSource._fetch = fake_fetch  # type: ignore[method-assign]
    try:
        with Session(eng) as sess:
            asyncio.run(run_discover(src, SearchQuery(), paths, sess))
    finally:
        del RemoteOKSource._fetch

    runs = sorted(paths.runs_dir.iterdir())
    assert runs, "no run dir created"
    report_file = runs[-1] / "report.json"
    assert report_file.exists()
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["source"] == "remoteok"
    assert "started_at" in data and "finished_at" in data
