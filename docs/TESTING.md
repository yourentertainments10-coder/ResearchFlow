# Testing Strategy

## 1. Principles
- Tests are written with (or before) the feature, not at the end.
- Network calls are never made in automated tests. Adapters are tested against saved fixtures.
- Analytics are tested with small hand-computed datasets whose answers are known.

## 2. Test database
Tests run on **real PostgreSQL**, never SQLite, because the design depends on partial indexes, composite foreign keys and transactional behaviour. Locally the suite starts an embedded Postgres automatically (`pgserver`, installed with the dev extras); in CI it uses a Postgres service container via `TEST_DATABASE_URL`. A template database with all migrations applied is built once per run and each test gets a fresh copy, so tests are isolated and fast.

## 3. Layers
| Layer | What | Tools |
|-------|------|-------|
| Unit | Normalisation, parsing, each metric function, validators | pytest |
| Property / invariant | Totals and shares always consistent | pytest, hypothesis (optional) |
| Integration | Raw fixture to DB to analytics tables, full pipeline in a temporary PostgreSQL database | pytest |
| Idempotency | Same input twice gives identical DB | pytest |
| Migration | Upgrade from empty and from previous version | alembic in a temp DB |
| Parser contract | Each source parser's output matches the expected `ParsedResult` for stored samples | pytest + fixtures |
| Architecture rules | Adapters never import parsers; parsers never import network or database code | import test (or import-linter) |
| Dashboard smoke | App starts and each page renders against a test DB | streamlit testing API |
| Data quality (runtime) | Reconciliation and quarantine reports on real data | part of pipeline, not CI |

## 4. Fixtures
`tests/fixtures/`
- `sources/<source>/*.json|html`: saved real responses (small, trimmed)
- `golden/small_competition.csv`: about 30 events with every tricky case: double bronze, tie, mixed event, team event, open event, reallocation, unknown country alias
- `golden/expected_*.csv`: hand-calculated expected outputs for every metric

## 5. Must-have test cases
1. Country alias variants ("IND", "India", "Republic of India") resolve to one country.
2. Gender extraction from "Men's", "Women's", "Mixed", and open events.
3. Team event counts once per country.
4. Double bronze events produce two Bronze placings (slots 1 and 2) without violating `ux_placings_current`.
5. Reallocation (Gold from A to B): the old placing has `is_current = 0` and `valid_to` set; the new placing is current with `supersedes_id` pointing at the old one; exactly one `reallocated` history row exists; the unique index is never violated; analytics before and after differ as expected; replaying the same input changes nothing; rebuilding from raw data gives the same result.
6. Unknown country or sport goes to quarantine with a reason and does not stop the run.
7. Re-running ingestion yields no change.
8. Reconciliation mismatch is reported, not hidden.
9. Invariants: sum(country_sport totals) == total medals; per-country shares sum to 1; gender parts sum to All.
10. HHI, RCA, conversion rate match the golden expected values; division by zero returns null.
11. Parser raises a clear error when the page structure changes (the parser must not guess).
12. Rate limiter enforces the minimum interval (with a fake clock).
13. Manual CSV import follows the same validation path.
14. Snapshot diff detects a new country medal, a changed placing and a new sport for a country.
15. Raw version: same URL and same hash gives no new version, a fetch logged `unchanged`, and no re-parse.
16. Raw version: same URL and a new hash gives `version_no + 1`, an updated latest version, and a parse.
17. Raw version: content reverts A to B to A. No duplicate version, fetch logged `reverted`, latest points back to the older version, and the re-parse reproduces the earlier data.
18. Conflict policy, one test per row of the table in `DATA_PIPELINE.md` section 7: fresh official wins; stale official plus a secondary correction is held, flagged `disputed`, with an immediate re-fetch; two non-official sources disagreeing is flagged; `sie resolve-conflict` clears the flag and stores the note.
19. Snapshot identity: no new `change` snapshot when the fingerprint is unchanged; a new one when it changes or the analytics version changes; exactly one `daily` snapshot per competition-timezone day; a rebuild reproduces the same fingerprints.
20. Architecture rules: adapters do not import parsers; parsers do not import `httpx`, `sqlalchemy` or `sie.db`.
21. Domain invariants I1 to I6 from `DOMAIN_MODEL.md`, including that closed placings count in no metric.
22. A placing whose entrant belongs to another country is quarantined; an entrant credited to two countries is quarantined as `MULTI_COUNTRY_ENTRANT`.
23. Ties are stored with `is_tie`, count correctly, and never overwrite another placing.
24. A sweep (one country, two placings in one event) counts as two country medals.

### 5a. Operational foundations (Phase 6, built)
`tests/unit/test_ops_contract.py` (categories, retry rules, outcome table, freshness classification, fingerprint) and `tests/integration/test_ops_foundation.py` (each failure category on real PostgreSQL, raw evidence untouched by a fetch failure, retries idempotent, run summary, freshness through failure and recovery, stuck runs).

### 5b. Scheduler, retries and locking (Phase 6A, built)
`tests/unit/test_scheduler_contract.py` (the locked retry numbers, backoff, lock keys) and `tests/integration/test_scheduler.py`: successful run, fetch retry and exhaustion, load retry once, no retry for parse/validation/raw-store, lock exclusivity per source, a duplicate run skipped while another is active, five simultaneous runs producing one, lock released after failure and after a setup error, lock released when the session dies, rerun after a failed run, stuck-run closing. Removing the lock makes the concurrency tests fail.

### 5c. Health and alerts (Phase 6B, built)
`tests/unit/test_alert_contract.py` (each alert condition, the threshold boundary, ordering, keys, partitioning, notifier behaviour incl. a raising and a lossy channel) and `tests/integration/test_health.py` on real PostgreSQL: fresh, stale, failing (no alert at 1 failure, alert at 3), never succeeded, recovery, stuck runs, several independent sources, determinism, no false alert, and the `sie health` exit codes.

### 5d. Backup, publish, delivery (Phase 6C/6D, built)
`tests/integration/test_backup.py` (real pg_dump/pg_restore: round trip, tampered dump, count mismatch, corrupted blob, missing manifest, `fs` warning, pruning, no partial files, scratch DB always dropped), `tests/integration/test_publish.py` (contents, byte-identical output, fingerprint, nothing internal exported, refusal keeps old bundle, tamper detection, CLI) and `tests/unit/test_alert_delivery.py` plus new `test_health.py` cases (dedup, resolve and return, retry after failed delivery, reminders, webhook against a local HTTP server, first-failure alert).

### 5e. Portal fetcher (ADR-031, built)
`tests/unit/test_portal_fetch.py` (no network: URLs requested, only public endpoints, User-Agent and placeholder refusal, 2-second gap, 429/5xx/404/302 stop at once, discipline codes validated, all-or-nothing, undecodable and oversized bodies, no redirect following, byte-identical capture) and `tests/integration/test_portal_refresh.py` (real PostgreSQL, fake portal: load then unchanged, three recorded fetch failures with no data change, last good data kept, kill switch and placeholder User-Agent exit 2 and record nothing, CLI end to end). Never run against the live portal in CI.

## 6. Quality gates
- CI (GitHub Actions) runs on every push: `ruff check`, `ruff format --check`, `pytest --cov`, migration test.
- Minimum coverage: 85 percent for `pipeline/` and `analytics/`.
- A change is mergeable only when all gates pass.

## 7. Source health checks (scheduled, separate from CI)
A daily job fetches one known stable page per source and checks that the parser still works. On failure it alerts the owner, so structure changes are noticed before data goes stale.

## 8. Manual acceptance (end of each phase)
Run the phase's acceptance steps in `ROADMAP.md` and save the output as evidence in `docs/evidence/`.

## 9. Dashboard smoke test (browser)
`tests/dashboard_smoke.py` opens the page in Chromium (Playwright) and fails on a blank page, any page or console error, any failed or non-2xx request, any request that leaves the page's own origin, missing headings, horizontal overflow on mobile, and numbers that disagree with `reports/placings.csv`. All expected numbers are derived from that CSV, never typed in. It covers all seven routes at desktop (1280 px) and mobile (390 px, touch) in light and dark colour schemes, checks the navigation, the overview figures, the medal table rows (top 10, 40 countries, 49 sports, first 100 medals in the explorer, each from the data), the top country row, the dark and light background colours, and that the theme toggle flips the theme. `tests/unit/test_dashboard_smoke.py` proves it passes on the real page and fails on nine broken variants (blank page, the PR #14 apostrophe syntax error, runtime error, console error, external request, mobile overflow, wrong numbers, missing section, a dark scheme that is not dark). `test_dashboard_pages.py` and the `node --check` test remain.

**Reproduce locally**
```
pip install -e ".[dev,reports,browser]" && playwright install chromium
python -m sie.dashboard                               # rebuilds site/index.html from reports/
python tests/dashboard_smoke.py --url file://$PWD/site/index.html --screenshots shots/
pytest tests/unit/test_dashboard_smoke.py tests/unit/test_dashboard_pages.py
REQUIRE_BROWSER=1 pytest tests/unit/test_dashboard_smoke.py   # fail instead of skip without a browser
```
**Against the deployed site (owner, needs internet)**
```
python tests/dashboard_smoke.py --url https://researchflow.yourentertainments10.workers.dev/ --screenshots shots/
```
Passing locally says nothing about the deployment. Only a run of the second command, or opening the live URL by hand, verifies the deployed page; record its result in `ACCEPTANCE.md`.

**Manual mobile and dark-mode check (for what a script cannot judge)**
1. Open the live URL on a phone, or in desktop Chrome DevTools device mode at 390 x 844.
2. Visit Overview, Countries, Sports, Gender, Timeline, Data Explorer, Methodology (the `#/route` links in the navigation). No page should scroll sideways, text should not be cut off, the medal table columns that hide on narrow screens are expected.
3. Switch the operating system or browser to dark mode and reload; then press the sun/moon button to flip it. Charts, tables and text must stay readable and the choice must persist on reload.
4. Open DevTools Console: no red errors, and the Network tab shows only the page itself.

**Limits.** It does not judge visual quality (the screenshots are for a human to look at), uses Chromium only (no Safari or Firefox), does not exercise every filter combination or tooltip, and cannot see Cloudflare-side problems such as an old deployment unless pointed at the live URL.
