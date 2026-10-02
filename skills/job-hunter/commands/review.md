---
description: Surface items needing human review (paused adapters + inbox drafts).
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*), Read
---

Show what's pending review.

Run `job-hunter review`. For each item it surfaces:
- **Paused adapter**: tell the user which platform_signature and where the YAML lives. Suggest opening it in `$EDITOR` and re-running shadow-mode applications to rebuild confidence.
- **Inbox draft**: read the draft file at `<data>/adapters_inbox/<sig>.yaml`. Walk the user through each field, flagging any `source: TODO`. After they've edited and confirmed, suggest `job-hunter adapter promote <sig>`.

Don't auto-edit drafts — the user is the final reviewer.
