# Data Pipeline

## 1. Source hierarchy
1. Official competition results (API, JSON feed, downloadable results book or PDF)
2. Official federation or event result pages
3. Reputable sports databases
4. Major news agencies (used to cross-check, not as a primary feed)
5. Manual CSV supplied by the owner (always allowed; same validation)

Never use social media or unverifiable pages as a medal source.

## 2. Phase 0: source discovery (a gate)
Goal: find the most reliable, machine-readable way to get event-level results. Output: `docs/SOURCE_DISCOVERY.md` filled with evidence.

Checklist per candidate source:
- [ ] URL, owner, language
- [ ] `robots.txt` content and whether the relevant paths are allowed
- [ ] Terms of use: is automated or personal-analysis access allowed?
- [ ] Access method: documented API, undocumented JSON used by the site, static HTML tables, JavaScript-rendered pages, PDF
- [ ] How to find JSON endpoints: browser developer tools, Network tab, filter XHR/Fetch, reload the results page, look for requests returning JSON with medal or event data
- [ ] Does it give event-level data (sport, discipline, event, gender, gold/silver/bronze country)?
- [ ] Does it include the official medal table (needed for reconciliation)?
- [ ] Update frequency and timestamp availability
- [ ] Sample saved as a fixture under `tests/fixtures/<source>/`
- [ ] Stability risk (is the structure likely to change?)

Decision rule: pick the highest-priority source that gives event-level data and permits access. If none permits automated access, the primary path becomes **manual CSV import** (Section 9) plus automation of everything after it.

## 3. Pipeline stages
| Stage | Input | Output | Notes |
|-------|-------|--------|-------|
| Fetch | config, last run time | `RawDocument` | Rate-limited, cached, conditional requests |
| Store raw | `RawDocument` | file + `raw_versions` row + `raw_fetches` log | Hash (SHA-256); version rules in section 4 |
| Parse | raw file | `ParsedResult` list | Pure function, tested on fixtures |
| Normalise | `ParsedResult` | canonical ids | Reference tables + aliases |
| Validate | normalised rows | valid / quarantine | Rules in Section 6 |
| Load | valid rows | DB upsert | One transaction |
| Reconcile | DB + official table | `reconciliation_results` | Per country, per medal type |
| Snapshot | DB | `analytics_snapshots` | After every successful load |

## 4. Raw storage and versioning
Terms: a **raw document** is one logical page or feed, identified by (source, url). A **raw version** is one distinct content of that document, identified by its SHA-256. A **fetch** is one attempt to retrieve it, and every fetch is logged.

| Situation | Result |
|-----------|--------|
| New URL | Create the document, version 1, store the file, log `new_version`, parse |
| Same URL, same hash as the latest version | No new version, no file written. Log `unchanged`. Do not re-parse |
| Same URL, a hash never seen before | New version (`version_no + 1`), store the file, set it as latest, log `new_version`, parse |
| Same URL, hash equals an older version (content reverted) | No new version row or file. Set latest back to that older version, log `reverted`, re-parse so the database follows the source |
| Fetch error or non-success status | Log `error` with the status. No version change. Retry with backoff |

- Uniqueness: (document, sha256) and (document, version_no). Identical content is never stored twice, and the same URL fetched a thousand times does not create a thousand versions.
- Hash the exact bytes received. If a source embeds volatile fields (timestamps, tokens), its adapter defines a documented `canonical_bytes()` that strips them before hashing; Phase 0 records this per source.
- Storage key: `<source>/<YYYY-MM-DD>/<HHMMSS>_<sha256-first-12>.<ext>` (date and time of the first fetch of that version) inside the configured raw store: local `data/raw/` or an S3-compatible bucket such as Cloudflare R2 (ADR-017). Objects are write-once; nothing overwrites or deletes them.
- Raw objects are never committed to Git; small fixtures for tests are.
- Tables: `raw_documents`, `raw_versions`, `raw_fetches` (`DATABASE.md` section 4). Placings and observations reference the raw version they came from.

## 5. Normalisation
Reference files in `data/reference/` (committed, reviewed by hand):
- `countries.csv`: canonical code (use the official 3-letter NOC code), name, region, flag emoji optional
- `country_aliases.csv`: alias to canonical code (examples: "IND", "India", "Republic of India" all map to IND; "Chinese Taipei" vs "Taiwan"; "Hong Kong, China")
- `sports.csv`, `disciplines.csv`, `sport_aliases.csv`: canonical names, with the sport-to-discipline hierarchy (examples: "Athletics" = "Track and Field"; "Aquatics" contains swimming, diving, artistic swimming, water polo)
- `gender_aliases.csv`: Men, Male, M, "Men's" to **Men**; Women, Female, W, "Women's" to **Women**; Mixed to **Mixed**; Open or unspecified to **Open**

Rules:
- Matching is case-insensitive after trimming and Unicode normalisation (NFKC); emoji and punctuation are stripped for matching only.
- A value not found in the reference tables is **never auto-created**. It is quarantined with reason `UNKNOWN_COUNTRY`, `UNKNOWN_SPORT`, etc. The owner adds the alias and re-runs.
- Event names are kept as given in `event_name_raw`, and a cleaned `event_name` is stored. Gender is derived from structured fields first, then from the event name, and the method is recorded.

## 6. Validation rules
| Rule | Severity |
|------|----------|
| Country, sport, discipline, gender all resolved | error (quarantine) |
| Medal type in {Gold, Silver, Bronze} | error |
| Event has at most 1 gold placing and 1 silver placing, unless the source explicitly marks a tie (then up to 2 of that medal, and the next medal is omitted per the sport's rule) | error |
| Bronze placings: 1, or 2 for sports flagged `double_bronze`, or as allowed by a marked tie | error |
| A country appears at most once per event for team events | error |
| Event marked completed has gold, silver and at least one bronze | warning (may be partial in a live feed) |
| Event date within the competition dates | warning |
| Duplicate (event, medal, slot, country) | skipped as duplicate, logged |
| Entrant credited to more than one country | error (quarantine `MULTI_COUNTRY_ENTRANT`) |
| Entrant's country differs from the placing's country | error (quarantine) |
| Sum of event medals per country equals the official table | reconciliation check |
| Total distinct events not above the official events total | error if exceeded |
| Source timestamp not older than the previously stored version for the same event | warning |

## 7. Conflicts between sources (freshness-aware)
Principle: **source priority is a tie-break input, never a blind override.** A higher-priority source can be stale, and a lower-priority source can carry a newer correction.

Every claim a source makes about a placing is stored as a **source observation** (source, value, `observed_at`, `source_updated_at` if the source gives one, raw version). Placings hold the *accepted* value. Observations are evidence.

Definitions:
- An official observation is **fresh** when we fetched the official source successfully *after* the most recent observation from any other source about the same event, and within `FRESHNESS_THRESHOLD` (config; default 30 minutes while the competition is running, 24 hours otherwise). Otherwise it is **stale**.
- A **conflict** exists when two current observations for the same (event, medal, slot) name different countries.

| Situation | Action | Event flag |
|-----------|--------|-----------|
| Only one source has the event | Accept | none |
| All sources agree | Accept; store the confirmations | none |
| Official fresh, another source disagrees | Prefer official automatically; record the conflict as `auto_resolved` with the rule applied | none (listed on the Data quality page) |
| Official stale, another source disagrees (for example a secondary source reports a correction) | Do **not** apply the other value and do **not** assume official is right. Keep the last accepted value, trigger an immediate re-fetch of the official source, record `needs_review` | `disputed` |
| After the re-fetch, official now agrees with the other source | Apply it as a reallocation or correction (`DATABASE.md` section 6); conflict `resolved` | cleared |
| After the re-fetch, official is unchanged and still disagrees | Keep the official value, conflict stays `needs_review` | `disputed` |
| Two non-official sources disagree and there is no official observation | Keep the last accepted value, `needs_review` | `disputed` |
| Official changes or removes a placing we had already accepted | Not a conflict: a reallocation or correction with history | none |

Handling disputed events:
- They stay in analytics with the last accepted value, are counted in `analytics_snapshots.disputed_events`, and are flagged on every table and chart that includes them.
- The conflict is re-evaluated on every run and can clear by itself when official refreshes and agrees.
- The owner can settle one manually with `sie resolve-conflict <id> --accept <source> --note "..."`. The note and the owner are stored.
- Typical benign causes: update timing, shared medals, reclassification. Investigate, do not assume.

## 8. Incremental updates and change detection
- Each run compares content hashes. Only changed documents are re-parsed.
- Event state machine: `scheduled` to `in_progress` to `completed` to `amended`. Amendments (medal reallocation) follow the procedure in `DATABASE.md` section 6: the old placing version is closed and a new current version is created, with a `placing_history` row.
- After load, compute the difference versus the previous snapshot: new medals, changed medals, rank movements. Stored in `snapshot_changes`.

## 9. Manual CSV import (always supported)
File: `data/manual/<anything>.csv` with header:
```
competition,sport,discipline,event,gender,medal,country,athlete_or_team,date,source_url,source_note
```
- One row per medal placing. Team events: one row per country.
- Same validation, normalisation, quarantine and loading as automated data. Rows are tagged `source = manual`.
- The CSV file itself is stored as a raw document (source `manual`, url `file://<name>`) and versioned by hash, so every placing has a raw version.
- Command: `sie import-csv data/manual/file.csv`.
- Use this path to fill gaps, or when a source does not permit automation.
- Optional columns also accepted: `participation` (Individual / Team / Pair), `slot`, `is_tie` (yes / no). A second gold or silver needs `is_tie` yes on both rows; a second bronze is accepted only in sports flagged `double_bronze`.
- Re-running the same file changes nothing. If rows were quarantined and the owner then adds an alias, re-running the same file loads them and marks their quarantine rows `resolved`; quarantine rows are never deleted.
- A structural problem (unknown or missing column, ragged row, oversized cell, too many rows, invalid UTF-8) refuses the whole file, but the file is still kept in the raw store and the run is recorded as `failed`.

## 10. Politeness and legality
- Honest `User-Agent` including a contact address.
- Minimum 2 seconds between requests to one host, no parallel hammering, cache every response, stop on HTTP 429 or 403 and report.
- Do not bypass logins, paywalls, CAPTCHAs, or access controls.
- Store only public, factual results data. Link every row to its source.
- Review terms of use of each source in Phase 0 and record the decision.

## 11. Observability
Every run writes to `ingest_runs`: run_id, started_at, finished_at, status, documents_fetched, documents_changed, rows_loaded, rows_quarantined, error_summary. The dashboard "Data quality" page reads from it.

## 12. Failure categories, retry contract and source freshness (Phase 6 foundation, built)
Code: `src/sie/pipeline/failure.py`, `observe.py`, `freshness.py`, and `record_fetch_failure` in `runner.py`. No new tables: it reuses `ingest_runs`, `raw_fetches`, `raw_versions` and `quarantine`.

**Run lifecycle.** `ingest_runs` row: competition, source, `started_at`, `finished_at`, `status` (`running`, `success`, `failed`), `docs_fetched`, `docs_changed`, `rows_loaded`, `rows_quarantined`, `error_summary`. The run is committed before anything else happens, so a run that dies is still on record (`stuck_runs` finds runs left `running`). A failed run is stored with `error_summary = "[category] detail"`.

**Deterministic outcome** (`derive_outcome`, a pure function of the stored row): `running`, `failed`, `succeeded_with_quarantine` (some row needs attention, takes precedence), `unchanged` (the source bytes equal the previous version), `succeeded`. `sie run-summary <id>` prints the structured summary (counts, quarantine by reason, failure category, retry rule) built from the database row.

| Category | Meaning | Retry | Why it is safe |
|----------|---------|-------|----------------|
| `fetch_failure` | Source unreachable or answered with an error | Automatic, up to 3 attempts with backoff | Nothing stored, no version changes. A `raw_fetches` row with outcome `error` and the HTTP status is the only trace |
| `raw_store_failure` | Bytes could not be stored intact (for example a write-once conflict) | Manual only | The run is closed as failed; existing evidence is never replaced |
| `parse_failure` | Input is not in the shape the parser expects | After a parser fix | Raw input kept; the same bytes parse the same way, so retrying unchanged is pointless |
| `validation_failure` | The normalise or validate stage itself broke (a single bad row is quarantine, not a failed run) | After fixing rules or reference mappings | Raw input kept; nothing loaded |
| `load_failure` | The all-or-nothing database load was rejected | Once automatically, then manual | One transaction, rolled back |

**Idempotency and evidence.** Re-running a failed stage on the same input is always safe: raw bytes are written once and hashed (`unchanged` on repeat), the load is one transaction, quarantine is not duplicated. A failed fetch never creates, replaces or re-points a raw version. Bytes that already exist under a key with different content raise an error instead of overwriting.

**Source freshness** (`source_freshness`, `sie source-status <source>`, read-only): `last_attempt_at/status`, `last_success_at`, `last_change_at`, a `fingerprint` (one hash over the latest version of every document of the source, so it changes only when content changes), and the raw artifact references (URL, version, sha256, backend, path). Status: `never_succeeded`, `failing` (the latest finished attempt failed after the last success, even if the success is recent), `stale` (last success older than `FRESHNESS_THRESHOLD_MINUTES`), `fresh`. `sie source-status` exits 1 unless the source is fresh.

### Scheduler, retries and locking (Phase 6A, built)
Code: `src/sie/pipeline/scheduler.py`, `src/sie/db/locks.py`; commands `sie scheduled-run <capture|csv> <file>` and `sie recover-stuck`; workflow `.github/workflows/refresh.yml`. It applies the retry contract above and does not redefine it.

- **Lock.** One session-level PostgreSQL advisory lock per (competition, source), taken with `pg_try_advisory_lock` on a dedicated connection held for the whole run. A second run of the same source does not wait: it returns `skipped_locked`, writes no run row and exits 0. Different sources use different keys and run independently. The lock ends with its session, so a crashed process cannot leave a source locked.
- **Retries.** Fetch: at most 3 attempts in total, waiting 2 s then 4 s between them, each failure recorded as its own `fetch_failure` run. Load: at most 1 automatic retry, on the bytes already fetched (nothing is fetched or stored twice). Parse, validation and raw-store failures are returned as they are, never retried here.
- **Stuck runs.** After taking the lock, the scheduler closes this source's runs still `running` and older than 1 hour as `failed` with the text "abandoned: ...". They get no failure category (the locked categories are unchanged). `sie recover-stuck` does the same for any or all sources. Only `ingest_runs` is touched.
- **Portal fetcher (ADR-031).** `sie scheduled-run portal` fetches the portal through `sources/bornan/fetch.py` and ingests it through the same scheduler path as a file. It needs `PORTAL_FETCH_ENABLED=true` and a real `HTTP_USER_AGENT`; otherwise it exits 2 and records nothing. `sie fetch-portal --out FILE` saves the capture without loading it. Requests: `ALL/disc/data`, then `{DISC}/medals/discipline` for each discipline, 2 seconds apart. All or nothing; a 429 or 5xx stops at once. `refresh.yml` runs it daily at 02:30 UTC when the repository variable `PORTAL_FETCH_ENABLED` is `true`, then `sie health`.

### Source health and alerts (Phase 6B, built)
Code: `src/sie/pipeline/health.py`, `alerts.py`, `notify.py`; command `sie health`. It reads freshness (`freshness.py`) and adds no new classification: `fresh`, `stale`, `failing` and `never_succeeded` keep the meaning above, and read-only throughout.

**Health contract** (`SourceHealth`, one per source, evaluated independently): the freshness status; last successful run and last failed run (id, time, failure category, error detail); `consecutive_failures` (failed runs since the last success, or all failed runs if none ever succeeded; a run closed as abandoned counts as one failure); stuck run ids; last change time, fingerprint and raw artifact references. `never_succeeded` (no good data yet, including a source that has not run at all) is a different state from `stale` (had good data, it aged out) and `failing` (the latest finished attempt failed after a success).

**Alert contract** (`evaluate_alerts`, pure and deterministic, level-triggered):
| Alert | Condition | Severity | Threshold source |
|-------|-----------|----------|------------------|
| `never_succeeded` | status `never_succeeded` | critical | none |
| `repeated_failures` | `consecutive_failures >= 3` | critical | the fetch attempt limit of the retry contract (`RETRY_RULES`), so one scheduled run that exhausts its fetch retries already alerts; a single failure does not, it may be retried |
| `stale` | status `stale` | warning | `FRESHNESS_THRESHOLD_MINUTES` |
| `stuck_runs` | a run `running` longer than the stuck age | warning | the scheduler's 1 hour (`--stuck-after-minutes`) |
A healthy source raises nothing. Each alert has a stable `key` (`competition:source:kind`) and evidence (counts, run ids, last error). Alerts are produced for as long as the condition holds; remembering what was already sent is the delivery layer's job (`partition_alerts` splits new, still active and resolved against the previous keys).

**Sources checked:** `--source` (repeatable), otherwise `SCHEDULED_SOURCES` (new, comma separated; the sources expected to refresh, so a source that never started still alerts) plus every source that has a run.

**Notifications:** `Notifier.send(alerts) -> DeliveryResult`. Built in: `LogNotifier` (the only channel; critical as error, warning as warning) and `RecordingNotifier` (tests). `deliver()` never raises, reports every alert a channel lost or dropped, and sends nothing for an empty batch. `WebhookNotifier` (6B follow-up) posts one JSON batch (`text`, `content`, `alerts`) to `NOTIFY_WEBHOOK_URL`; https only (http for localhost); any non-2xx or network error marks every alert in the batch as failed.

**Command:** `sie health` prints one deterministic JSON report (sorted keys, sources sorted) and exits 1 if any alert exists. `sie source-status` is kept unchanged: it reports a single source's freshness; `sie health` is new because it covers several sources and adds failure counts, stuck runs and alerts.

**Delivery and de-duplication (ADR-030):** `sie health --channel log|webhook|none`. State is a JSON file (`ALERT_STATE_PATH`, default `DATA_DIR/alert_state.json`). An alert is sent when it is new, returns after being resolved, was never delivered successfully, or (if `ALERT_RENOTIFY_MINUTES` > 0) is due a reminder. Failed deliveries leave no record, so the next check retries. Resolved alerts are marked inactive. Output always lists every active alert plus a `notification` block `{sent, suppressed, resolved, failed}`; exit 1 while any alert is active.

**`needs_attention` (critical):** the latest failure's category is parse, validation or raw store (not auto-retryable), so it alerts on the first failure instead of waiting for three. Fetch and load failures still wait for the retry limit.

**Backup (6C, encrypted since ADR-034):** `sie backup [--out-dir DIR] [--keep 14] [--encrypt-to AGE_PUBLIC_KEY] [--require-encryption] [--identity-file KEY] [--scratch-url URL]` and `sie restore-test DUMP [--identity-file KEY] [--scratch-url URL]`. Dump and manifest counts come from one exported snapshot. With a recipient the dump is stored only as `*.dump.age` (age public-key encryption, the plaintext is deleted); the restore test decrypts it with the identity file and restores into a scratch database (`sie_restore_*`, always dropped) on a scratch server that must not be the production URL, then checks the dump hash, the Alembic revision, the row counts and the raw-blob hashes against the manifest. The `backup.yml` workflow runs this nightly and uploads only ciphertext and the manifest. Older text and flags (`--out`, plaintext dumps) no longer apply.

**Publish (6D):** `sie publish --out DIR` writes `medals.csv`, `events.csv`, `manifest.json` (counts, `data_as_of`, `data_fingerprint`, per-file SHA-256) from `reporting` views only. Refuses to publish an unknown competition or one with no current placings, leaving the old bundle untouched. The dashboard HTML is still built by `python -m sie.dashboard`.

**Not built (by choice):** entrants and anything from the portal's `entries/...` endpoints (birth dates, participant lists). Open after Phase 6: the owner-side checks listed in `ACCEPTANCE.md` section 8 (webhook drill, deployed-site smoke test, restore with the owner's own key), durable alert state (ADR-030), and the sticky-resolution question in ADR-032.

## 13. Conflict policy engine (Phase 3, built)
Code: `pipeline/conflicts.py` (pure `decide`), `db/conflicts.py` (observations, conflicts, disputed flag, resolution), called from `pipeline/load.py`. Commands: `sie conflicts [--all]`, `sie resolve-conflict ID --accept SOURCE --note "..." [--by NAME]`. Rules and consequences: ADR-032.

- Official source = first entry of `SOURCE_PRIORITY`; fresh window = `FRESHNESS_THRESHOLD_MINUTES`. Every row of the section 7 table has a unit test (`tests/unit/test_conflict_policy.py`); the database behaviour is tested in `tests/integration/test_conflicts.py`.
- `policy_rule` values: `official_fresh`, `official_stale`, `official_still_disagrees`, `non_official`, `agreement`.
- `LoadSummary.refetch_official` reports that the policy wants an immediate official re-fetch. Acting on it is the scheduler's job (Phase 6, not touched here).
