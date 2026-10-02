"""End-to-end smoke for the Phase 2 CLI verbs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_hunter import paths as paths_mod
from job_hunter.cli import app


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    return tmp_path


def test_init_creates_layout_and_runs_migrations(isolated_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    # `init` calls doctor at the end; doctor may fail on missing playwright/gh
    # in the test env, which is expected. We assert the install half succeeded.
    cfg = isolated_home / "config" / "job-hunter"
    data = isolated_home / "data" / "job-hunter"
    assert (cfg / "secrets" / "personal.env").exists()
    assert (data / "jobs.db").exists()
    assert "Applied migrations" in result.stdout or "up to date" in result.stdout


def test_sync_writes_tracking_md_on_empty_db(isolated_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init"])

    os.environ["JOB_HUNTER_FREEZE_NOW"] = "2026-05-13T14:00:00-03:00"
    try:
        result = runner.invoke(app, ["sync"])
    finally:
        os.environ.pop("JOB_HUNTER_FREEZE_NOW", None)

    assert result.exit_code == 0, result.stdout
    index = isolated_home / "data" / "job-hunter" / "tracking.md"
    assert index.exists()
    assert "# Job tracking" in index.read_text(encoding="utf-8")


def test_doctor_runs(isolated_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["doctor"])
    # rc may be 1 because playwright/gh aren't expected in this sandbox
    assert "job-hunter doctor" in result.stdout


def test_lint_runs_on_empty_runtime(isolated_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 0, result.stdout


def test_info_prints_paths(isolated_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert str(isolated_home) in result.stdout


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "job-hunter " in result.stdout
