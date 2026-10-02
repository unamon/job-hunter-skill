"""XDG path resolution across (override-set, override-unset) x (XDG vars set, unset)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from job_hunter import paths as paths_mod


def _clear_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "JOB_HUNTER_HOME_OVERRIDE"):
        monkeypatch.delenv(var, raising=False)


def test_override_wins_over_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths_mod.clear_cache()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "should-be-ignored"))
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path / "override"))

    p = paths_mod.resolve()
    assert p.config_dir == tmp_path / "override" / "config" / "job-hunter"
    assert p.data_dir == tmp_path / "override" / "data" / "job-hunter"
    assert p.state_dir == tmp_path / "override" / "state" / "job-hunter"


def test_xdg_env_vars_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths_mod.clear_cache()
    _clear_xdg(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    p = paths_mod.resolve()
    assert p.config_dir == tmp_path / "cfg" / "job-hunter"
    assert p.data_dir == tmp_path / "data" / "job-hunter"
    assert p.state_dir == tmp_path / "state" / "job-hunter"


def test_defaults_when_xdg_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths_mod.clear_cache()
    _clear_xdg(monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    p = paths_mod.resolve()
    if sys.platform == "win32":
        # platformdirs uses the Known Folder API (not HOME): %LOCALAPPDATA%\job-hunter
        assert p.config_dir.name == "job-hunter"
        assert p.config_dir == p.data_dir == p.state_dir
        return
    if sys.platform == "darwin":
        assert "Library" in p.config_dir.parts
        return
    # On Linux, platformdirs falls back to ~/.config, ~/.local/share, ~/.local/state
    assert p.config_dir.as_posix().endswith(".config/job-hunter")
    assert p.data_dir.as_posix().endswith(".local/share/job-hunter")
    assert p.state_dir.as_posix().endswith(".local/state/job-hunter")


def test_ensure_creates_all_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    p = paths_mod.resolve()
    p.ensure()
    for d in (
        p.config_dir,
        p.secrets_dir,
        p.data_dir,
        p.tracking_dir,
        p.adapters_inbox,
        p.adapters_user,
        p.files_dir,
        p.runs_dir,
        p.state_dir,
        p.logs_dir,
    ):
        assert d.exists() and d.is_dir(), f"missing: {d}"

    # idempotent
    p.ensure()
