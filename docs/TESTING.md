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

## 6. Quality gates
- CI (GitHub Actions) runs on every push: `ruff check`, `ruff format --check`, `pytest --cov`, migration test.
- Minimum coverage: 85 percent for `pipeline/` and `analytics/`.
- A change is mergeable only when all gates pass.

## 7. Source health checks (scheduled, separate from CI)
A daily job fetches one known stable page per source and checks that the parser still works. On failure it alerts the owner, so structure changes are noticed before data goes stale.

## 8. Manual acceptance (end of each phase)
Run the phase's acceptance steps in `ROADMAP.md` and save the output as evidence in `docs/evidence/`.
