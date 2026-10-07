# Sports Intelligence Engine (SIE)

ResearchFlow is being built as a **competition-agnostic sports research and intelligence engine**: it collects official multi-sport results, stores them as clean event-level data, runs deterministic analytics and shows the findings on a dashboard.

**Asian Games 2026 (Aichi-Nagoya) is the first production dataset and validation target, not the product boundary.** The design keeps competitions separate by `competition_id`, so another competition is meant to be added through its own adapter, parser configuration and reference mappings, not by changing the core. That second competition and the proof it needs are **planned, not built** (see `docs/PRODUCT_VISION.md`).

**Not yet supported:** answering arbitrary research questions, multiple competitions, and any LLM layer. Today the system provides the verified Asian Games 2026 data, deterministic analytics and a fixed-page dashboard. The immediate engineering priority is Phase 4 (analytics).

## What it answers
- Which country wins medals in which sports (full matrix, not only a top 10).
- Men / Women / Mixed breakdowns for every country and sport.
- Where a country is specialised and where it is weak (concentration, revealed comparative advantage, conversion rate against available events).
- How standings and strengths change day by day.

## Core principle
**Code computes, the LLM only explains.** Every number comes from event-level rows in the database, each row traceable to a source URL and a retrieval time. An LLM is optional and never the source of truth.

## Cost
Everything in the core system is free: Python, pandas, PostgreSQL (Neon free plan or any Postgres), Streamlit, GitHub, GitHub Actions. Free plans have limits, see `docs/DEPLOYMENT.md`. Optional extras (LLM API, paid sports-data API, custom domain) are clearly marked optional.

## Documents (read in this order)
| # | File | Purpose |
|---|------|---------|
| 0 | `docs/PRODUCT_VISION.md` | Long-term direction: multi-competition, research queries, evidence, role of AI (planned vs built) |
| 1 | `docs/PRODUCT_REQUIREMENTS.md` | What we build and how we know it is done |
| 2 | `docs/DOMAIN_MODEL.md` | Formal definitions: event, medal placing, entrant, country medal |
| 3 | `docs/ARCHITECTURE.md` | Components, stack, folder layout, data flow |
| 4 | `docs/DATA_PIPELINE.md` | Sources, discovery, collection, versioning, cleaning, validation, conflicts |
| 5 | `docs/DATABASE.md` | Schema, versioning, snapshots, migrations |
| 6 | `docs/ANALYTICS_SPEC.md` | Exact metric definitions and outputs |
| 7 | `docs/TESTING.md` | Test strategy and quality gates |
| 8 | `docs/SECURITY.md` | Secrets, scraping ethics, future auth rules |
| 9 | `docs/DEPLOYMENT.md` | Where data lives, free hosting and automation |
| 10 | `docs/ROADMAP.md` | Phases, design-freeze gate, acceptance criteria |
| 11 | `docs/AI_PROMPTS.md` | Ready prompts for an AI coding agent |
| 12 | `docs/DECISIONS.md` | Architecture decision records |
| - | `AGENTS.md` | Engineering rules every AI agent must follow |
| - | `CHANGELOG.md` | History of changes to the project and its docs |

## How to use with an AI coding agent
1. Put this whole folder in a new Git repository.
2. Tell the agent: "Read `AGENTS.md` and everything in `docs/` first. Do not write code yet."
3. Work through `docs/ROADMAP.md` one phase at a time, using the prompts in `docs/AI_PROMPTS.md`.
4. Phase 0 (source discovery) is a gate. Nothing else starts until it is done.

## Status
Asian Games 2026 (Aichi-Nagoya) medal data: 469 events, 59 disciplines in 49 sports, 1568 medals, reconciled with the official table (0 mismatches) by the report code. The ingestion foundation (Phase 2) is built for placings; the event-level analytics now read the database (`reporting.medal_facts`), but the committed `site/` and `reports/` were produced before that and have not been regenerated, so the project is **not** end to end yet.

| Part | State |
|------|-------|
| PostgreSQL schema, roles, migrations | done (Phase 1) |
| One ingestion path (`sie load-capture`, `sie import-csv`): raw store, quarantine, validator, `ingest_runs` | done, idempotent, reallocation-aware (ADR-021) |
| Entrants (names) stored | not built; the portal parser drops names on purpose |
| Event-level analytics read `v_medal_facts` (`sie.analytics.facts`) | done and regression-tested against the verified report; `site/` and `reports/` not yet regenerated |
| Official-table report (`analytics/report.py`) | reads the official standings feed on purpose (it is the reference), not our data |
| Analytics (country, sport, gender, concentration, specialisation) | done |
| Static dashboard (`site/index.html`) and Excel/HTML reports (`reports/`) | done |
| Live fetching from the portal | built, daily schedule gated by `PORTAL_FETCH_ENABLED`; D1 cleared by the owner 2026-10-07 (ADR-031); not yet run against the live portal |
| Scheduler, backups, conflict engine, hosted Postgres | planned (Phases 3 and 6) |

### Run it
```bash
pip install -e ".[dev,reports]"
python -m sie.analytics.full_report path/to/ag2026_all_medals.json   # reports/*.xlsx, *.html, placings.csv
python -m sie.dashboard                                              # site/index.html (template: src/sie/web/dashboard.html)
sie migrate && sie seed-reference && sie load-capture path/to/ag2026_all_medals.json   # needs DATABASE_URL
sie import-csv data/manual/results.csv                                                  # manual results (docs/DATA_PIPELINE.md s9)
pytest
```
The capture is made in the owner's browser (snippet in `docs/SOURCE_DISCOVERY.md`) and is not stored in Git because it holds athlete names.

### Live dashboard
https://researchflow.yourentertainments10.workers.dev/ (Cloudflare, redeploys on every push to `main`).

### Host the dashboard free
Cloudflare (Workers static assets, config in `wrangler.jsonc`) or Pages: publish the `site/` folder. No build step; the deploy command is `npx wrangler deploy`.
