# Phase 4: Web research with citations (design, for owner approval)

Status: **approved by the owner 2026-10-23 with a wider scope, implementation in stages.** Built so far: 4a (safe fetch core) and 4b (discovery and ranking) on `feature/phase-4-web-research`, stacked on Phase 3 and PR #28. Not built: 4c (collection into a normalised dataset, needs the merged `sie.datasets`), a concrete search provider, and everything after (see section 8). Depends on Phase 2 (PR #28, ledger) and Phase 3 (project health). Rules: `AGENTS.md` rule 9 (respect sources), rule 2 (no invented numbers), `SECURITY.md` section 2, ADR-031 (the existing portal fetcher is the model for politeness), ADR-042 (retention).

## 1. Requirement (restated)
A user asks a research question; the system discovers candidate sources from free search options and public dataset catalogues first, prioritises official/government sources, fetches accessible public HTML, CSV, JSON and open APIs politely, records evidence and retrieval metadata, normalises data into a reusable dataset, and answers with reproducible analytics and citations. Follow-up questions reuse the collected dataset whenever it contains the needed fields. Web material is evidence with a status, never verified data (ADR-039 point 3). No paid provider, model API, or billable service may be used by default or as an automatic fallback. If free discovery is unavailable or exhausted, report the limitation and offer the file/URL/manual import fallback.

## 2. Assumptions (please confirm or correct)
1. **Discovery starts with free sources.** Query free search options that have a documented no-cost allowance and public dataset catalogues; prefer official/government sources and open APIs. No paid provider is called, even as a fallback. Do not crawl search-result pages or recursively follow arbitrary links. A provider must declare its free-tier limits and terms in its adapter docs. When the quota is exhausted, fail closed with a clear message and keep any cached leads.
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

## 6. Operating defaults
1. Use general network guards in code rather than forcing per-host owner approval for ordinary public sources; always respect robots.txt, published terms, API rules, honest User-Agent, rate limits and source blocks. Never bypass login, paywall, CAPTCHA or access controls.
2. Keep `excerpt` retention as the default; full copies only under the existing recorded `source_policies.csv` policy.
3. Keep the 2-second per-host interval and 5 MiB default size cap.
4. Keep robots/refusal details in source notes; no migration 006 for these fields.
5. First live fetch is a controlled manual owner run; automate only after that passes.
6. Discovery is part of Phase 4 and must try free search options and public catalogues before returning a request for manually supplied leads.

## 7. Out of scope
Language-model planning and extraction (Phase 5), conversation and authentication (Phase 6), XLSX verification (needs `sie.datasets` loader, separate step after #28), scheduled or automatic fetching, any production action.

## 8. Owner decisions of 2026-10-23 and how they are met
| Decision | What was built or planned |
|----------|---------------------------|
| Source discovery is built in, not a fixed URL list | 4b ranks leads from a pluggable `SearchProvider`; the default implementation must be free-first. Public dataset catalogues and documented free search options are tried before manual lead-file fallback. Never automatically switch to paid APIs. |
| Trust order: official and government first, Wikipedia and reputable references as secondary | `data/evidence/source_authority.csv` (tiers 1 to 4, longest suffix wins, unknown host = tier 4). Authority dominates the score; relevance and downloadable data follow |
| Default excerpt plus URL, date and evidence; full copies only by policy | Unchanged from ADR-042; `sie evidence fetch` registers excerpts |
| 2 s per host, 5 MiB, timeout | Settings `EVIDENCE_FETCH_INTERVAL_SECONDS` (minimum 2), `EVIDENCE_FETCH_MAX_BYTES`, `EVIDENCE_FETCH_TIMEOUT_SECONDS` |
| No migration 006 | None added; robots and refusal details are returned to the caller and stated in notes |
| Manual first, then a controlled live test, automation later | `sie evidence fetch` is a dry run unless `--execute`; no workflow runs it |
| The fixed allow-list is dropped | Replaced by guards that apply to every host: https only, public addresses only, robots.txt obeyed, rate limit, stop on 403/429/5xx, caps. A blocked or unsuitable source is listed with its reason, and the next candidate is used |

## 9. Still to build (each its own PR and approval where it adds a dependency or cost)
1. **Free discovery adapters**: add documented free search/catalogue adapters only after checking current terms, quota and access requirements. No new dependency unless justified; use existing HTTP and parsing stack wherever practical. Keep a local catalogue adapter and manual lead-file fallback. If a free provider requires a key, clearly state signup/quotas but never request payment details or make billable calls.
2. **4c Collection**: turn fetched CSV, JSON and HTML tables into a normalised dataset with provenance per row, reusing `sie.datasets`. Conflicting and missing values are flagged, never filled.
3. **Analysis, charts and reports** on the collected dataset, with same-dataset follow-up reuse. Deterministic pandas/SQL operations and existing charting dependencies first; no model required for first working analytics loop.
4. **Metric definitions** for questions such as "how many times has a party won": Lok Sabha seats, assembly seats and governments formed are different metrics and are kept apart, never summed.
5. **Cost guard**: tests prove that exhausted quotas, timeouts or unavailable providers cannot trigger any paid endpoint; every adapter reports whether access is free, capped or manual.
