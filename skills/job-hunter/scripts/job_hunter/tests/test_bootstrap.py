"""cli._bootstrap: the cross-platform port of install_hook.sh used by `job init`."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from job_hunter import paths as paths_mod
from job_hunter.cli import _bootstrap


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> paths_mod.Paths:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    return paths_mod.resolve()


def test_creates_layout_and_copies_templates(home: paths_mod.Paths) -> None:
    _bootstrap(home)
    for d in (home.secrets_dir, home.tracking_dir, home.files_dir, home.runs_dir, home.logs_dir):
        assert d.is_dir(), f"missing: {d}"
    assert home.secrets_env.exists()
    assert home.profile_yaml.exists()


def test_does_not_clobber_user_edits(home: paths_mod.Paths) -> None:
    _bootstrap(home)
    home.profile_yaml.write_text("roles: [my-custom-edit]\n", encoding="utf-8")
    home.secrets_env.write_text("# user-edited\n", encoding="utf-8")
    _bootstrap(home)
    assert home.profile_yaml.read_text(encoding="utf-8") == "roles: [my-custom-edit]\n"
    assert "user-edited" in home.secrets_env.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_secrets_chmodded_600(home: paths_mod.Paths) -> None:
    _bootstrap(home)
    home.secrets_env.chmod(0o644)
    _bootstrap(home)
    assert home.secrets_env.stat().st_mode & 0o777 == 0o600
    assert not (home.secrets_env.stat().st_mode & stat.S_IROTH)
