"""Live browser fill for `job apply` (shadow mode).

`run_shadow` walks the adapter fields on an already-open page, screenshots
with `secret.*` inputs masked, runs the pre-submit checks, then blocks on the
terminal for y/N/edit. Resolved values go straight from the resolver into the
page; only selectors and source references are printed or written to
`report.json`.

`launch_and_run` owns the Playwright lifecycle. Tests drive `run_shadow` with
a fake page, so nothing here imports Playwright at module load.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from .adapters import Adapter, FieldPlan, SecretResolver
from .adapters.resolver import _split_source
from .apply import (
    CheckResult,
    FillReport,
    confirm_submit_blocking,
    detect_auth_wall,
    run_pre_submit_checks,
)
from .models import FillOutcome
from .paths import Paths

console = Console()

FILL_TIMEOUT_MS = 5_000
FORM_TIMEOUT_MS = 20_000
SUBMIT_SETTLE_MS = 15_000
UPLOAD_SETTLE_MS = 2_000
TEXT_SUFFIXES = (".txt", ".md")


@dataclass(frozen=True)
class FieldResult:
    selector: str
    source: str
    status: str  # filled | skipped | missing | error
    detail: str = ""


def _text_file(paths: Paths, key: str) -> Path | None:
    for suffix in TEXT_SUFFIXES:
        candidate = paths.files_dir / f"{key}{suffix}"
        if candidate.exists():
            return candidate
    return None


def fill_fields(
    page: Any,
    adapter: Adapter,
    resolver: SecretResolver,
    generate_inputs: dict[str, str],
) -> list[FieldResult]:
    """Fill every adapter field on `page`. Never raises per field."""
    results: list[FieldResult] = []
    for f in adapter.fields:
        kind, key = _split_source(f.source)
        loc = page.locator(f.selector).first
        try:
            if loc.count() == 0:
                results.append(FieldResult(f.selector, f.source, "missing", "not on page"))
                continue
            if kind == "file":
                is_upload = loc.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'")
                if is_upload:
                    path = resolver.get_file_path(key)
                    if path is None:
                        results.append(FieldResult(f.selector, f.source, "skipped", "no file"))
                        continue
                    loc.set_input_files(str(path), timeout=FILL_TIMEOUT_MS)
                    # ATS widgets validate/upload asynchronously; let that land
                    # before the screenshot.
                    page.wait_for_timeout(UPLOAD_SETTLE_MS)
                    results.append(FieldResult(f.selector, f.source, "filled", path.name))
                    continue
                # Text target (e.g. a cover-letter textarea): paste the .txt/.md variant.
                text_path = _text_file(resolver.paths, key)
                if text_path is None:
                    results.append(FieldResult(f.selector, f.source, "skipped", "no text file"))
                    continue
                loc.fill(text_path.read_text(encoding="utf-8").strip(), timeout=FILL_TIMEOUT_MS)
                results.append(FieldResult(f.selector, f.source, "filled", text_path.name))
                continue
            value = resolver.get_value(f, generate_inputs)
            if not value:
                results.append(FieldResult(f.selector, f.source, "skipped", "no value"))
                continue
            loc.fill(str(value), timeout=FILL_TIMEOUT_MS)
            results.append(FieldResult(f.selector, f.source, "filled"))
        except Exception as e:  # noqa: BLE001 — report and keep going
            # Type name only: some driver errors echo the value being typed.
            results.append(FieldResult(f.selector, f.source, "error", type(e).__name__))
    return results


def check_required_on_page(adapter: Adapter, results: list[FieldResult]) -> CheckResult:
    by_selector = {r.selector: r for r in results}
    unfilled = [
        f.selector
        for f in adapter.required_fields()
        if by_selector.get(f.selector) is None or by_selector[f.selector].status != "filled"
    ]
    if unfilled:
        return CheckResult("required_filled_on_page", False, ", ".join(unfilled))
    return CheckResult("required_filled_on_page", True)


# Playwright's `mask=` boxes are computed before a full-page capture re-lays
# the page out, so they can land on the wrong element. Hide the text in the
# DOM instead, for exactly as long as the capture takes.
_HIDE_JS = """e => {
  e.dataset.jhColor = e.style.color;
  e.dataset.jhFill = e.style.webkitTextFillColor;
  e.style.color = 'transparent';
  e.style.webkitTextFillColor = 'transparent';
}"""
_SHOW_JS = """e => {
  e.style.color = e.dataset.jhColor || '';
  e.style.webkitTextFillColor = e.dataset.jhFill || '';
  delete e.dataset.jhColor;
  delete e.dataset.jhFill;
}"""


def screenshot_masked(page: Any, adapter: Adapter, path: Path) -> None:
    """Full-page screenshot with every `secret.*` field's text hidden."""
    secret_locs = [
        page.locator(f.selector).first
        for f in adapter.fields
        if _split_source(f.source)[0] == "secret"
    ]
    hidden = []
    for loc in secret_locs:
        with contextlib.suppress(Exception):
            if loc.count():
                loc.evaluate(_HIDE_JS)
                hidden.append(loc)
    try:
        page.screenshot(path=str(path), full_page=True)
    finally:
        for loc in hidden:
            with contextlib.suppress(Exception):
                loc.evaluate(_SHOW_JS)


def _print_results(results: list[FieldResult], checks: list[CheckResult]) -> None:
    style = {"filled": "green", "skipped": "dim", "missing": "yellow", "error": "red"}
    for r in results:
        s = style.get(r.status, "white")
        detail = f" ({escape(r.detail)})" if r.detail else ""
        console.print(f"  [{s}]{r.status:8}[/{s}] {escape(r.selector)} ← {r.source}{detail}")
    for c in checks:
        mark = "[green]ok[/green]  " if c.ok else "[red]FAIL[/red]"
        detail = f" — {escape(c.detail)}" if c.detail and not c.ok else ""
        console.print(f"  {mark} {c.name}{detail}")


def _ask_retry() -> str:
    try:
        return input("Press Enter to retry Submit, or type n to stop: ").strip().lower()
    except EOFError:
        return "n"


def click_submit(
    page: Any, adapter: Adapter, *, retry_prompt: Callable[[], str] | None = None
) -> bool:
    """Click submit; if something covers it (cookie banner, modal), let the user
    clear it and retry instead of crashing with the filled form lost."""
    ask = retry_prompt or _ask_retry
    while True:
        try:
            page.locator(adapter.submit.selector).first.click(timeout=FILL_TIMEOUT_MS)
            return True
        except Exception as e:  # noqa: BLE001 — Playwright raises TimeoutError here
            console.print(
                f"[yellow]Couldn't click Submit ({type(e).__name__})[/yellow] — something "
                "is covering it, often a cookie banner. Clear it in the browser."
            )
            if ask() in {"n", "no"}:
                return False


def write_report(run_dir: Path, report: FillReport) -> Path:
    out = run_dir / "report.json"
    out.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    return out


def run_shadow(
    page: Any,
    adapter: Adapter,
    plan: FieldPlan,
    resolver: SecretResolver,
    *,
    generate_inputs: dict[str, str],
    run_dir: Path,
    locale_hint: str,
    label: str,
    log_paths: list[Path],
    prompt: Callable[[str], str] = confirm_submit_blocking,
    retry_prompt: Callable[[], str] | None = None,
) -> FillReport:
    """Fill, check, ask. Clicks submit only on an explicit `y`."""
    report = FillReport(
        started_at=datetime.now(UTC).isoformat(),
        mode="shadow",
        artifacts_path=str(run_dir),
        fields_total=len(adapter.fields),
    )
    results = fill_fields(page, adapter, resolver, generate_inputs)
    report.fields_filled = sum(r.status == "filled" for r in results)
    report.errors = [
        f"{r.selector}: {r.status} {r.detail}".strip()
        for r in results
        if r.status in {"missing", "error"}
    ]

    screenshot_masked(page, adapter, run_dir / "before_submit.png")
    checks = run_pre_submit_checks(
        plan, adapter, locale_hint=locale_hint, run_dir=run_dir, log_paths=log_paths
    )
    checks.append(check_required_on_page(adapter, results))
    report.checks = [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]

    console.print(
        f"[bold]{escape(label)}[/bold] — {report.fields_filled}/{report.fields_total} fields filled"
    )
    _print_results(results, checks)
    if not all(c.ok for c in checks):
        console.print(
            "[yellow]Some checks failed — fix them in the browser before saying y.[/yellow]"
        )

    while True:
        answer = prompt(label)
        if answer == "edit":
            console.print("Edit the form in the browser window, then answer again.")
            continue
        break

    if answer in {"y", "yes"}:
        if click_submit(page, adapter, retry_prompt=retry_prompt):
            # SPAs may never go idle; screenshot whatever is there after the wait.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=SUBMIT_SETTLE_MS)
            screenshot_masked(page, adapter, run_dir / "after_submit.png")
            report.outcome = FillOutcome.SUBMITTED.value
        else:
            report.outcome = FillOutcome.ABORTED_FOR_REVIEW.value
            report.reason = "submit_not_clickable"
    else:
        report.outcome = FillOutcome.ABORTED_FOR_REVIEW.value
        report.reason = "no_tty" if answer == "" else "user_declined"

    report.finished_at = datetime.now(UTC).isoformat()
    write_report(run_dir, report)
    return report


def launch_and_run(
    url: str,
    adapter: Adapter,
    plan: FieldPlan,
    resolver: SecretResolver,
    *,
    generate_inputs: dict[str, str],
    run_dir: Path,
    locale_hint: str,
    label: str,
    log_paths: list[Path],
) -> FillReport:
    """Open a headed Chromium (or `BROWSER_WS_ENDPOINT`) and run shadow fill."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ws = os.environ.get("BROWSER_WS_ENDPOINT")
        browser = p.chromium.connect(ws) if ws else p.chromium.launch(headless=False)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            for marker in adapter.match.dom_markers:
                page.wait_for_selector(marker, timeout=FORM_TIMEOUT_MS)
            wall = detect_auth_wall(url=page.url, body_text=page.inner_text("body"))
            if wall:
                console.print(f"[yellow]heads-up[/yellow]: {escape(wall)}")
            try:
                report = run_shadow(
                    page,
                    adapter,
                    plan,
                    resolver,
                    generate_inputs=generate_inputs,
                    run_dir=run_dir,
                    locale_hint=locale_hint,
                    label=label,
                    log_paths=log_paths,
                )
            except Exception as e:
                # Don't throw away a filled form: keep the window until the user is done.
                console.print(
                    f"[red]fill failed[/red]: {type(e).__name__}. Browser left open — "
                    "you can finish by hand, then run `job stage <id> --to applied`."
                )
                with contextlib.suppress(EOFError):
                    input("Press Enter to close the browser... ")
                raise
            if report.outcome != FillOutcome.SUBMITTED.value and report.reason != "no_tty":
                console.print(
                    "Browser left open — you can finish by hand, then run "
                    "`job stage <id> --to applied`."
                )
                input("Press Enter to close the browser... ")
            return report
        finally:
            browser.close()


__all__ = [
    "FieldResult",
    "check_required_on_page",
    "fill_fields",
    "launch_and_run",
    "run_shadow",
    "write_report",
]
