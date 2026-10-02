---
name: job-hunter
description: "Use this skill when the user asks about job hunting, job applications, scraping job boards, tracking application stages, or filling out job application forms. Triggers include mentions of LinkedIn, Gupy, RemoteOK, Job na Gringa, application tracking, cover letter drafting tied to a tracked job, or any request to apply to a job listing URL. The skill discovers tech openings matching the roles in the user's profile.yaml across BR and international remote markets, tracks each through stages (discovered, queued, applying, applied, screening, technical, behavioral, offer, rejected, withdrawn), and assists with form filling in two modes (shadow and auto) while keeping PII out of model context. Do NOT use for general career advice, resume writing from scratch, or interview coaching unrelated to a tracked job."
license: Apache-2.0
version: 0.13.1
---

# job-hunter

Discover, track, and assist with tech job applications (target roles come from the user's `profile.yaml`) across LinkedIn, Gupy, RemoteOK, Job na Gringa, and seven more sources. Tracks every opportunity from `discovered` through `offer`/`rejected` in a local SQLite DB mirrored to human-readable Markdown. Fills application forms in two modes — `shadow` (default; pauses before submit for your review) and `auto` (gated by adapter reliability score and explicit consent flags) — using a YAML adapter system that learns from unknown forms and improves over time.

**When this skill is the right tool:** the user mentions a specific job board or posting URL; asks to track an application; wants to fill a form on a known ATS (Gupy, Greenhouse, Lever, Workday, Ashby); asks for a weekly application report; asks to add a new source or fix a broken adapter. **When it is not:** generic career coaching, resume writing from a blank page, mock interviews not tied to a tracked job.

## Forbidden actions (PII isolation)

This skill keeps personally identifiable information (CPF, RG, phone, full address, birth date, salary expectations, bank info, LinkedIn session cookie) entirely out of the model's context window. Violating this voids the safety guarantee. Treat the secrets file as if reading it would corrupt the database.

**Never run any of these commands against `$XDG_CONFIG_HOME/job-hunter/secrets/personal.env`** (default `~/.config/job-hunter/secrets/personal.env`):

```
cat   head   tail   less   more   bat   view
grep  rg     ag     awk    sed
printenv  env  source
strings  od   xxd   hexdump
cp <secrets> <anywhere-the-model-might-read>
```

Also never:

- Read the file via the `Read` tool.
- Pipe it through any command whose output enters the conversation.
- Suggest the user paste its contents into chat for debugging.
- Decode base64 / hex / etc. that you suspect contains secrets.

**What you CAN do:** read `assets/personal.env.example` (empty-value template — that's the schema you write against), generate scripts that call `os.environ` after `python-dotenv` has loaded the file at runtime in a child process, and run `job-hunter lint` which reports presence/absence by key name without ever printing values.

If a user pastes a real CPF or other PII into chat, stop, warn them, and instruct them to move it into `personal.env`.

## Slash commands (when installed as a Claude Code plugin)

```
/job-hunter:discover [source]    # pull new jobs (default: remoteok)
/job-hunter:list [filters]       # show the pipeline as a Rich table
/job-hunter:track <url>          # track a specific posting + queue it
/job-hunter:apply <id>           # preview the form-fill plan (dry-run)
/job-hunter:status               # pipeline summary + suggested next steps
/job-hunter:review               # paused adapters + inbox drafts
/job-hunter:sync                 # regenerate tracking.md
/job-hunter:doctor               # validate install / perms / deps
/job-hunter:web                  # launch local triage webapp (FastAPI + HTMX)
/job-hunter:dig <id>             # re-fetch JD + brief: fit / friction / angles / next move
/job-hunter:tailor-resume <id>   # Reactive Resume JSON tailored to a JD
```

These are thin wrappers around the CLI — Claude executes the relevant `job-hunter` command via `Bash` and formats the result.

## CLI surface

The package installs two console scripts: `job-hunter` (canonical) and `job` (alias). All commands below work with either name.

```
job init                                          # idempotent setup, creates XDG dirs, copies templates
job discover [--source SOURCE] [--query QUERY] [--watch]
job list [--stage STAGE] [--source SOURCE] [--since DATE]
job show <id>
job queue <id>
job apply <id> [--mode shadow|auto] [--dry-run] [--no-wait] [--i-understand]
job apply --batch --stage queued [--mode ...]
job approve <fill_attempt_id> [--letter]
job review
job stage <id> --to <stage> [--note "..."]
job adapter list
job adapter promote <signature>
job adapter test <signature> --url URL
job adapter mark-auto-eligible <signature>
job adapter contribute <signature>                # PRs adapter back upstream via gh
job sync [--no-md-sync]
job report [--weekly]
job lint                                          # runs lint_secret_leaks against runtime dirs
job doctor                                        # validates install, paths, perms
job web [--host 127.0.0.1] [--port 8765] [--no-open]   # local webapp for triage
```

Defaults:
- `--mode shadow` if omitted.
- `--watch` interval: 6 hours.
- `--no-md-sync` defers tracking.md regeneration for batch operations.
- `--i-understand` is required once per session for auto mode.

## Runtime layout

The repo is read-only after install. All mutable state lives in XDG paths in the user's home.

```
$XDG_CONFIG_HOME/job-hunter/        # default ~/.config/job-hunter/
├── config.yaml                     # XDG path overrides, defaults
├── profile.yaml                    # non-PII profile (roles, locations, links)
├── field_labels.yaml               # user overrides for the bundled label dictionary
└── secrets/
    ├── personal.env                # chmod 600, NEVER READ BY THIS SKILL
    └── README.md

$XDG_DATA_HOME/job-hunter/          # default ~/.local/share/job-hunter/
├── jobs.db                         # SQLite, source of truth
├── tracking.md                     # human-readable mirror
├── tracking/                       # per-job markdown files
├── adapters_inbox/                 # newly-learned adapters awaiting review
├── adapters_user/                  # user-customized adapters (override bundled by platform_signature)
├── files/                          # resumes, cover letter base, attachments
└── runs/<iso-timestamp>/           # per-run artifacts (report.json, screenshots, HAR)

$XDG_STATE_HOME/job-hunter/         # default ~/.local/state/job-hunter/
└── logs/                           # rotating logs + ratelimit.json token buckets
```

For tests: `JOB_HUNTER_HOME_OVERRIDE=/tmp/job-hunter-test` redirects all three XDG roots under one tmp dir. Documented for test fixtures only — never use in prod.

## File map under `scripts/job_hunter/`

Navigate without reading everything. One line per file.

- `cli.py` — Typer + Rich CLI entrypoint. Subcommands wire to functions in the other modules.
- `paths.py` — XDG path resolution; honors `JOB_HUNTER_HOME_OVERRIDE` for tests.
- `models.py` — SQLModel definitions: `Job`, `Application`, `StageHistory`, `SiteAdapter`, `FillAttempt`, `CoverLetterApproval`.
- `db.py` — Engine + session factory. Runs migrations from `../migrations/` on `job init`.
- `tracking_md.py` — Deterministic markdown sync (`tracking.md` + per-job files). Atomic write-temp-rename. Preserves `<!-- notes:start -->...<!-- notes:end -->` blocks.
- `sources/` — One module per data source. All implement the `Source` protocol (`discover()`, `fetch_detail()`).
- `adapters/` — Adapter resolver, YAML schema, source dispatcher (`profile.*`, `secret.*`, `file.*`, `generate.*`).
- `apply.py` — Playwright runner. Shadow blocks for `y/N/edit`; auto runs cooldown + submits if all 5 gates hold.
- `learn.py` — Inspects unknown forms, hashes platform signature, drafts adapter into `adapters_inbox/`.
- `webapp/` — Local FastAPI + HTMX triage UI. `app.py` (factory), `routes.py`, `scoring.py`, `i18n.py`, `templates/`, `static/`, `i18n_data/` (en + pt_BR). Launched via `job-hunter web`. Localhost-only by default.

At `scripts/` root (sibling of the package):

- `lint_secret_leaks.py` — Scans runtime dirs for CPF/CNPJ/RG/phone regex matches. Exits non-zero on hit.
- `healthcheck.py` — Used by `job doctor`. Validates pyproject install, Playwright browser, XDG perms, gh auth (if `adapter contribute` is to work).
- `install_hook.sh` — Idempotent bootstrap: creates XDG dirs, copies templates from `assets/` if absent, prompts user to `chmod 600` the secrets file.

DB migrations live at `scripts/migrations/NNN_description.sql` and are applied in lexical order.

## Loading references on demand

Read these only when working on that specific area. They are NOT part of the always-loaded SKILL.md context.

| File | Read when |
|------|-----------|
| `references/sources/linkedin.md` | Implementing or debugging LinkedIn scraping; cookie expired |
| `references/sources/gupy.md` | Adding a Gupy company subdomain; fixing parser |
| `references/sources/job_na_gringa.md` | Maintaining the Job na Gringa scraper |
| `references/sources/remoteok.md` | RemoteOK JSON shape changes |
| `references/sources/remotive.md`, `wwr.md`, `himalayas.md`, `programathor.md`, `coodesh.md`, `trampos.md`, `arcdev.md` | Implementing source 5–11 |
| `references/adapters/gupy.md`, `greenhouse.md`, `lever.md`, `workday.md`, `ashby.md` | Editing an adapter or learning its quirks |
| `references/apply_modes.md` | Changing shadow/auto behavior; tuning auto gates |
| `references/markdown_sync.md` | Touching `tracking_md.py`; determinism failures |
| `references/self_improvement.md` | Working on `learn.py`; debugging platform signatures |
| `references/decisions.md` | Understanding why something diverges from the spec |
| `references/troubleshooting.md` | Install, perms, Playwright, gh auth, cookie issues |

## Failure modes and recovery

| Symptom | Likely cause | Recovery |
|---------|--------------|---------|
| `job doctor` reports "secrets file world-readable" | Forgot `chmod 600 ~/.config/job-hunter/secrets/personal.env` | `chmod 600` the file; doctor exits clean |
| LinkedIn discover returns 0 results unexpectedly | Cookie expired or LinkedIn flagged the session | Refresh `LINKEDIN_LI_AT` from browser devtools; `references/sources/linkedin.md` has step-by-step |
| `apply` reports `aborted_for_review` immediately | No adapter matches the URL signature; `learn.py` drafted a new one | Edit the draft in `adapters_inbox/`, run `job adapter test <sig> --url ...`, then `job adapter promote <sig>` |
| Auto mode refuses to submit | One of 5 gates failed (success rate <90%, missing `--i-understand`, `generate.*` without approval, etc.) | `job apply <id>` (without `--mode auto`) submits via shadow; or fix the gate per error message |
| `tracking.md` shows churn on every regen | Non-deterministic input (e.g. unstable ordering) | File a bug; the determinism test should have caught this — see `references/markdown_sync.md` |
| `job lint` flags a CPF match in `runs/` | A scraper or apply run leaked PII into an artifact | Open the file, remove the value, fix the leak source (likely a log statement); `lint` exits non-zero until clean |
| Playwright launch fails on Wayland | `playwright install chromium` not run, or Chromium can't find a display in headed mode | `job doctor` reports both; `playwright install chromium` and/or set `BROWSER_WS_ENDPOINT` to a Browserless container |
| `apply` hangs in `claude -p` | Shadow mode needs a TTY; headless detected, auto-aborts | Run from an interactive shell, or pre-approve and use `--mode auto` |

For anything else, attach the relevant `runs/<iso-timestamp>/report.json` to a GitHub issue.
