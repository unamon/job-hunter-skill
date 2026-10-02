---
description: Regenerate tracking.md from the SQLite DB.
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*)
---

Run `job-hunter sync` to regenerate `<data>/tracking.md` (and per-job files) from the database.

After it completes, print the path and the head of `tracking.md` so the user sees the current pipeline at a glance.
