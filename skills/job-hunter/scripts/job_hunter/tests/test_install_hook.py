"""install_hook.sh: creates XDG dirs, copies templates if absent, never clobbers."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# `job init` uses the Python port (cli._bootstrap) everywhere; the shell hook
# is kept for POSIX users who run it directly. See test_bootstrap.py.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash hook is POSIX-only")

REPO_ROOT = Path(__file__).resolve().parents[5]
HOOK = REPO_ROOT / "skills" / "job-hunter" / "scripts" / "install_hook.sh"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def test_creates_dirs_on_clean_home(tmp_path: Path) -> None:
    env = {"JOB_HUNTER_HOME_OVERRIDE": str(tmp_path)}
    r = _run(env)
    assert r.returncode == 0, r.stderr

    cfg = tmp_path / "config" / "job-hunter"
    data = tmp_path / "data" / "job-hunter"
    state = tmp_path / "state" / "job-hunter"
    for d in (
        cfg / "secrets",
        data / "tracking",
        data / "adapters_inbox",
        data / "adapters_user",
        data / "files",
        data / "runs",
        state / "logs",
    ):
        assert d.is_dir(), f"missing: {d}"


def test_templates_copied_and_chmodded(tmp_path: Path) -> None:
    env = {"JOB_HUNTER_HOME_OVERRIDE": str(tmp_path)}
    r = _run(env)
    assert r.returncode == 0, r.stderr

    secrets = tmp_path / "config" / "job-hunter" / "secrets" / "personal.env"
    profile = tmp_path / "config" / "job-hunter" / "profile.yaml"
    assert secrets.is_file()
    assert profile.is_file()

    mode = secrets.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_idempotent_does_not_clobber_user_edits(tmp_path: Path) -> None:
    env = {"JOB_HUNTER_HOME_OVERRIDE": str(tmp_path)}
    _run(env)

    secrets = tmp_path / "config" / "job-hunter" / "secrets" / "personal.env"
    profile = tmp_path / "config" / "job-hunter" / "profile.yaml"

    profile.write_text("roles: [my-custom-edit]\n")
    secrets.write_text("LINKEDIN_LI_AT=user-edited  # job-hunter:allow-pii\n")
    # Make sure perms stay even after a manual write
    secrets.chmod(0o644)

    r = _run(env)
    assert r.returncode == 0, r.stderr

    assert (
        profile.read_text(encoding="utf-8") == "roles: [my-custom-edit]\n"
    ), "profile.yaml was clobbered"
    assert "user-edited" in secrets.read_text(encoding="utf-8"), "secrets.env was clobbered"

    mode = secrets.stat().st_mode & 0o777
    assert mode == 0o600, f"expected hook to re-chmod 600, got {oct(mode)}"
    assert not (secrets.stat().st_mode & stat.S_IROTH), "secrets is world-readable"
