---
description: Generate a Reactive Resume JSON tailored to a tracked role.
argument-hint: "<application-id> [--out path.json]"
allowed-tools: Bash(job-hunter:*), Bash(job:*), Bash(uv run job-hunter:*), Read, Write, WebFetch
---

You're producing a Reactive Resume (https://github.com/amruthpillai/reactive-resume) JSON file tailored to the JD of application `$ARGUMENTS`.

## Inputs you need

1. **The JD.** Run `job-hunter show $ARGUMENTS` and use the printed `description`. If empty/short, fetch the `url` via WebFetch for the long-form JD.
2. **The user's profile.** Read `<config>/profile.yaml` (`job-hunter info` prints `<config>`) — that's the non-PII profile (roles, links, achievements, experience timeline). Use only what's there. If a field is missing, leave the corresponding Reactive Resume section empty rather than inventing content.

## What you must NOT read

- `<config>/secrets/personal.env` — that file holds CPF/RG/phone/address/birthdate/etc. Reactive Resume's PII fields stay empty; the user fills them in the UI after import.
- Any address, birth date, phone number, government ID, or full-resolution headshot.

## Output

Write a single JSON file conforming to Reactive Resume's import schema (the shape exported by https://github.com/amruthpillai/reactive-resume "Export → JSON"). If `--out` was provided, write there; otherwise default to `<data>/files/resume_<application-id>_<YYYY-MM-DD>.json`.

Top-level keys you should populate, in priority order:

- `basics`: `name`, `headline`, `url`, `summary`. **Leave `email`, `phone`, `location`, `birthdate`, `picture` empty** — the user pastes these manually post-import.
- `sections.profiles`: GitHub, LinkedIn, personal site if in profile.yaml.
- `sections.summary`: 3–4 sentences. Lead with the framing from the profile's `public_profile_blurb`, adjusted toward what the JD emphasizes. Quote at most one short JD phrase verbatim (≤15 words).
- `sections.experience`: keep entries the profile already has; **reorder bullets per entry to surface the ones most aligned with the JD**. Do not invent achievements.
- `sections.skills`: filter the user's known skill set down to the union of (their skills) ∩ (skills the JD names). If the user lists .NET and the JD names ASP.NET Core/Entity Framework/LINQ, include all three under one "Languages & Frameworks" subgroup.
- `sections.projects` / `sections.publications` / `sections.awards`: include items aligned to the JD; drop the rest.
- `metadata.template`: default to `azurill` (matches a clean dark theme on Reactive Resume). The user can switch in the UI.

## After writing

Print:

1. The full output path.
2. The chosen Reactive Resume template name.
3. One short paragraph (≤80 words) explaining which JD signals drove the reordering decisions — so the user understands the diff vs their default resume.
4. A one-liner reminder to fill PII fields (email, phone, address, birthdate) inside the Reactive Resume UI after importing.

Do not push the file anywhere, do not open a browser, do not upload it. The user imports it locally.
