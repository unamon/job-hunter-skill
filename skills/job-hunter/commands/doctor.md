---
description: Validate the job-hunter install (XDG dirs, perms, Playwright, gh, version).
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*)
---

Run `job-hunter doctor` and report the result.

If any check fails:
- Quote the failing row(s).
- Give the exact fix from `skills/job-hunter/references/troubleshooting.md` (e.g. `chmod 600 <config>/secrets/personal.env` on macOS/Linux, `python -m playwright install chromium`).
- Do NOT print the contents of `<config>/secrets/personal.env`.
