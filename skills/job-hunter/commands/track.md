---
description: Track a specific job URL (add to DB, queue it).
argument-hint: "<url>"
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*), Bash(sqlite3:*), Bash(python:*)
---

Track this job URL: $ARGUMENTS

Workflow:
1. Detect the source from the URL (e.g. `gupy.io` → gupy, `boards.greenhouse.io` → greenhouse via adapter, etc.).
2. If the URL belongs to a discover-supported source, run `job-hunter discover --source <name> --query <derived-keywords>` to pull it in.
3. If the URL doesn't fit any discover-source (e.g. it's a direct ATS apply link from somewhere else), add the row manually:
   - INSERT into `jobs` and `applications` in `<data>/jobs.db` (`job-hunter info` prints `<data>`). Use the `sqlite3` CLI if present; otherwise (typical on Windows) a short Python script using the stdlib `sqlite3` module.
   - Set `source = manual`, fill in title/company/url, scraped_at = now, fingerprint = sha256 truncated.
4. Run `job-hunter queue <id>` on the newly-inserted application.
5. Show the resulting row.

Ask the user for title + company if they're not derivable from the URL — don't guess silently.
