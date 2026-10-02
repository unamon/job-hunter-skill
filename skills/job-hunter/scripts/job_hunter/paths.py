"""XDG path resolution.

Resolution order:
1. If `JOB_HUNTER_HOME_OVERRIDE` is set (used by tests), all three roots
   live under `${override}/{config,data,state}/job-hunter/`.
2. Otherwise, honor `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME`.
3. Otherwise, platformdirs defaults (Linux: `~/.config`, `~/.local/share`,
   `~/.local/state`; Windows: all three are `%LOCALAPPDATA%\\job-hunter`).

The override exists so tests can run with a clean filesystem under `tmp_path`
without touching the developer's real config dir. Never use it in prod.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "job-hunter"

_OVERRIDE_ENV = "JOB_HUNTER_HOME_OVERRIDE"


@dataclass(frozen=True)
class Paths:
    """Resolved runtime paths. All `.ensure()` calls are idempotent."""

    config_dir: Path
    data_dir: Path
    state_dir: Path

    @property
    def secrets_dir(self) -> Path:
        return self.config_dir / "secrets"

    @property
    def secrets_env(self) -> Path:
        return self.secrets_dir / "personal.env"

    @property
    def profile_yaml(self) -> Path:
        return self.config_dir / "profile.yaml"

    @property
    def config_yaml(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def field_labels_override(self) -> Path:
        return self.config_dir / "field_labels.yaml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def tracking_index(self) -> Path:
        return self.data_dir / "tracking.md"

    @property
    def tracking_dir(self) -> Path:
        return self.data_dir / "tracking"

    @property
    def adapters_inbox(self) -> Path:
        return self.data_dir / "adapters_inbox"

    @property
    def adapters_user(self) -> Path:
        return self.data_dir / "adapters_user"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def ratelimit_json(self) -> Path:
        return self.logs_dir / "ratelimit.json"

    @property
    def chrome_profile_linkedin(self) -> Path:
        """Persistent Chromium profile for LinkedIn — same fingerprint across
        runs so LinkedIn doesn't invalidate the session.
        """
        return self.data_dir / "chrome-profiles" / "linkedin"

    def ensure(self) -> None:
        """Create every directory we expect to exist. Idempotent."""
        for d in (
            self.config_dir,
            self.secrets_dir,
            self.data_dir,
            self.tracking_dir,
            self.adapters_inbox,
            self.adapters_user,
            self.files_dir,
            self.runs_dir,
            self.state_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _override_root() -> Path | None:
    val = os.environ.get(_OVERRIDE_ENV)
    return Path(val).expanduser() if val else None


def resolve() -> Paths:
    """Resolve the runtime paths. NOT cached — env vars can change between tests."""
    override = _override_root()
    if override is not None:
        return Paths(
            config_dir=override / "config" / APP_NAME,
            data_dir=override / "data" / APP_NAME,
            state_dir=override / "state" / APP_NAME,
        )

    # platformdirs only reads XDG_* on Linux; check them explicitly so the
    # documented override also works on Windows/macOS.
    dirs = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=False)
    return Paths(
        config_dir=_xdg_or("XDG_CONFIG_HOME", dirs.user_config_dir),
        data_dir=_xdg_or("XDG_DATA_HOME", dirs.user_data_dir),
        state_dir=_xdg_or("XDG_STATE_HOME", dirs.user_state_dir),
    )


def _xdg_or(var: str, default: str) -> Path:
    val = os.environ.get(var)
    return Path(val).expanduser() / APP_NAME if val else Path(default)


@lru_cache(maxsize=1)
def cached() -> Paths:
    """Cached version for normal runs. Tests should use `resolve()` directly."""
    return resolve()


def clear_cache() -> None:
    cached.cache_clear()
