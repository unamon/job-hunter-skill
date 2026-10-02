# Source: Gupy

Brazilian ATS used by most mid-to-large BR tech companies. Hostname pattern: `<company>.gupy.io`.

## Discovery: portal search (default)

`portal.gupy.io` has a public JSON search across every company, the one its own UI calls:

```
GET https://portal.gupy.io/api/job-search/jobs?jobName=<term>&limit=100&offset=<n>
-> {"data": [...], "pagination": {"total", "limit", "offset"}}
```

`discover` runs one search per `profile.yaml` role, pages up to `max_pages`, dedupes by job `id`, and keeps only titles that pass `matches_role` (the search is fuzzy). Each row's `careerPageUrl` is usually empty, so the host comes from `jobUrl`; postings are stored under the canonical `https://<host>/jobs/<id>` so the gupy apply adapter matches.

## Optional company list

`$XDG_CONFIG_HOME/job-hunter/gupy_companies.yaml` adds a direct HTML scrape of specific career pages (for companies that don't publish to the portal):

```yaml
companies:
  - somecompany   # subdomain of somecompany.gupy.io
```

The old built-in default list (nubank, itau, ifood, ...) was dropped: by Oct 2026 six of seven no longer had a Gupy page.

## Endpoints

- Listing: `https://<company>.gupy.io/jobs` (HTML, server-rendered)
- Detail: `https://<company>.gupy.io/jobs/<job_id>` (HTML)
- Some companies also expose `https://api.gupy.io/api/v1/jobs?companyId=...` (JSON; check per-company)

## Parser

`selectolax` on the HTML listing:
- Job card: `[data-testid="job-card"]`
- Title: `h3` inside the card
- Location/remote: `[data-testid="job-card-location"]`
- Link: `a[href]` to detail page

## Fingerprint

`external_id` = the job's path segment (e.g. `4392838` from `/jobs/4392838`). Combined with `source="gupy"` + the company subdomain stored in `raw_payload.company_subdomain` for uniqueness.

## Rate limit

Soft: 1 request per 2-4s per host. Gupy is forgiving but we don't push it.

## Phase 1 scope

Stub. Implementation in **Phase 3**. The reference adapter for the Gupy form (separate from this source scraper) is in `references/adapters/gupy.md`.
