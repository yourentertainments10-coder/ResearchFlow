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
