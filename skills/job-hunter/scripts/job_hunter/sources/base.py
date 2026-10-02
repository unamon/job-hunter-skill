"""Source protocol + shared utilities (rate limit, run reports, slug)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import traceback
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from ..paths import Paths


class SourceError(Exception):
    """Raised when a source cannot proceed (auth failure, schema drift, etc.)."""


@dataclass
class SearchQuery:
    roles: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    max_pages: int = 5

    def matches_role(self, text: str) -> bool:
        """Cheap keyword filter — returns True if any role token appears in text."""
        if not self.roles:
            return True
        lower = text.lower()
        if any(bad.lower() in lower for bad in self.exclude_keywords):
            return False
        return any(r.lower() in lower for r in self.roles)


@dataclass
class JobPosting:
    source: str
    external_id: str
    url: str
    title: str
    company: str
    location: str | None = None
    remote: bool | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None
    posted_at: datetime | None = None
    description: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    salary_period: str | None = None  # "hour" | "day" | "month" | "year" | None

    def fingerprint(self) -> str:
        snippet = (self.description or "")[:500]
        material = f"{self.company}::{self.title}::{snippet}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class RateLimitConfig:
    domain: str
    min_seconds: float
    max_seconds: float


@runtime_checkable
class Source(Protocol):
    """Sources implement async discover() + fetch_detail().

    `discover` is an async generator. The Protocol describes its CALL signature,
    not its definition style — `def` here, even though implementations use
    `async def ... yield ...`. Mypy accepts the match because async-gen funcs'
    runtime return type is AsyncGenerator (which is an AsyncIterator).
    """

    name: str
    base_url: str
    rate_limit: RateLimitConfig | None

    def discover(
        self, query: SearchQuery, client: httpx.AsyncClient
    ) -> AsyncIterator[JobPosting]: ...

    async def fetch_detail(self, posting: JobPosting, client: httpx.AsyncClient) -> JobPosting:
        """Optionally hydrate a posting with full description. Default = no-op."""
        ...


# ─── HTTP helpers ────────────────────────────────────────────────────────────

_DEFAULT_UAS = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
)


def random_ua() -> str:
    return random.choice(_DEFAULT_UAS)


def default_client(
    timeout: float = 30.0, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    base_headers = {"User-Agent": random_ua(), "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8"}
    if headers:
        base_headers.update(headers)
    return httpx.AsyncClient(timeout=timeout, headers=base_headers, follow_redirects=True)


# ─── shared rate limiter (file-backed token bucket per domain) ────────────────


class RateLimiter:
    """Persisted last-request timestamps. Concurrent terminals share the budget.

    File: $XDG_STATE_HOME/job-hunter/logs/ratelimit.json
    Shape: {"linkedin.com": {"last_at": 17151..., "min": 12, "max": 25}, ...}
    """

    def __init__(self, paths: Paths) -> None:
        self.path = paths.ratelimit_json
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict[str, float]]:
        if not self.path.exists():
            return {}
        try:
            data: dict[str, dict[str, float]] = json.loads(self.path.read_text(encoding="utf-8"))
            return data
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, state: dict[str, dict[str, float]]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    async def wait(self, cfg: RateLimitConfig) -> None:
        state = self._load()
        bucket = state.get(cfg.domain, {})
        last = bucket.get("last_at", 0.0)
        now = time.time()
        gap = random.uniform(cfg.min_seconds, cfg.max_seconds)
        elapsed = now - last
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)
        state[cfg.domain] = {
            "last_at": time.time(),
            "min": cfg.min_seconds,
            "max": cfg.max_seconds,
        }
        self._save(state)


# ─── run reports ─────────────────────────────────────────────────────────────


@dataclass
class DiscoveryReport:
    source: str
    started_at: str
    finished_at: str = ""
    discovered: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def record_error(self, where: str, exc: BaseException) -> None:
        self.failed += 1
        self.errors.append(
            {
                "where": where,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(traceback.format_exception(exc)),
            }
        )


def new_run_dir(paths: Paths, source: str) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = paths.runs_dir / f"{ts}-{source}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_report(run_dir: Path, report: DiscoveryReport) -> None:
    (run_dir / "report.json").write_text(
        json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8"
    )
