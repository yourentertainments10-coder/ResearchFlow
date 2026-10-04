# Architecture

## 1. Overview
A batch data pipeline plus an analytics layer plus a read-only dashboard. No web backend with user accounts is needed in v1.

Terminology (event, medal placing, entrant, country medal) is defined in `DOMAIN_MODEL.md` and used exactly that way everywhere.

```
 Public sources (official site/API, sports pages, manual CSV)
                    |
        [1] Source Adapters  (one per source; fetch only)
                    |
        [2] Raw Store        (immutable objects: local disk or S3-compatible bucket; raw_* tables index them)
                    |
        [3] Source Parsers   (one per source; pure; raw -> structured rows)
                    |
        [4] Normaliser       (country/sport/discipline/gender via reference tables)
                    |
        [5] Validator        (rules; failures -> quarantine)
                    |
        [6] Loader           (transactional, idempotent upsert)
                    |
              PostgreSQL       (managed service in production; DATABASE_URL)
                    |
        [7] Analytics Engine (SQL views + pandas; writes snapshots)
                    |
         +----------+-----------+
         |                      |
   [8] Dashboard           [9] Reports/Exports
   (Streamlit)             (CSV, Excel, charts, insight text)
                                 |
                       [10] Optional LLM explainer
```

## 2. Stack (all free)
| Layer | Choice | Why |
|-------|--------|-----|
| Language | Python 3.11+ | Data work, scraping, one language end to end |
| HTTP | `httpx` | Timeouts, retries, HTTP/2, async-capable |
| HTML parsing | `selectolax` or `beautifulsoup4` | Fast, simple |
| JS-rendered pages | `playwright` (only if no JSON endpoint exists) | Last resort; heavy |
| Data | `pandas` | Aggregations, pivots |
| DB | **PostgreSQL 15+** (ADR-015) | Relational integrity, partial indexes, composite FKs, transactions, JSONB; managed free and paid options on every host |
| DB driver | `psycopg` 3 | Current, maintained Postgres driver for SQLAlchemy 2 |
| Raw store | Local disk, or S3-compatible object storage such as Cloudflare R2 (ADR-017) | Stateless runners cannot keep files; the interface hides the backend |
| SQL and migrations | `SQLAlchemy 2` (Core, explicit SQL for the critical procedures) + `Alembic` (explicit-SQL migrations) | Full control of the constraints that matter; `DATABASE_URL` is the only switch between environments |
| Validation | `pydantic` v2 | Typed models for parsed rows and settings |
| CLI | `typer` | `sie ingest`, `sie analyze`, etc. |
| Dashboard | `streamlit` + `plotly` | Fast interactive pages, free hosting option |
| Scheduling | GitHub Actions cron, or OS scheduler (state lives in Postgres, so an empty runner is fine) | Free |
| Tests | `pytest`, `pytest-cov`, `hypothesis` (optional) | |
| Quality | `ruff`, `pre-commit` | |
| Logging | standard `logging` with a JSON formatter | |

Power BI Desktop is an optional alternative front end: it connects to PostgreSQL directly with a read-only role, or reads the exported CSV.

## 3. Repository layout
```
sie/
|-- README.md
|-- AGENTS.md
|-- docs/                      (these documents)
|-- pyproject.toml
|-- .env.example
|-- .gitignore
|-- alembic/                   (migrations)
|-- data/
|   |-- raw/                   (immutable; gitignored except small fixtures)
|   |-- reference/             (committed CSV mappings: countries, sports, aliases)
|   |-- manual/                (manual CSV imports)
|   `-- exports/               (generated; gitignored)
|-- src/sie/
|   |-- config.py              (settings from env)
|   |-- logging_setup.py
|   |-- db/                    (session.py, placings.py = reallocation procedure, sql/001_initial_schema.sql)
|   |-- storage/               (Phase 3: RawStore protocol; local-disk and S3/R2 backends)
|   |-- sources/               (base.py: SourceAdapter and SourceParser protocols)
|   |   `-- <source_name>/     (adapter.py = fetch only, parser.py = parse only)
|   |-- pipeline/
|   |   |-- fetch.py  parse.py  normalise.py  validate.py  load.py  reconcile.py
|   |   `-- runner.py          (orchestrates one run, writes ingest_runs)
|   |-- analytics/
|   |   |-- metrics.py         (pure functions; one per metric)
|   |   |-- views.sql
|   |   |-- snapshots.py  changes.py  insights.py
|   |-- reporting/             (exports.py, excel.py)
|   |-- llm/                   (optional explainer; off by default)
|   `-- cli.py
|-- dashboard/
|   |-- app.py
|   `-- pages/                 (one file per page)
|-- tests/
|   |-- unit/  integration/  fixtures/
`-- .github/workflows/         (ci.yml, refresh.yml)
```

## 4. Component responsibilities
| Component | Does | Must not |
|-----------|------|----------|
| Source adapter (one per source) | Fetch bytes and metadata for a given date or sport; return `RawDocument` | Parse anything, import its parser, touch the database |
| Raw store | Save bytes under the key `<source>/<date>/<timestamp>_<hash>` through the `RawStore` interface (local disk or bucket); insert raw rows | Modify or delete existing objects |
| Source parser (one per source) | Turn one raw document into `ParsedResult` objects. Pure function: no network, no database | Fetch, normalise names, guess missing fields |
| Normaliser | Map to canonical ids through reference tables | Invent new canonical values silently |
| Validator | Apply rules; split into valid and quarantined | Fix data silently |
| Loader | Upsert valid rows in one transaction | Run partially |
| Analytics engine | Compute metrics from DB; save snapshots | Fetch from the web |
| Dashboard | Read database and snapshots; render | Contain metric formulas |
| LLM explainer | Rephrase computed insight text | Add or change numbers |

## 5. Data flow of one run
1. `runner` creates an `ingest_runs` row (status `running`).
2. For each configured adapter: fetch changed documents (conditional requests with ETag or Last-Modified where supported, otherwise compare content hash).
3. Save raw documents using the version rules in `DATA_PIPELINE.md` section 4: same URL and same hash is logged as `unchanged` and not re-parsed; same URL and a new hash creates a new raw version.
4. Parse, normalise, validate. Quarantine failures with reasons.
5. Load valid rows in one transaction. Placing changes follow the reallocation procedure in `DATABASE.md` section 6 and are recorded in `placing_history`.
6. Reconcile against the official medal table. Store the result in `reconciliation_results`.
7. Recompute analytics. Write a `change` snapshot only if the data fingerprint changed (plus one `daily` snapshot per competition-timezone day), then compute changes versus the previous change snapshot (`DATABASE.md` section 8).
8. Export CSV and update the dashboard data. Mark the run `success` or `failed` with an error summary.

## 6. Adapter and parser interfaces (fetch and parse are separate)
```python
class SourceAdapter(Protocol):  # fetch only: network access, nothing else
    name: str

    def list_documents(self, since: datetime | None) -> list[DocumentRef]: ...
    def fetch(self, ref: DocumentRef) -> RawDocument: ...  # bytes + url + headers + fetched_at


class SourceParser(Protocol):  # parse only: pure function over saved bytes
    source: str

    def parse(self, doc: RawDocument) -> list[ParsedResult]: ...
```
Rules:
- The pipeline fetches through the adapter, saves the raw version, then calls the parser registered for the same source name, always on the *saved* raw bytes. This is what makes parsing reproducible and testable on fixtures.
- An adapter never imports its parser. A parser never imports network or database code. This is enforced by an automated import test (`TESTING.md`).
- Adding a source means one adapter class, one parser class and one config entry.
- Source priority in config is only a tie-break input to the conflict policy (`DATA_PIPELINE.md` section 7). It never overrides data automatically.

## 7. Dashboard pages
1. **Overview**: medal table, completeness gauge, latest changes.
2. **Country x Sport**: heatmap and sortable table with gender filter.
3. **Country profile**: sport breakdown, share bars, tier labels, RCA, men vs women.
4. **Women's analysis**: women's share per country, strongest women's sports per country.
5. **Sport view**: who dominates a sport, market share, events won and available.
6. **Compare**: two to five countries side by side.
7. **Concentration**: HHI, effective sports, top-3 share scatter.
8. **Trends**: standings and metric changes across snapshots.
9. **Data quality**: reconciliation, quarantine, source freshness, run history.

## 8. Configuration
Environment variables, loaded by `config.py`: `DATABASE_URL` (provider URLs such as `postgres://...?sslmode=require` are accepted), `DATA_DIR`, `COMPETITION_ID`, `HTTP_USER_AGENT`, `HTTP_MIN_INTERVAL_SECONDS`, `SOURCE_PRIORITY` (tie-break input only), `FRESHNESS_THRESHOLD_MINUTES` (default 30 while the competition runs; see `DATA_PIPELINE.md` section 7), `LOG_LEVEL`, optional `LLM_API_KEY`, `NOTIFY_WEBHOOK_URL`. Phase 3 adds `RAW_STORE` (`local` or `s3`), `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.

## 9. Failure handling
| Failure | Behaviour |
|---------|-----------|
| Network error or timeout | Retry with exponential backoff, then fail the document only |
| Site structure changed (parse error) | Fail loudly, keep old data, alert; never guess |
| Unknown country or sport | Quarantine row, show in Data quality page |
| Source disagreement | Conflict policy table in `DATA_PIPELINE.md` section 7: a fresh official value is preferred; stale or competing values are held, the event is flagged `disputed` and queued for review. Never a silent overwrite |
| Run crash mid-way | Transaction rolled back; next run resumes safely |
| Official table mismatch | Run succeeds but the dashboard shows a red reconciliation warning |

## 10. Scaling path
Designed so growth is additive, not a rewrite:
- **More competitions:** new `competitions` row, new source adapter and parser, reference CSVs. No core change.
- **More load or users:** a read-only database role and provider connection pooling, then a read replica for the dashboard; scheduled work moves from a cron job to a worker queue only if runs start overlapping.
- **Real user accounts, roles, an API:** add a FastAPI service over the same database (the analytics functions are already pure and reusable); the rules for authentication are already written in `SECURITY.md` section 7. The Streamlit dashboard can stay as the internal tool.
- **More data (athlete-level, entries, live results):** new tables by migration; the four-concept domain model (`DOMAIN_MODEL.md`) already separates entrants from placings.
- **Different front end:** the dashboard never contains formulas, so React or Power BI can replace it.
Stage-by-stage growth plan and the rules that keep it possible: `SCALABILITY.md`.
