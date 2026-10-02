"""Determinism + notes-block preservation for tracking_md."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from job_hunter import paths as paths_mod
from job_hunter import tracking_md
from job_hunter.db import get_engine, run_migrations
from job_hunter.models import Application, Job, Stage, StageHistory
from job_hunter.paths import Paths

TZ = ZoneInfo("America/Sao_Paulo")
FROZEN_NOW = datetime(2026, 5, 13, 14, 0, 0, tzinfo=TZ)

DBFixture = tuple[Paths, Engine]


@pytest.fixture
def db_with_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DBFixture:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    p = paths_mod.resolve()
    p.ensure()
    run_migrations(p)

    eng = get_engine(p)
    with Session(eng) as sess:
        j1 = Job(
            source="linkedin",
            external_id="ext-1",
            url="https://example.com/jobs/1",
            title="Senior Android Engineer",
            company="Nubank",
            location="Remote / BR",
            scraped_at=datetime(2026, 5, 10, tzinfo=UTC).replace(tzinfo=None),
            fingerprint="fp1",
            description="Build great Android apps.",
        )
        j2 = Job(
            source="gupy",
            external_id="ext-2",
            url="https://example.com/jobs/2",
            title="Mobile Platform Engineer (KMP)",
            company="Stark Bank",
            scraped_at=datetime(2026, 5, 11, tzinfo=UTC).replace(tzinfo=None),
            fingerprint="fp2",
        )
        sess.add(j1)
        sess.add(j2)
        sess.commit()
        sess.refresh(j1)
        sess.refresh(j2)

        assert j1.id is not None and j2.id is not None

        a1 = Application(
            job_id=j1.id,
            current_stage=Stage.TECHNICAL.value,
            next_action="Coding challenge",
            next_action_due=datetime(2026, 5, 15, tzinfo=TZ).replace(tzinfo=None),
            adapter_used="linkedin-easyapply",
            updated_at=datetime(2026, 5, 12, tzinfo=TZ).replace(tzinfo=None),
        )
        a2 = Application(
            job_id=j2.id,
            current_stage=Stage.APPLIED.value,
            updated_at=datetime(2026, 5, 11, tzinfo=TZ).replace(tzinfo=None),
        )
        sess.add(a1)
        sess.add(a2)
        sess.commit()
        sess.refresh(a1)
        sess.refresh(a2)

        assert a1.id is not None
        sess.add(
            StageHistory(
                application_id=a1.id,
                from_stage=Stage.QUEUED.value,
                to_stage=Stage.APPLIED.value,
                transitioned_at=datetime(2026, 5, 11, tzinfo=TZ).replace(tzinfo=None),
                note="submitted",
            )
        )
        sess.add(
            StageHistory(
                application_id=a1.id,
                from_stage=Stage.APPLIED.value,
                to_stage=Stage.TECHNICAL.value,
                transitioned_at=datetime(2026, 5, 12, tzinfo=TZ).replace(tzinfo=None),
            )
        )
        sess.commit()

    return p, eng


def test_byte_identical_across_regenerations(db_with_data: DBFixture) -> None:
    p, eng = db_with_data

    with Session(eng) as sess:
        tracking_md.regenerate(p, sess, now=FROZEN_NOW)
    first_index = p.tracking_index.read_bytes()
    first_files = {f.name: f.read_bytes() for f in sorted(p.tracking_dir.glob("*.md"))}

    with Session(eng) as sess:
        tracking_md.regenerate(p, sess, now=FROZEN_NOW)
    second_index = p.tracking_index.read_bytes()
    second_files = {f.name: f.read_bytes() for f in sorted(p.tracking_dir.glob("*.md"))}

    assert first_index == second_index, "tracking.md drifted between regenerations"
    assert first_files.keys() == second_files.keys(), "per-job filenames drifted"
    for name in first_files:
        assert first_files[name] == second_files[name], f"per-job file drifted: {name}"


def test_notes_block_preserved(db_with_data: DBFixture) -> None:
    p, eng = db_with_data

    with Session(eng) as sess:
        tracking_md.regenerate(p, sess, now=FROZEN_NOW)
    targets = sorted(p.tracking_dir.glob("*.md"))
    assert targets, "expected per-job files"
    target = targets[0]

    text = target.read_text(encoding="utf-8")
    user_added = "Things I'm researching:\n- Their CI setup"
    new = text.replace(
        tracking_md.NOTES_START + "\n\n" + tracking_md.NOTES_END,
        f"{tracking_md.NOTES_START}\n{user_added}\n{tracking_md.NOTES_END}",
    )
    target.write_text(new, encoding="utf-8", newline="\n")

    with Session(eng) as sess:
        tracking_md.regenerate(p, sess, now=FROZEN_NOW)
    rewritten = target.read_text(encoding="utf-8")
    assert user_added in rewritten, "user notes lost across regeneration"


def test_index_contains_active_and_stages(db_with_data: DBFixture) -> None:
    p, eng = db_with_data
    with Session(eng) as sess:
        tracking_md.regenerate(p, sess, now=FROZEN_NOW)
    text = p.tracking_index.read_text(encoding="utf-8")
    assert "## Active pipeline" in text
    assert "Nubank" in text
    assert "Stark Bank" in text
    assert "## Weekly summary" in text


def test_slugify_ascii() -> None:
    expected = "nubank-senior-android-engineer"
    assert tracking_md.slugify("Nubank — Senior Android Engineer") == expected
    assert tracking_md.slugify("Itaú Unibanco") == "itau-unibanco"
    assert tracking_md.slugify("") == "untitled"
    assert tracking_md.slugify("!!!") == "untitled"
