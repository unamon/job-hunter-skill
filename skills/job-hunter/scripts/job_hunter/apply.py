"""Playwright form runner.

Two modes:
- shadow (default): fill, run pre-submit checks, BLOCK on stdin (y/N/edit),
  submit only on `y`. Auto-aborts as `aborted_for_review` if no TTY
  (`claude -p`, CI).
- auto: fill, run pre-submit checks, cooldown countdown, submit. Gated
  by 5 conditions; ANY fail → degrade to shadow.

This module is structured so the logic (gate eval, pre-submit checks,
plan execution) is testable without Playwright. The Playwright bits
live behind `_PlaywrightRunner` and are exercised only by integration
tests marked `playwright`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from .adapters import Adapter, FieldPlan, SecretResolver
from .adapters.loader import load_all, match_url
from .lint_secret_leaks import PATTERNS as PII_PATTERNS
from .models import FillMode, FillOutcome
from .paths import Paths

if TYPE_CHECKING:
    from .adapters.generators import GeneratorFn

console = Console()


# ─── auto-mode gating (pure logic, testable) ──────────────────────────────────


@dataclass(frozen=True)
class AutoGateInputs:
    adapter_auto_eligible: bool
    success_rate: float
    cli_mode_is_auto: bool
    i_understand: bool
    has_unapproved_generated_required: bool


@dataclass(frozen=True)
class AutoGateResult:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_auto_gates(inputs: AutoGateInputs) -> AutoGateResult:
    reasons: list[str] = []
    if not inputs.adapter_auto_eligible:
        reasons.append("adapter.auto_eligible=false")
    if inputs.success_rate < 0.9:
        reasons.append(f"success_rate={inputs.success_rate:.2f} < 0.9")
    if not inputs.cli_mode_is_auto:
        reasons.append("--mode auto not set")
    if not inputs.i_understand:
        reasons.append("--i-understand not set this session")
    if inputs.has_unapproved_generated_required:
        reasons.append("required generate.* field without prior approval")
    return AutoGateResult(allowed=not reasons, reasons=tuple(reasons))


# ─── pre-submit checks (testable without a browser) ──────────────────────────


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def check_no_pii_in_paths(paths: list[Path]) -> CheckResult:
    """Scan provided files for our PII regex set. Used after fill, before submit."""
    hits: list[str] = []
    for p in paths:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, pat in PII_PATTERNS.items():
            if pat.search(text):
                hits.append(f"{p}:{kind}")
                break
    if hits:
        return CheckResult(
            "assert_no_pii_in_logs", False, f"PII pattern matched in: {', '.join(hits)}"
        )
    return CheckResult("assert_no_pii_in_logs", True)


def check_all_required_filled(plan: FieldPlan) -> CheckResult:
    missing = plan.missing_required()
    if missing:
        labels = ", ".join(e.source_key for e in missing)
        return CheckResult(
            "assert_all_required_filled",
            False,
            f"missing required: {labels}",
        )
    return CheckResult("assert_all_required_filled", True)


def check_resume_matches_locale(plan: FieldPlan, locale_hint: str) -> CheckResult:
    """If the posting language is pt-BR, require file.resume_pt; en → file.resume_en."""
    expected = "resume_pt" if locale_hint.startswith("pt") else "resume_en"
    for e in plan.entries:
        if e.source_kind == "file" and e.source_key.startswith("resume"):
            if e.source_key != expected:
                return CheckResult(
                    "assert_resume_matches_locale",
                    False,
                    f"posting locale={locale_hint} but adapter uses {e.source_key}",
                )
            return CheckResult("assert_resume_matches_locale", True)
    # No resume field — that's fine.
    return CheckResult("assert_resume_matches_locale", True)


# ─── run-dir layout & report ────────────────────────────────────────────────


@dataclass
class FillReport:
    started_at: str
    finished_at: str = ""
    mode: str = "shadow"
    outcome: str = FillOutcome.PENDING.value
    fields_filled: int = 0
    fields_total: int = 0
    artifacts_path: str = ""
    checks: list[dict[str, str | bool]] = None  # type: ignore[assignment]
    reason: str | None = None
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.checks is None:
            self.checks = []
        if self.errors is None:
            self.errors = []


def new_apply_run_dir(paths: Paths, application_id: int) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = paths.runs_dir / f"{ts}-apply-{application_id:03d}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ─── shadow-mode TTY policy ─────────────────────────────────────────────────


def is_tty_available() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


# ─── auth-wall detection (Workday / Greenhouse "Create account" gates) ───────


AUTH_WALL_PHRASES = (
    "create account",
    "create an account",
    "criar conta",
    "candidate sign in",
    "candidate login",
    "applicant login",
    "candidate portal",
    "sign in to apply",
    "log in to apply",
    "entrar para se candidatar",
    "candidate self service",
)


def detect_auth_wall(*, url: str, body_text: str | None = None) -> str | None:
    """Heuristic: return a reason string if the page looks like an ATS auth wall.

    Conservative — we'd rather false-positive (flag for review) than auto-apply
    with half-filled fields that get rejected. Returns None when no wall detected.
    """
    lowered_url = url.lower()
    if "myworkdayjobs.com/wday/authgwy" in lowered_url:
        return "Workday tenant-specific candidate account required"
    for marker in ("/auth/login", "/login/?next=", "/candidate/login"):
        if marker in lowered_url:
            return "ATS login URL — applicant account required"
    if body_text:
        sample = body_text.lower()[:8000]
        for phrase in AUTH_WALL_PHRASES:
            if phrase in sample:
                return f"page contains '{phrase}' — applicant account likely required"
    return None


# ─── high-level orchestrator (no Playwright import in tests) ─────────────────


@dataclass
class ApplyInputs:
    paths: Paths
    application_id: int
    url: str
    locale_hint: str  # "pt-BR" | "en"
    mode: FillMode
    i_understand: bool
    adapter_overrides: list[Adapter] | None = None
    generators: dict[str, GeneratorFn] | None = None
    generate_inputs: dict[str, str] | None = None


@dataclass
class ApplyOutcome:
    outcome: FillOutcome
    report: FillReport
    plan: FieldPlan | None
    adapter: Adapter | None
    reason: str | None = None


def plan_for_url(
    inputs: ApplyInputs,
) -> tuple[Adapter | None, FieldPlan | None, str | None]:
    """Resolve adapter for url + build a plan. No browser involvement.

    Returns (adapter, plan, error_message). On no-match, returns (None, None, msg).
    """
    # Defense in depth: Firecrawl is forbidden in the apply path because form
    # values include PII. See references/firecrawl.md.
    from .firecrawl_client import assert_apply_path_safe

    assert_apply_path_safe()

    adapters = inputs.adapter_overrides or load_all(inputs.paths)
    adapter = match_url(inputs.url, adapters)
    if adapter is None:
        return None, None, f"no adapter matches URL {inputs.url}"
    resolver = SecretResolver(inputs.paths, generators=inputs.generators)
    plan = resolver.build_plan(adapter, inputs.generate_inputs or _public_inputs(inputs.url))
    return adapter, plan, None


def _public_inputs(url: str) -> dict[str, str]:
    """Default public context for generators. Caller normally overrides."""
    return {
        "job_title": "the role",
        "company": _company_from_url(url),
        "role_summary": "",
        "public_profile_blurb": "",
    }


def _company_from_url(url: str) -> str:
    # Best-effort: nubank.gupy.io -> "nubank"; boards.greenhouse.io/foo -> "foo"
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host.endswith(".gupy.io"):
        return host.split(".gupy.io")[0]
    if host == "boards.greenhouse.io" and parsed.path:
        return parsed.path.strip("/").split("/")[0]
    return host or "the company"


def run_pre_submit_checks(
    plan: FieldPlan,
    adapter: Adapter,
    *,
    locale_hint: str,
    run_dir: Path,
    log_paths: list[Path],
) -> list[CheckResult]:
    """Run each check in adapter.submit.pre_submit_checks. Returns results list."""
    results: list[CheckResult] = []
    for name in adapter.submit.pre_submit_checks:
        if name == "assert_all_required_filled":
            results.append(check_all_required_filled(plan))
        elif name == "assert_no_pii_in_logs":
            results.append(check_no_pii_in_paths(log_paths))
        elif name == "assert_resume_matches_locale":
            results.append(check_resume_matches_locale(plan, locale_hint))
        elif name == "screenshot":
            # Placeholder: actual screenshot taken by Playwright runner.
            screenshot = run_dir / "before_submit.png"
            results.append(CheckResult("screenshot", screenshot.exists(), str(screenshot)))
        else:
            results.append(CheckResult(name, True, "unknown check; treated as ok"))
    return results


# ─── confirm prompt (shadow mode) ────────────────────────────────────────────


def confirm_submit_blocking(label: str) -> str:
    """Read one of {y,N,edit} from stdin. Returns lowercased response or '' on EOF."""
    if not is_tty_available():
        return ""
    while True:
        raw = input(f"[{label}] Form ready. Review browser. Submit? (y/N/edit): ")
        choice = raw.strip().lower()
        if choice in {"y", "yes", "n", "no", "edit", ""}:
            return choice or "n"


# ─── browser bits, optional import ───────────────────────────────────────────


def has_playwright() -> bool:
    # find_spec works when only the package (not the CLI shim) is installed.
    return importlib.util.find_spec("playwright") is not None


def cooldown(seconds: float, *, on_tick: Callable[[int], None] | None = None) -> bool:
    """Block for `seconds` with optional per-second callback. Returns False on Ctrl+C."""
    end = time.monotonic() + seconds
    try:
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            if on_tick is not None:
                on_tick(int(remaining))
            time.sleep(min(1.0, remaining))
    except KeyboardInterrupt:
        return False


__all__ = [
    "AutoGateInputs",
    "AutoGateResult",
    "ApplyInputs",
    "ApplyOutcome",
    "CheckResult",
    "FillReport",
    "check_all_required_filled",
    "check_no_pii_in_paths",
    "check_resume_matches_locale",
    "confirm_submit_blocking",
    "cooldown",
    "evaluate_auto_gates",
    "is_tty_available",
    "new_apply_run_dir",
    "plan_for_url",
    "run_pre_submit_checks",
]


def _unused_check(_: object) -> None:
    """Keep type-checker happy with optional imports."""
    if os.environ.get("UNUSED"):  # noqa: SIM102
        _ = console
