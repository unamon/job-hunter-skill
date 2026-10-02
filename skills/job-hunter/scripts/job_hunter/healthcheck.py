"""`job doctor` implementation.

Validates the user's install so the next `job apply` doesn't crash mid-form.
Reads only metadata (file existence, permissions, env-var presence by name).
NEVER reads the secrets file contents.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .paths import Paths, resolve

console = Console()


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _python_version() -> Check:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 12)
    return Check(
        "Python >= 3.12",
        ok,
        f"running {major}.{minor}.{sys.version_info.micro}",
    )


def _uv_installed() -> Check:
    path = shutil.which("uv")
    return Check("uv on PATH", path is not None, path or "not found")


def _playwright_chromium(paths: Paths) -> Check:
    """Ask the bundled playwright (same interpreter) where Chromium lives.

    `playwright install --dry-run` exits 0 whether or not the browser is
    present, so we parse its install location and check the directory.
    Running via `sys.executable -m playwright` works even when the
    `playwright` console script isn't on PATH (e.g. `uv tool install`).
    """
    if "BROWSER_WS_ENDPOINT" in os.environ:
        return Check(
            "Playwright Chromium",
            True,
            f"skipped — using remote endpoint {os.environ['BROWSER_WS_ENDPOINT']!r}",
        )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        return Check("Playwright Chromium", False, f"check failed: {exc}")
    if result.returncode != 0:
        return Check("Playwright Chromium", False, "playwright package not importable")
    location = _chromium_install_location(result.stdout)
    if location is None:
        return Check("Playwright Chromium", False, "could not parse `playwright install --dry-run`")
    if not location.exists():
        return Check(
            "Playwright Chromium",
            False,
            f"not installed — run `{Path(sys.executable).name} -m playwright install chromium`",
        )
    return Check("Playwright Chromium", True, str(location))


def _chromium_install_location(dry_run_output: str) -> Path | None:
    """First `Install location:` after the `browser: chromium ...` header."""
    in_chromium = False
    for line in dry_run_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("browser:"):
            in_chromium = stripped.split()[1:2] == ["chromium"]
        elif in_chromium and stripped.startswith("Install location:"):
            return Path(stripped.split(":", 1)[1].strip())
    return None


def _xdg_dirs(paths: Paths) -> Iterator[Check]:
    for label, p in (
        ("Config dir", paths.config_dir),
        ("Data dir", paths.data_dir),
        ("State dir", paths.state_dir),
        ("Logs dir", paths.logs_dir),
    ):
        yield Check(label, p.exists(), str(p))


def _secrets_perms(paths: Paths) -> Check:
    p = paths.secrets_env
    if not p.exists():
        return Check(
            "Secrets file present",
            False,
            f"missing {p} — run `job init` and edit (never `cat` it)",
        )
    if os.name == "nt":
        # POSIX mode bits are meaningless on Windows (always reports 0o666).
        # %LOCALAPPDATA% is already ACL'd to the current user.
        return Check("Secrets file present", True, f"{p} (per-user AppData)")
    mode = p.stat().st_mode & 0o777
    if mode == 0o600:
        return Check("Secrets file 0600", True, f"{p} permissions ok")
    return Check(
        "Secrets file 0600",
        False,
        f"{p} is {oct(mode)} — run `chmod 600 {p}`",
    )


def _secrets_world_readable(paths: Paths) -> Check:
    """Even if not 0600, flag anything WORLD-readable as a hard failure."""
    p = paths.secrets_env
    if not p.exists():
        return Check("Secrets not world-readable", True, "n/a (file absent)")
    if os.name == "nt":
        return Check("Secrets not world-readable", True, "n/a on Windows (per-user ACL)")
    mode = p.stat().st_mode
    world = mode & stat.S_IROTH
    return Check(
        "Secrets not world-readable",
        world == 0,
        "ok" if world == 0 else f"WORLD-READABLE: {p} — chmod 600 immediately",
    )


def _profile_yaml(paths: Paths) -> Check:
    p = paths.profile_yaml
    return Check(
        "profile.yaml present",
        p.exists(),
        str(p) if p.exists() else "missing — run `job init`",
    )


def _gh_auth() -> Check:
    gh = shutil.which("gh")
    if gh is None:
        return Check(
            "gh CLI auth (for `adapter contribute`)",
            False,
            "gh not on PATH — `adapter contribute` will print manual steps",
        )
    try:
        result = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        return Check("gh CLI auth", False, f"check failed: {exc}")
    if result.returncode != 0:
        return Check(
            "gh CLI auth",
            False,
            "`gh auth login` needed for `adapter contribute`",
        )
    return Check("gh CLI auth", True, "logged in")


def _version_agreement(paths: Paths) -> Check:
    """Best-effort: in dev checkouts we can compare marketplace.json + SKILL.md."""
    # In an installed wheel we can't see the repo files — just confirm package version.
    return Check(
        "Package version",
        True,
        f"job_hunter.__version__ = {__version__}",
    )


def collect(paths: Paths | None = None) -> list[Check]:
    paths = paths or resolve()
    checks: list[Check] = [
        _python_version(),
        _uv_installed(),
        _playwright_chromium(paths),
        *_xdg_dirs(paths),
        _secrets_perms(paths),
        _secrets_world_readable(paths),
        _profile_yaml(paths),
        _gh_auth(),
        _version_agreement(paths),
    ]
    return checks


def main() -> int:
    paths = resolve()
    checks = collect(paths)

    table = Table(title=f"job-hunter doctor — v{__version__}")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for c in checks:
        marker = "[green]OK[/green]" if c.ok else "[red]FAIL[/red]"
        table.add_row(c.name, marker, c.detail)
    console.print(table)

    failed = [c for c in checks if not c.ok]
    if failed:
        console.print(f"[red]{len(failed)} check(s) failed.[/red] See above.")
        return 1
    console.print("[green]All checks passed.[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
