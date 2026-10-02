---
description: Preview the form-fill plan for an application (dry-run by default).
argument-hint: "<application-id> [--mode shadow|auto]"
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*)
---

Preview the application form-fill plan for $ARGUMENTS.

Run `job-hunter apply $ARGUMENTS --dry-run` and display the resulting Field plan table.

Then explain:
- What fields are missing values (check the Has value column).
- Which `source.*` references will be pulled from `secret.*` (PII) vs `profile.*` (public).
- Whether the adapter is `auto_eligible`.

NEVER suggest filling in PII values via chat. If something is missing, tell the user to edit `<config>/secrets/personal.env` or `<config>/profile.yaml` directly.

If the user wants a live (non-dry-run) fill, point them at `job-hunter apply <id>` in their own terminal — shadow mode blocks on stdin, so it can't run through Claude's Bash tool. It opens a headed Chromium, fills every field, saves `runs/<ts>-apply-<id>/before_submit.png` (secret fields hidden), then asks `y/N/edit`. `y` clicks submit and moves the application to `applied`; `N` leaves the browser open so they can finish by hand. Every attempt is recorded in `fill_attempts` with a `report.json` that holds selectors and outcomes, never values.

Text targets with a `file.*` source (e.g. Workable's cover-letter textarea) are filled from `<data>/files/<key>.txt` or `.md`.
