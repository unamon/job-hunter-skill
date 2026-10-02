"""CLI verbs added in Phase 6: list, show, queue, stage, apply --dry-run,
adapter list/promote/test/mark-auto-eligible, review, report."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlmodel import Session
from typer.testing import CliRunner

from job_hunter import paths as paths_mod
from job_hunter.cli import app
from job_hunter.db import get_engine, run_migrations
from job_hunter.models import Application, Job, Stage

runner = CliRunner()


@pytest.fixture
def populated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> paths_mod.Paths:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    monkeypatch.setenv("JOB_HUNTER_FREEZE_NOW", "2026-05-13T14:00:00-03:00")
    p = paths_mod.resolve()
    p.ensure()
    run_migrations(p)
    eng = get_engine(p)
    with Session(eng) as sess:
        j = Job(
            source="remoteok",
            external_id="ext1",
            url="https://example.com/jobs/1",
            title="Senior Android Engineer",
            company="Nubank",
            scraped_at=datetime(2026, 5, 10),
            fingerprint="fp1",
        )
        sess.add(j)
        sess.commit()
        sess.refresh(j)
        assert j.id is not None
        a = Application(
            job_id=j.id,
            current_stage=Stage.DISCOVERED.value,
            updated_at=datetime(2026, 5, 10),
        )
        sess.add(a)
        sess.commit()
    return p


def test_list_prints_rows(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 0, r.stdout
    assert "Nubank" in r.stdout
    assert "discovered" in r.stdout


def test_show_application(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["show", "1"])
    assert r.exit_code == 0
    assert "Nubank" in r.stdout
    assert "Senior Android Engineer" in r.stdout
    assert "discovered" in r.stdout


def test_show_missing_id_exits_nonzero(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["show", "999"])
    assert r.exit_code == 1
    assert "not found" in r.stdout


def test_queue_transitions_to_queued(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["queue", "1"])
    assert r.exit_code == 0
    assert "discovered → queued" in r.stdout

    # Idempotent — queueing again is a noop
    r2 = runner.invoke(app, ["queue", "1"])
    assert r2.exit_code == 0
    assert "already in stage queued" in r2.stdout


def test_stage_transition_with_note(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["stage", "1", "--to", "applied", "--note", "test note"])
    assert r.exit_code == 0
    assert "discovered → applied" in r.stdout


def test_stage_unknown_value_exits_nonzero(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["stage", "1", "--to", "bogus"])
    assert r.exit_code == 2
    assert "unknown stage" in r.stdout


def test_adapter_list_shows_6_bundled(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["adapter", "list"])
    assert r.exit_code == 0
    for sig in ("gupy", "greenhouse", "lever", "workday", "ashby", "workable"):
        assert sig in r.stdout
    assert "bundled" in r.stdout


def test_adapter_test_matching_url(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["adapter", "test", "gupy", "--url", "https://nubank.gupy.io/jobs/123"])
    assert r.exit_code == 0
    assert "match" in r.stdout


def test_adapter_test_non_matching_url(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["adapter", "test", "gupy", "--url", "https://example.com/jobs/1"])
    assert r.exit_code == 1


def test_adapter_promote_from_inbox(populated: paths_mod.Paths) -> None:
    draft = {
        "platform_signature": "abc123def4567890",
        "version": 1,
        "match": {"url_pattern": "https://test.example.com/*"},
        "fields": [{"selector": "input", "source": "profile.full_name"}],
        "submit": {"selector": "button[type='submit']"},
    }
    (populated.adapters_inbox / "abc123def4567890.yaml").write_text(yaml.safe_dump(draft))

    r = runner.invoke(app, ["adapter", "promote", "abc123def4567890"])
    assert r.exit_code == 0
    assert "Promoted" in r.stdout

    # Inbox emptied, user dir populated
    assert not (populated.adapters_inbox / "abc123def4567890.yaml").exists()
    assert (populated.adapters_user / "abc123def4567890.yaml").exists()


def test_adapter_mark_auto_eligible_flips_flag(populated: paths_mod.Paths) -> None:
    sig = "abc123def4567890"
    yaml_text = """
platform_signature: abc123def4567890
version: 1
match:
  url_pattern: "https://test.example.com/*"
fields:
  - selector: "input"
    source: "profile.full_name"
submit:
  selector: "button[type='submit']"
  auto_eligible: false
"""
    (populated.adapters_user / f"{sig}.yaml").write_text(yaml_text)
    r = runner.invoke(app, ["adapter", "mark-auto-eligible", sig])
    assert r.exit_code == 0
    new_text = (populated.adapters_user / f"{sig}.yaml").read_text(encoding="utf-8")
    assert "auto_eligible: true" in new_text


def test_review_empty(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["review"])
    assert r.exit_code == 0
    assert "Nothing to review" in r.stdout


def test_review_lists_inbox_drafts(populated: paths_mod.Paths) -> None:
    (populated.adapters_inbox / "abc1234567890def.yaml").write_text(
        "platform_signature: abc1234567890def\nfields: []\nsubmit:\n  selector: btn\n"
    )
    r = runner.invoke(app, ["review"])
    assert r.exit_code == 0
    assert "abc1234567890def" in r.stdout


def test_report_prints_stage_counts(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["report"])
    assert r.exit_code == 0
    assert "discovered" in r.stdout
    assert "total: 1" in r.stdout


def test_apply_dry_run_against_gupy_url(populated: paths_mod.Paths) -> None:
    # First make the Job URL match Gupy.
    eng = get_engine(populated)
    with Session(eng) as sess:
        j = sess.get(Job, 1)
        assert j is not None
        j.url = "https://nubank.gupy.io/jobs/4392838"
        sess.add(j)
        sess.commit()

    r = runner.invoke(app, ["apply", "1", "--dry-run"])
    assert r.exit_code == 0, r.stdout
    assert "gupy" in r.stdout
    assert "Field plan" in r.stdout
    assert "required field(s) missing" in r.stdout  # we have empty profile/secrets
    assert "--dry-run: skipping browser" in r.stdout


def test_apply_dry_run_no_adapter_match(populated: paths_mod.Paths) -> None:
    r = runner.invoke(app, ["apply", "1", "--dry-run"])
    assert r.exit_code == 1
    assert "no adapter" in r.stdout
