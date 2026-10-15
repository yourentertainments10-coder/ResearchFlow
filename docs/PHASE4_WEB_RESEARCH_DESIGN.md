# Phase 4: Web research with citations (design, for owner approval)

Status: **proposal only. No Phase 4 code is written.** Depends on Phase 2 (PR #28, ledger) and Phase 3 (project health). Rules: `AGENTS.md` rule 9 (respect sources), rule 2 (no invented numbers), `SECURITY.md` section 2, ADR-031 (the existing portal fetcher is the model for politeness), ADR-042 (retention).

## 1. Requirement (restated)
A project owner gives the system a web address (or a small set of official sources). The system fetches the page politely, records it as an `evidence_source` (`kind='web'`) with its terms and retention, lets claims be located in it, and later answers cite it. Web material is evidence with a status, never verified data (ADR-039 point 3). The system never browses freely, never searches the open web on its own, and never runs a model in this phase.

## 2. Assumptions (please confirm or correct)
1. **Fetch only addresses the owner supplies** (CLI or later API). No search-engine crawling, no link following. Official sites and open APIs first.
2. **Allow-list by host.** A host must be listed in a committed file `data/evidence/fetch_hosts.csv` (host, robots checked on, terms reference, contact for the site if any, max requests per run). Unlisted host: refused. This is separate from `source_policies.csv`, which only decides whether full copies may be kept.
3. **Retention stays as ADR-042**: default `excerpt`; `full` only where `source_policies.csv` allows it.
4. **No JavaScript rendering, no logins, no paywalls, no CAPTCHA handling.** A page that needs them is a manual upload.
5. **Extraction is deterministic**: a claim is created by a person or a rule-based extractor with a locator (`char_range`, `json_pointer`, `table_cell` for HTML tables or CSV/JSON endpoints). The language model that proposes claims is Phase 5.
6. **Not automated.** Run by the owner from the CLI. No scheduled workflow and no production write without a separate decision.

## 3. Impact analysis
| Area | Impact |
|------|--------|
| Database | None expected. `evidence_sources` already has web fields (`requested_url`, `final_url`, `http_status`, `terms_ref`, `retain_until`). One small migration 006 only if we choose to store `robots_checked_at` and `fetch_error` (see question 4) |
| Network | The first code in the evidence package that touches the network. Isolated in one module, injectable `get`, tested with a fake server only. CI makes no real request |
| Pipeline / sports | None. The portal fetcher is untouched |
| `sie.answer` | None. Citations use the existing `EvidenceRef` via `evidence_ref_from_source` |
| Config | New settings for the User-Agent contact (reuse `HTTP_USER_AGENT`), per-host interval (default 2 s), size cap, timeout |
| Security | New attack surface: SSRF, oversized or hostile content, redirects, decompression bombs. Section 4 lists the controls |
| Docs | ADR-044, `EVIDENCE_LEDGER_DESIGN.md`, `SECURITY.md`, `TESTING.md`, `CHANGELOG` |

## 4. Design
New module `src/sie/evidence/web.py`, called by `sie evidence fetch <project> <url>` (owner CLI, dry-run prints what would be requested).

Request rules (all enforced in code, all tested without the network):
- https only; no credentials in the URL; port 443 only unless the allow-list says otherwise; query kept as given.
- Host must be in `fetch_hosts.csv`. The host name is resolved first and the request is refused if any address is loopback, private, link-local or metadata (SSRF guard); the connection uses the checked address.
- **robots.txt**: fetched and obeyed per host before the first page; a disallow, a missing-but-unreachable robots file (5xx) or a 401/403 stops the fetch. The result and time are recorded in the source note.
- Honest `User-Agent` with a contact (the placeholder is refused, as in ADR-031). At least 2 seconds between requests to a host; stop on 403, 429 or 5xx; no retry storm.
- Redirects: followed at most once and only to the same host; anything else is reported, not followed.
- Content type allow-list (`text/html`, `text/plain`, `text/csv`, `application/json`); size cap (default 5 MiB) and a decompression ceiling; read with a timeout.
- Body is hashed and registered through the existing `register_fetched_source` (so retention, namespacing, isolation and purge already apply).

Output of a fetch: the `source_id`, the retention mode applied, the final URL and status, the robots result, and the limitations ("page may change; retrieved at ..."). Health (Phase 3) then reports it as `current` or `stale` by age.

Citations: an answer cites a web claim through its `EvidenceRef` (title, URL, retrieved-at, tier, and "excerpt only" when bytes are not kept). A claim on an excerpt-only source stays `unverified` (ADR-042); that limitation is disclosed, not hidden.

## 5. Test plan
Fake HTTP server and an injected `get`; no real network in CI. Unit: URL and host rules, SSRF address classification (loopback, RFC1918, link-local, IPv6, DNS rebinding case), robots parsing (allow, disallow, 5xx, 403, missing), redirect policy, content-type and size caps, rate limit with a fake clock, placeholder User-Agent refused. Integration (PostgreSQL): a fetched page becomes an excerpt source; a policy-allowed host keeps a full copy; same content twice is idempotent; two projects stay isolated; purge removes it; health reports it. Security: a test that no code path requests an unlisted host. Lint, format, the repo's checks.

## 6. Open questions for the owner
1. Which hosts go on the first allow-list? Suggest only sites whose terms you have read and recorded; I will not add any myself.
2. Is `excerpt` retention still the right default for fetched pages (so most claims on them stay `unverified`)? Alternative: keep `full` for hosts you mark in `source_policies.csv`.
3. Per-host interval 2 s and size cap 5 MiB: keep?
4. Add migration 006 for `robots_checked_at` / `fetch_error` columns, or keep them in the source note text? Recommendation: note text, no migration.
5. Should the first live fetch be a manual owner run from your machine (as with the portal in ADR-031), since this sandbox cannot reach the web hosts? Recommendation: yes.
6. Search or discovery of sources (finding URLs) stays out of Phase 4. OK?

## 7. Out of scope
Language-model planning and extraction (Phase 5), conversation and authentication (Phase 6), XLSX verification (needs `sie.datasets` loader, separate step after #28), scheduled or automatic fetching, any production action.
