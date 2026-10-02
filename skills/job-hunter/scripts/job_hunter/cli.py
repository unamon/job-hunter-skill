"""Typer CLI entrypoint."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlmodel import Session, select

from . import __version__, healthcheck, tracking_md
from .adapters import Adapter, FieldPlan, SecretResolver
from .adapters.generators import DEFAULTS as GENERATORS
from .adapters.loader import list_bundled, list_user, load_adapter, load_all
from .apply import (
    ApplyInputs,
    FillReport,
    has_playwright,
    is_tty_available,
    new_apply_run_dir,
    plan_for_url,
)
from .db import get_engine, run_migrations, session
from .discover import load_query, run_discover
from .models import (
    ACTIVE_STAGES,
    TERMINAL_STAGES,
    Application,
    FillAttempt,
    FillMode,
    FillOutcome,
    Job,
    SiteAdapter,
    Stage,
    StageHistory,
)
from .paths import Paths, resolve
from .salary import aggregate as salary_aggregate
from .salary import suggest_expectation
from .sources import REGISTRY, SourceError, get_source

app = typer.Typer(
    name="job-hunter",
    help="Discover, track, and assist with tech job applications matching your profile.yaml.",
)
console = Console()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        console.print(f"job-hunter {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


@app.command()
def info() -> None:
    """Print build info."""
    console.print(f"[bold]job-hunter[/bold] {__version__}")
    paths = resolve()
    console.print(f"Config: {paths.config_dir}", soft_wrap=True)
    console.print(f"Data:   {paths.data_dir}", soft_wrap=True)
    console.print(f"State:  {paths.state_dir}", soft_wrap=True)


@app.command()
def init() -> None:
    """Idempotent setup: create XDG dirs, copy templates, run migrations.

    Safe to re-run after upgrades — it never overwrites your edits.
    """
    paths = resolve()
    _bootstrap(paths)
    applied = run_migrations(paths)
    if applied:
        console.print(f"Applied migrations: {', '.join(applied)}")
    else:
        console.print("Migrations: up to date.")

    rc = healthcheck.main()
    if rc != 0:
        console.print(
            "[yellow]Doctor reported failures. " "Fix them before running other commands.[/yellow]"
        )
        raise typer.Exit(rc)


@app.command()
def sync(
    no_md_sync: bool = typer.Option(False, "--no-md-sync", help="Skip markdown regeneration."),
) -> None:
    """Regenerate `tracking.md` and per-job files from the DB."""
    if no_md_sync:
        console.print("Skipping md sync (per --no-md-sync).")
        return
    paths = resolve()
    paths.ensure()
    run_migrations(paths)

    now = tracking_md.fixed_now_from_env() or datetime.now(tracking_md.DEFAULT_TZ)
    with session(paths) as sess:
        tracking_md.regenerate(paths, sess, now=now)
    console.print(f"Synced tracking.md and per-job files to {paths.tracking_dir}")


@app.command()
def discover(
    source: str = typer.Option(..., "--source", help="Source name (see REGISTRY in sources/)."),
    query_override: str | None = typer.Option(
        None, "--query", help="Comma-separated role keywords overriding profile.yaml."
    ),
    no_md_sync: bool = typer.Option(
        False, "--no-md-sync", help="Skip markdown regen until end of batch."
    ),
) -> None:
    """Discover postings from a source. Upserts jobs, creates Application rows."""
    paths = resolve()
    paths.ensure()
    run_migrations(paths)

    if source not in REGISTRY:
        console.print(f"[red]unknown source[/red]: {source}")
        console.print(f"available: {', '.join(sorted(REGISTRY))}")
        raise typer.Exit(2)

    src = get_source(source)
    query = load_query(paths)
    if query_override:
        query.roles = [tok.strip() for tok in query_override.split(",") if tok.strip()]

    # Load PII env (e.g. LINKEDIN_LI_AT) into this process. Never print contents.
    if paths.secrets_env.exists():
        load_dotenv(paths.secrets_env, override=False)

    try:
        with session(paths) as sess:
            report, new_ids = asyncio.run(run_discover(src, query, paths, sess))
    except SourceError as e:
        console.print(f"[red]source error[/red]: {e}")
        raise typer.Exit(1) from e

    console.print(
        f"[bold]{source}[/bold]: discovered={report.discovered} new={report.new} "
        f"updated={report.updated} failed={report.failed}"
    )
    if report.errors:
        for err in report.errors[:5]:
            console.print(f"  [yellow]err[/yellow] {err['where']}: {err['message']}")
        if len(report.errors) > 5:
            console.print(f"  ...and {len(report.errors) - 5} more (see run report)")

    if not no_md_sync:
        now = tracking_md.fixed_now_from_env() or datetime.now(tracking_md.DEFAULT_TZ)
        with session(paths) as sess:
            tracking_md.regenerate(paths, sess, now=now)


@app.command(name="list")
def list_cmd(
    stage: str | None = typer.Option(None, "--stage", help="Filter by current stage."),
    source: str | None = typer.Option(None, "--source", help="Filter by source name."),
    since: str | None = typer.Option(
        None, "--since", help="ISO date (YYYY-MM-DD) — scraped on/after."
    ),
) -> None:
    """List applications. Prints a table sorted by application id ascending."""
    paths = resolve()
    paths.ensure()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        stmt = select(Application, Job).join(Job)
        if stage:
            stmt = stmt.where(Application.current_stage == stage)
        if source:
            stmt = stmt.where(Job.source == source)
        if since:
            try:
                cutoff = datetime.fromisoformat(since)
            except ValueError as e:
                console.print(f"[red]invalid --since[/red]: {e}")
                raise typer.Exit(2) from e
            stmt = stmt.where(Job.scraped_at >= cutoff.replace(tzinfo=None))
        rows = list(sess.exec(stmt).all())

    table = Table(title=f"job-hunter list ({len(rows)} rows)")
    for col in ("ID", "Company", "Title", "Stage", "Source"):
        table.add_column(col)
    for app, job in rows:
        table.add_row(f"{app.id or 0:03d}", job.company, job.title, app.current_stage, job.source)
    console.print(table)


@app.command(name="show")
def show_cmd(id: int = typer.Argument(..., help="Application id.")) -> None:
    """Print a single application's detail."""
    paths = resolve()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        app = sess.get(Application, id)
        if app is None:
            console.print(f"[red]not found[/red]: application {id}")
            raise typer.Exit(1)
        job = sess.get(Job, app.job_id)
        console.print(f"[bold]Application {app.id:03d}[/bold]")
        console.print(f"  stage   : {app.current_stage}")
        if job:
            console.print(f"  company : {job.company}")
            console.print(f"  title   : {job.title}")
            console.print(f"  source  : {job.source}")
            console.print(f"  url     : {job.url}")
            if job.location:
                console.print(f"  location: {job.location}")
        if app.next_action:
            console.print(f"  next    : {app.next_action} (due {app.next_action_due})")
        if app.adapter_used:
            console.print(f"  adapter : {app.adapter_used}")

        history = list(
            sess.exec(
                select(StageHistory)
                .where(StageHistory.application_id == id)
                .order_by(StageHistory.transitioned_at)  # type: ignore[arg-type]
            ).all()
        )
        if history:
            console.print("\n[bold]Stage history[/bold]")
            for h in history:
                console.print(
                    f"  {h.transitioned_at.isoformat(timespec='seconds')}: "
                    f"{h.from_stage or '—'} → {h.to_stage}"
                )


@app.command(name="queue")
def queue_cmd(id: int = typer.Argument(...)) -> None:
    """Transition discovered → queued."""
    _transition(id, Stage.QUEUED, note="queued via CLI")


@app.command(name="stage")
def stage_cmd(
    id: int = typer.Argument(...),
    to: str = typer.Option(..., "--to", help="Target stage."),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Force a stage transition."""
    try:
        target = Stage(to)
    except ValueError as e:
        allowed = ", ".join(s.value for s in Stage)
        console.print(f"[red]unknown stage[/red]: {to}. Allowed: {allowed}")
        raise typer.Exit(2) from e
    _transition(id, target, note=note)


def _transition(id: int, to: Stage, note: str | None) -> None:
    paths = resolve()
    paths.ensure()
    run_migrations(paths)
    eng = get_engine(paths)
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(eng) as sess:
        app = sess.get(Application, id)
        if app is None:
            console.print(f"[red]not found[/red]: application {id}")
            raise typer.Exit(1)
        prev = app.current_stage
        if prev == to.value:
            console.print(f"already in stage {to.value}; no-op")
            return
        app.current_stage = to.value
        app.updated_at = now
        if to.value == Stage.APPLIED.value and app.applied_at is None:
            app.applied_at = now
        sess.add(app)
        sess.add(
            StageHistory(
                application_id=id,
                from_stage=prev,
                to_stage=to.value,
                transitioned_at=now,
                note=note,
            )
        )
        sess.commit()

    fixed_now = tracking_md.fixed_now_from_env() or datetime.now(tracking_md.DEFAULT_TZ)
    with session(paths) as sess:
        tracking_md.regenerate(paths, sess, now=fixed_now)
    console.print(f"Application {id:03d}: {prev} → {to.value}")


@app.command(name="apply")
def apply_cmd(
    id: int = typer.Argument(..., help="Application id."),
    mode: str = typer.Option("shadow", "--mode", help="shadow | auto | dry_run"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Resolve plan, print, don't open a browser."
    ),
    i_understand: bool = typer.Option(False, "--i-understand", help="Required for auto mode."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Skip cooldown countdown in auto."),
) -> None:
    """Open the application URL and run the form-fill pipeline.

    Phase 5: shadow/auto logic + plan are testable. Live browser invocation
    lands fully in a follow-up; for now `--dry-run` walks the entire plan
    without opening Chromium.
    """
    paths = resolve()
    paths.ensure()
    run_migrations(paths)
    if paths.secrets_env.exists():
        load_dotenv(paths.secrets_env, override=False)

    try:
        fill_mode = FillMode(mode)
    except ValueError as e:
        console.print(f"[red]unknown --mode[/red]: {mode}")
        raise typer.Exit(2) from e

    eng = get_engine(paths)
    with Session(eng) as sess:
        app = sess.get(Application, id)
        if app is None:
            console.print(f"[red]not found[/red]: application {id}")
            raise typer.Exit(1)
        job = sess.get(Job, app.job_id)
        if job is None:
            console.print(f"[red]inconsistent state[/red]: application {id} has no job row")
            raise typer.Exit(1)
        url = job.url
        company = job.company
        title = job.title

    inputs = ApplyInputs(
        paths=paths,
        application_id=id,
        url=url,
        locale_hint="pt-BR" if any(c in url for c in (".com.br", ".gupy.io")) else "en",
        mode=fill_mode,
        i_understand=i_understand,
    )
    adapter, plan, err = plan_for_url(inputs)
    if err is not None or adapter is None or plan is None:
        console.print(f"[yellow]no adapter[/yellow]: {err}")
        console.print(
            "Run `job adapter list` to see what's installed, or `job adapter test "
            "<sig> --url ...` to dry-run a learned draft."
        )
        raise typer.Exit(1)

    console.print(f"[bold]Application {id:03d}[/bold]: {company} — {title}")
    console.print(f"  url     : {url}")
    console.print(f"  adapter : {adapter.platform_signature} (v{adapter.version})")
    console.print(f"  mode    : {fill_mode.value}")

    table = Table(title="Field plan")
    for col in ("Selector", "Source", "Required", "Has value"):
        table.add_column(col)
    for entry in plan.entries:
        marker = "[green]yes[/green]" if entry.has_value else "[red]no[/red]"
        table.add_row(
            escape(entry.selector),
            f"{entry.source_kind}.{entry.source_key}",
            "yes" if entry.required else "no",
            marker,
        )
    console.print(table)

    missing = plan.missing_required()
    if missing:
        console.print(
            f"[yellow]{len(missing)} required field(s) missing[/yellow]. "
            "Fix profile.yaml / secrets / files before live mode."
        )

    if dry_run:
        console.print("[dim]--dry-run: skipping browser.[/dim]")
        return

    if fill_mode == FillMode.DRY_RUN:
        return
    if fill_mode == FillMode.AUTO:
        # No bundled adapter is auto_eligible yet, so the gates always degrade.
        console.print("[yellow]auto mode gates not met[/yellow]; degrading to shadow.")
        fill_mode = FillMode.SHADOW
    if not is_tty_available():
        console.print(
            "[yellow]shadow mode needs a TTY[/yellow]; in headless contexts the run "
            "would auto-abort to `aborted_for_review`. Use `--mode dry_run` to preview."
        )
        raise typer.Exit(1)
    if not has_playwright():
        console.print("[red]playwright not installed[/red]; run `job doctor`.")
        raise typer.Exit(1)

    _run_live_fill(
        paths, id, url=url, company=company, title=title, inputs=inputs, adapter=adapter, plan=plan
    )


def _run_live_fill(
    paths: Paths,
    id: int,
    *,
    url: str,
    company: str,
    title: str,
    inputs: ApplyInputs,
    adapter: Adapter,
    plan: FieldPlan,
) -> None:
    from .apply_live import launch_and_run, write_report

    resolver = SecretResolver(paths, generators=GENERATORS)
    generate_inputs = {
        "job_title": title,
        "company": company,
        "role_summary": "",
        "public_profile_blurb": str(resolver._profile_value("public_profile_blurb") or ""),
    }
    run_dir = new_apply_run_dir(paths, id)
    started = datetime.now(UTC)
    label = f"{id:03d} {company}"
    try:
        report = launch_and_run(
            url,
            adapter,
            plan,
            resolver,
            generate_inputs=generate_inputs,
            run_dir=run_dir,
            locale_hint=inputs.locale_hint,
            label=label,
            log_paths=sorted(paths.logs_dir.glob("*.log")) if paths.logs_dir.exists() else [],
        )
    except Exception as e:  # noqa: BLE001 — record the failed attempt, then surface it
        report = FillReport(
            started_at=started.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            outcome=FillOutcome.FAILED.value,
            artifacts_path=str(run_dir),
            fields_total=len(adapter.fields),
            reason=type(e).__name__,
        )
        write_report(run_dir, report)
        console.print(f"[red]fill failed[/red]: {type(e).__name__}. Artifacts: {run_dir}")

    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(get_engine(paths)) as sess:
        sess.add(
            FillAttempt(
                application_id=id,
                started_at=started.replace(tzinfo=None),
                finished_at=now,
                outcome=report.outcome,
                artifacts_path=str(run_dir),
                fields_filled=report.fields_filled,
                fields_total=report.fields_total,
                mode=FillMode.SHADOW.value,
            )
        )
        app_row = sess.get(Application, id)
        if app_row is not None:
            app_row.adapter_used = adapter.platform_signature
            sess.add(app_row)
        sess.commit()

    console.print(f"outcome: [bold]{report.outcome}[/bold]  (artifacts: {run_dir})")
    if report.outcome == FillOutcome.SUBMITTED.value:
        _transition(id, Stage.APPLIED, note=f"submitted via {adapter.platform_signature} (shadow)")
    elif report.outcome == FillOutcome.FAILED.value:
        raise typer.Exit(1)


@app.command(name="approve")
def approve_cmd(
    fill_attempt_id: int = typer.Argument(...),
    letter: bool = typer.Option(False, "--letter", help="Approve generated cover letter for auto."),
) -> None:
    """Post-hoc approval (e.g. you manually submitted after a shadow N)."""
    paths = resolve()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        # FillAttempt CRUD: only the model exists in Phase 5 — record approval
        # via stage update + a note. Live fill records will be wired in the
        # apply-live path.
        # For now we treat this as a noop with a clear message.
        _ = sess
    console.print(
        f"[dim]approve {fill_attempt_id} (letter={letter}): no fill_attempts to update yet — "
        "this verb is reserved for the live-fill path.[/dim]"
    )


@app.command(name="review")
def review_cmd() -> None:
    """Show applications needing human review (aborted_for_review / paused adapters)."""
    paths = resolve()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        # Phase 5 surfaces: paused adapters + applications without forward progress.
        paused = list(
            sess.exec(select(SiteAdapter).where(SiteAdapter.paused_for_review == True)).all()  # noqa: E712
        )
        if paused:
            console.print("[bold]Paused adapters (3+ consecutive failures):[/bold]")
            for ad in paused:
                console.print(f"  - {ad.platform_signature} ({ad.adapter_path})")
        # Inbox drafts (from learn.py) awaiting promotion.
        if paths.adapters_inbox.exists():
            drafts = sorted(paths.adapters_inbox.glob("*.yaml"))
            if drafts:
                console.print("\n[bold]Adapter drafts in inbox awaiting promotion:[/bold]")
                for d in drafts:
                    console.print(f"  - {d.stem}  ({d})")
                console.print(
                    "Edit a draft, then `job adapter promote <signature>` to move it to "
                    "adapters_user/."
                )
        no_inbox = not paths.adapters_inbox.exists() or not any(paths.adapters_inbox.iterdir())
        if not paused and no_inbox:
            console.print("Nothing to review. [green]✓[/green]")


# ─── adapter subcommands ─────────────────────────────────────────────────────

adapter_app = typer.Typer(help="Adapter management.")
app.add_typer(adapter_app, name="adapter")


@adapter_app.command("list")
def adapter_list_cmd() -> None:
    paths = resolve()
    adapters = load_all(paths)
    table = Table(title=f"adapters ({len(adapters)})")
    for col in ("Signature", "Version", "Source", "URL pattern", "Auto-eligible"):
        table.add_column(col)
    bundled_dir = list_bundled()
    user_dir = list_user(paths)
    for ad in adapters:
        origin = (
            "user"
            if (ad.source_path is not None and str(ad.source_path).startswith(str(user_dir)))
            else "bundled"
        )
        marker = "[green]yes[/green]" if ad.submit.auto_eligible else "no"
        table.add_row(
            ad.platform_signature,
            str(ad.version),
            origin,
            ad.match.url_pattern or "—",
            marker,
        )
    console.print(table)
    console.print(f"\nbundled: {bundled_dir}")
    console.print(f"user   : {user_dir}")


@adapter_app.command("promote")
def adapter_promote_cmd(signature: str = typer.Argument(...)) -> None:
    """Move adapters_inbox/<sig>.yaml -> adapters_user/<sig>.yaml; register in DB."""
    paths = resolve()
    paths.ensure()
    run_migrations(paths)
    inbox = paths.adapters_inbox / f"{signature}.yaml"
    if not inbox.exists():
        console.print(f"[red]no inbox draft[/red]: {inbox}")
        raise typer.Exit(1)
    # Parse it to confirm it's valid before moving.
    try:
        ad = load_adapter(inbox)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]inbox draft invalid[/red]: {e}")
        raise typer.Exit(1) from e

    target = paths.adapters_user / f"{signature}.yaml"
    shutil.move(str(inbox), str(target))

    eng = get_engine(paths)
    with Session(eng) as sess:
        existing = sess.exec(
            select(SiteAdapter).where(SiteAdapter.platform_signature == signature)
        ).first()
        if existing is None:
            sess.add(
                SiteAdapter(
                    platform_signature=signature,
                    adapter_path=str(target),
                    version=ad.version,
                )
            )
        else:
            existing.adapter_path = str(target)
            existing.version = ad.version
            existing.paused_for_review = False
            sess.add(existing)
        sess.commit()
    console.print(f"Promoted {signature} → {target}")


@adapter_app.command("test")
def adapter_test_cmd(
    signature: str = typer.Argument(...),
    url: str = typer.Option(..., "--url"),
) -> None:
    """Dry-run an adapter against a URL: load + match + print plan."""
    paths = resolve()
    adapters = [a for a in load_all(paths) if a.platform_signature == signature]
    if not adapters:
        console.print(f"[red]unknown signature[/red]: {signature}")
        raise typer.Exit(1)
    adapter = adapters[0]
    from .adapters.loader import match_url

    matched = match_url(url, [adapter])
    if matched is None:
        console.print(
            f"[yellow]url_pattern[/yellow] {adapter.match.url_pattern!r} does NOT match {url!r}"
        )
        raise typer.Exit(1)
    console.print(f"[green]match[/green]: {signature} on {url}")
    nfields = len(adapter.fields)
    nreq = len(adapter.required_fields())
    console.print(f"  fields    : {nfields} ({nreq} required)")
    console.print(f"  submit    : {adapter.submit.selector}")
    console.print(f"  auto      : {adapter.submit.auto_eligible}")


@adapter_app.command("mark-auto-eligible")
def adapter_mark_cmd(signature: str = typer.Argument(...)) -> None:
    """Flip auto_eligible=true for an adapter (after observing clean shadow runs)."""
    paths = resolve()
    user = paths.adapters_user / f"{signature}.yaml"
    if not user.exists():
        console.print(
            f"[red]not a user adapter[/red]: {signature}. Only adapters in {paths.adapters_user} "
            "can be marked auto-eligible (bundled stays conservative)."
        )
        raise typer.Exit(1)
    text = user.read_text(encoding="utf-8")
    new = text.replace("auto_eligible: false", "auto_eligible: true", 1)
    if new == text:
        console.print(f"adapter {signature} already auto_eligible (or no marker found)")
        return
    user.write_text(new, encoding="utf-8")
    console.print(f"adapter {signature}: auto_eligible: true")


@adapter_app.command("contribute")
def adapter_contribute_cmd(signature: str = typer.Argument(...)) -> None:
    """Open a PR upstream with the user-customized adapter."""
    import os as _os

    paths = resolve()
    src = paths.adapters_user / f"{signature}.yaml"
    if not src.exists():
        console.print(f"[red]not a user adapter[/red]: {src}")
        raise typer.Exit(1)

    upstream = _os.environ.get("JOB_HUNTER_UPSTREAM_REPO", "lomartins/job-hunter-skill")

    gh = shutil.which("gh")
    if gh is None:
        console.print(
            "[yellow]gh CLI not installed[/yellow]. Manual path:\n"
            f"  1. Fork {upstream}\n"
            f"  2. Copy {src} -> skills/job-hunter/assets/adapters/{signature}.yaml\n"
            "  3. Open a PR with the change."
        )
        return
    auth = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, check=False)
    if auth.returncode != 0:
        console.print(
            "[yellow]gh not authenticated[/yellow]. Run `gh auth login`, then re-run this verb."
        )
        return
    console.print(
        f"[dim]contribute path stubbed: would push {src} to {upstream} via gh. "
        "Live PR flow lands in a follow-up.[/dim]"
    )


@app.command(name="salary")
def salary_cmd(
    role: str = typer.Option(..., "--role", help="Role keyword (substring match)."),
    location: str | None = typer.Option(None, "--location", help="Location substring."),
    source: str | None = typer.Option(None, "--source", help="Filter to one source."),
    since_days: int | None = typer.Option(
        None, "--since-days", help="Only rows scraped in the last N days."
    ),
    padding: float = typer.Option(
        0.10, "--padding", help="Padding on top of p75 for the suggested expectation."
    ),
) -> None:
    """Aggregate salary distribution from postings already in the DB.

    Uses Job.salary_min / salary_max from Indeed + RemoteOK + Job na Gringa +
    Glassdoor rows you have. No live scraping. Output: per-currency percentiles
    + a suggested expectation (p75 + padding).
    """
    paths = resolve()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        report = salary_aggregate(
            sess,
            role=role,
            location=location,
            source=source,
            since_days=since_days,
        )

    if report.total_samples() == 0:
        console.print(
            f"[yellow]No salary samples for role={role!r}"
            + (f", location={location!r}" if location else "")
            + (f", source={source!r}" if source else "")
            + "[/yellow]. Run `job-hunter discover --source indeed` (or remoteok / "
            "job_na_gringa / glassdoor) first to gather data."
        )
        raise typer.Exit(1)

    table = Table(title=f"Salary distribution — role={role!r}")
    for col in ("Currency", "Count", "p25", "median", "p75", "Suggested (p75 + pad)"):
        table.add_column(col, justify="right")
    for currency in sorted(report.buckets):
        bucket = report.buckets[currency]
        suggestion = suggest_expectation(report, currency, padding=padding)
        table.add_row(
            currency,
            str(bucket.count),
            _fmt(bucket.percentile(25)),
            _fmt(bucket.median),
            _fmt(bucket.percentile(75)),
            _fmt(suggestion),
        )
    console.print(table)
    console.print(
        f"\nTotal samples: {report.total_samples()}. Padding: {int(padding * 100)}% on p75."
    )


def _fmt(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


@app.command(name="validate")
def validate_cmd(
    id: int | None = typer.Argument(
        None, help="Application id to validate. Omit to scan everything in `queued`."
    ),
    candidate_country: str = typer.Option(
        "Brazil", "--country", help="Your country, for onsite/locked checks."
    ),
) -> None:
    """Pre-flag obvious non-fits (junior, US-only, country-locked, onsite).

    Looks at title + location + description. Blocks = clear no, warns = check.
    Empty output = looks fine.
    """
    from .validate import validate_fit

    paths = resolve()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        if id is not None:
            app = sess.get(Application, id)
            if app is None:
                console.print(f"[red]not found[/red]: application {id}")
                raise typer.Exit(1)
            job = sess.get(Job, app.job_id)
            if job is None:
                console.print("[red]inconsistent state[/red]")
                raise typer.Exit(1)
            reports = [(app, validate_fit(job, candidate_country=candidate_country))]
        else:
            stmt = (
                select(Application, Job)
                .join(Job)
                .where(Application.current_stage == Stage.QUEUED.value)
            )
            reports = [
                (a, validate_fit(j, candidate_country=candidate_country))
                for a, j in sess.exec(stmt).all()
            ]
    table = Table(title="validate-fit")
    for col in ("ID", "Concerns"):
        table.add_column(col)
    flagged = 0
    for app, report in reports:
        if not report.concerns:
            continue
        flagged += 1
        details = "\n".join(f"[{c.severity}] {c.code}: {c.message}" for c in report.concerns)
        table.add_row(f"{app.id:03d}", details)
    console.print(table)
    console.print(
        f"\nScanned {len(reports)} application(s). Flagged: {flagged}. "
        f"(others have no concerns or missing description.)"
    )


@app.command(name="report")
def report_cmd(weekly: bool = typer.Option(False, "--weekly")) -> None:
    """Print pipeline statistics."""
    paths = resolve()
    run_migrations(paths)
    eng = get_engine(paths)
    with Session(eng) as sess:
        total_apps = sess.exec(select(Application)).all()
        by_stage: dict[str, int] = {}
        for a in total_apps:
            by_stage[a.current_stage] = by_stage.get(a.current_stage, 0) + 1
        active = sum(1 for a in total_apps if a.current_stage in {s.value for s in ACTIVE_STAGES})
        terminal = sum(
            1 for a in total_apps if a.current_stage in {s.value for s in TERMINAL_STAGES}
        )

    table = Table(title=("Weekly report" if weekly else "Pipeline report"))
    table.add_column("Stage")
    table.add_column("Count", justify="right")
    for stage in Stage:
        table.add_row(stage.value, str(by_stage.get(stage.value, 0)))
    console.print(table)
    console.print(f"\nactive: {active}  terminal: {terminal}  total: {len(total_apps)}")


@app.command(name="linkedin-login")
def linkedin_login_cmd(
    timeout: int = typer.Option(
        180, "--timeout", help="Seconds to wait for the user to finish logging in."
    ),
) -> None:
    """Open a headed Chromium pointed at LinkedIn login; save the session.

    Use this when LinkedIn invalidates `LINKEDIN_LI_AT` repeatedly. After you
    sign in (and solve any captcha) the persistent Chromium profile at
    `$XDG_DATA_HOME/job-hunter/chrome-profiles/linkedin/` keeps your session
    cookies + JSESSIONID, and subsequent `discover --source linkedin` runs
    reuse it. LinkedIn treats the profile as a returning user and stops
    invalidating tokens.
    """
    paths = resolve()
    paths.ensure()
    profile_dir = paths.chrome_profile_linkedin
    profile_dir.mkdir(parents=True, exist_ok=True)

    async def _run() -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            console.print(f"[red]Playwright not available[/red]: {e}")
            raise typer.Exit(1) from e

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            try:
                page = await context.new_page()
                await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
                console.print(
                    f"[bold]Sign in to LinkedIn in the browser window[/bold] — you have "
                    f"{timeout}s. Session saves to:\n  {profile_dir}"
                )
                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    if (
                        page.url.startswith("https://www.linkedin.com/")
                        and "/login" not in page.url
                        and "/checkpoint" not in page.url
                        and "/uas/login" not in page.url
                    ):
                        break
                    await asyncio.sleep(2)
                else:
                    console.print(
                        "[yellow]Timed out before login completed.[/yellow] "
                        "Re-run with --timeout 300 if needed."
                    )
                    raise typer.Exit(1)
                console.print(
                    f"[green]Logged in.[/green] Session saved to {profile_dir}. "
                    "Future `job-hunter discover --source linkedin` runs will reuse it."
                )
            finally:
                await context.close()

    asyncio.run(_run())


@app.command()
def doctor() -> None:
    """Validate install, paths, perms, deps."""
    rc = healthcheck.main()
    raise typer.Exit(rc)


@app.command(name="web")
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Default localhost-only."),
    port: int = typer.Option(8765, "--port", help="Port to listen on."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the default browser on launch."
    ),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code edits (dev only)."),
) -> None:
    """Launch the local triage webapp.

    Binds 127.0.0.1 by default — no auth, no LAN exposure. Override `--host`
    only if you understand the implication: this DB contains tracked
    application data and (optionally) PII-adjacent notes.
    """
    import threading
    import webbrowser

    import uvicorn

    url = f"http://{host}:{port}/jobs"
    console.print(f"[bold green]webapp[/bold green] → {url}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print(
            "[yellow]warning:[/yellow] binding non-localhost. App has no auth — "
            "exposed jobs.db and notes will be readable by anyone who can reach this port."
        )
    if open_browser and not reload:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "job_hunter.webapp.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command()
def lint() -> None:
    """Scan runtime dirs for accidentally leaked PII patterns."""
    paths = resolve()
    targets = [paths.data_dir, paths.state_dir]
    targets = [p for p in targets if p.exists()]
    if not targets:
        console.print("[yellow]No runtime dirs to scan yet — run `job init` first.[/yellow]")
        raise typer.Exit(0)
    from . import lint_secret_leaks

    args = ["--paths", *(str(p) for p in targets), "--allow-empty"]
    rc = lint_secret_leaks.main(args)
    raise typer.Exit(rc)


def _locate_assets_dir() -> Path | None:
    """Find skills/job-hunter/assets in source layout. None in wheel-only installs."""
    here = Path(__file__).resolve()
    # source layout: scripts/job_hunter/cli.py -> assets/
    candidate = here.parent.parent.parent / "assets"
    if candidate.is_dir():
        return candidate
    return None


def _bootstrap(paths: Paths) -> None:
    """Python port of install_hook.sh so `init` works on Windows too.

    Creates the runtime layout under the platformdirs roots, copies the
    profile/secrets templates only if absent (never clobbers edits), and
    chmods the secrets file 600 on POSIX. Never reads the secrets file.
    """
    paths.ensure()
    assets = _locate_assets_dir()
    if assets is None:
        console.print(
            "[yellow]Template assets not found (wheel install?).[/yellow] "
            f"Create {paths.profile_yaml} and {paths.secrets_env} by hand."
        )
    else:
        for src, dst in (
            (assets / "personal.env.example", paths.secrets_env),
            (assets / "profile.yaml.example", paths.profile_yaml),
        ):
            if dst.exists():
                console.print(f"  skip (exists): {dst}", soft_wrap=True)
                continue
            shutil.copyfile(src, dst)
            console.print(f"  copied: {src.name} -> {dst}", soft_wrap=True)
    if os.name != "nt" and paths.secrets_env.exists():
        paths.secrets_env.chmod(0o600)
    console.print(f"Config dir : {paths.config_dir}", soft_wrap=True)
    console.print(f"Data dir   : {paths.data_dir}", soft_wrap=True)
    console.print(f"State dir  : {paths.state_dir}", soft_wrap=True)


if __name__ == "__main__":
    app()
