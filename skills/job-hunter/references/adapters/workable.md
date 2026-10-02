# Adapter: Workable

`platform_signature: workable`. Hosted at `apply.workable.com/<account>/j/<shortcode>/`; the form itself lives at `.../apply/`. The account slug is often a parent company (e.g. OkWhen posts under `behaviorlive`).

## Match

```yaml
match:
  url_pattern: "*apply.workable.com/*/j/*"
  dom_markers:
    - "form[data-ui='application-form']"
```

## Field map (default form)

| Input | Adapter source |
|-------|---------------|
| `#firstname` | `profile.first_name` |
| `#lastname` | `profile.last_name` |
| `#email` | `profile.links.email` |
| `#headline` | `profile.headline` (optional) |
| `#input_phone` | `secret.JOB_HUNTER_PHONE` (optional; country-code picker defaults from the visitor's IP) |
| `#address` | not mapped — Workable pre-fills it from the visitor's IP location. `#city`/`#country`/`#postcode` are hidden inputs it composes into `#address`; filling them appends junk. |
| `input[type=file][data-ui=resume]` | `file.resume_en` — the input's `id` is randomized per page load, select by `data-ui` |
| `textarea#cover_letter` | `file.cover_letter_en` (optional; text pasted from `files/cover_letter_en.txt` or `.md`) |
| Custom questions | per job; appear under extra sections with `data-ui` keys |

## Finding the form from a job board

Boards like RemoteOK often paywall the outbound apply link. List a company's Workable postings with the public API:

```
POST https://apply.workable.com/api/v3/accounts/<account>/jobs
{"query":"","location":[],"department":[],"worktype":[],"remote":[]}
```

Each result's `shortcode` gives the form URL `https://apply.workable.com/<account>/j/<shortcode>/apply/`.

## Submit

```yaml
submit:
  selector: "button[type='submit'][data-ui='apply-button']"
  mode: shadow
  auto_eligible: false
```
