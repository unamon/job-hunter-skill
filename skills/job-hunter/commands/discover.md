---
description: Discover new job postings from a source. Default = remoteok.
argument-hint: "[source-name]  (e.g. remoteok, linkedin, gupy, job_na_gringa)"
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*)
---

Run job discovery via the job-hunter CLI. Source: $ARGUMENTS (default to `remoteok` if empty).

Steps:
1. Run `job-hunter discover --source <source>` where `<source>` is the argument (or `remoteok`).
2. Show the report counts (discovered / new / updated / failed) and surface any errors.
3. If new jobs were added, suggest `/job-hunter:list` so the user sees them.

PII safety: never echo `<config>/secrets/personal.env` or any LINKEDIN_LI_AT value in chat. The CLI loads them via `python-dotenv` in its own process.
