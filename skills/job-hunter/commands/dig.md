---
description: Dig into a tracked role — re-fetch JD, validate fit, surface tailoring angles.
argument-hint: "<application-id>"
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*), Read, WebFetch
---

You're investigating job application `$ARGUMENTS`. Goal: bring the row up-to-date and produce a one-screen brief the user can act on.

## Step 1 — Refresh the data

Run, in order:

1. `job-hunter show $ARGUMENTS` to inspect the current DB row.
2. If `description` is empty or shorter than ~400 chars, attempt to re-fetch by visiting the canonical `url` field with WebFetch and asking for the JD body. Update the DB via `job-hunter` only if the skill exposes an enrich verb; otherwise just incorporate the JD into your synthesis without persisting it.
3. `job-hunter validate $ARGUMENTS` to get the auto-generated fit concerns.

If any of those steps fail, surface the error and stop — don't fabricate JD content.

## Step 2 — Produce the brief

Write a markdown report with these sections, in this order. Keep it tight — total length under ~300 words.

### Snapshot
One-line description of the role: title, company, location, comp (if known), stage.

### Why it might fit
2–4 bullets pulled from the JD that map onto the user's profile (roles, skills, experience and location in profile.yaml). Quote short JD phrases (≤15 words) in quotes; do not paraphrase entire paragraphs.

### Friction
Surface anything that creates work or risk: timezone, country lock, on-site, US clearance, junior title hidden in seniority phrasing, generic ATS form likely to need profile boilerplate, no salary disclosed, etc.

### Tailoring angles
2–3 angles to emphasize in the resume / cover letter, tied to specific phrases in the JD. Each angle = one sentence: "Lead with X because the JD asks for Y."

### Next move
One imperative sentence: queue / withdraw / apply now / wait-for-info.

## Constraints

- Do not auto-apply.
- Do not auto-transition the stage. The user decides.
- Do not invent JD content the source didn't provide.
- Respect the PII rules: no secrets file reads, no CPF/RG/phone in your output.
