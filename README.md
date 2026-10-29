# ResearchFlow

**Product direction (2026-10-29):** ResearchFlow is evolving into a free-first research and visual data-analysis workspace, not a chat-window clone. Its primary screen should be a workspace of KPI cards, charts, tables, filters, data-quality notes and findings; natural language refines the analysis. The first complete generic journey is CSV upload → profiling → multiple charts → natural-language refinement → deterministic verification → export. See [WORKSPACE_PRODUCT_DIRECTION.md](docs/WORKSPACE_PRODUCT_DIRECTION.md).

**Current status:** the existing main branch is primarily the Asian Games sports pipeline and fixed dashboard. The generic visual workspace and complete CSV-to-export journey are not yet complete. Web research is on draft PR #31; live-source end-to-end acceptance and target-Windows save/reuse remain unverified. Asian Games outputs remain a regression baseline.

---

## Original sports-engine overview (implementation context)

ResearchFlow is being built as a **competition-agnostic sports research and intelligence engine**: it collects official multi-sport results, stores them as clean event-level data, runs deterministic analytics and shows the findings on a dashboard.

**Asian Games 2026 (Aichi-Nagoya) is the first production dataset and validation target, not the product boundary.** The design keeps competitions separate by `competition_id`, so another competition is meant to be added through its own adapter, parser configuration and reference mappings, not by changing the core. That second competition and the proof it needs are **planned, not built** (see `docs/PRODUCT_VISION.md`).

**Not yet supported on `main`:** answering arbitrary research questions, multiple competitions, and any LLM layer. Today the default product still provides verified Asian Games 2026 data, deterministic analytics and a fixed-page dashboard. A separate Phase 4 research prototype lives on draft PR #31; it implements free-source discovery, collection and deterministic reports, but the end-to-end live source acceptance gate has not passed and it is not merged. Current provider limitations are documented in `docs/GENERAL_RESEARCH_ENGINE_VISION.md` and `docs/FREE_SOURCE_CATALOGUE.md`.

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
| 0 | `docs/WORKSPACE_PRODUCT_DIRECTION.md` | Current product source of truth: visual workspace, CSV-first journey, source fallback, privacy, exports and roadmap |
| 1 | `docs/PRODUCT_VISION.md` | Sports-domain vision and historical status; subordinate to current workspace direction |
| 2 | `docs/PRODUCT_REQUIREMENTS.md` | What we build and how we know it is done |
| 3 | `docs/DOMAIN_MODEL.md` | Formal definitions: event, medal placing, entrant, country medal |
| 4 | `docs/ARCHITECTURE.md` | Components, stack, folder layout, data flow |
| 5 | `docs/DATA_PIPELINE.md` | Sources, discovery, collection, versioning, cleaning, validation, conflicts |
| 6 | `docs/DATABASE.md` | Schema, versioning, snapshots, migrations |
| 7 | `docs/ANALYTICS_SPEC.md` | Exact metric definitions and outputs |
| 8 | `docs/TESTING.md` | Test strategy and quality gates |
| 9 | `docs/SECURITY.md` | Secrets, scraping ethics, future auth rules |
| 10 | `docs/DEPLOYMENT.md` | Where data lives, free hosting and automation |
| 11 | `docs/ROADMAP.md` | Phases, design-freeze gate, acceptance criteria |
| 12 | `docs/AI_PROMPTS.md` | Ready prompts for an AI coding agent |
| 13 | `docs/DECISIONS.md` | Architecture decision records |
| - | `AGENTS.md` | Engineering rules every AI agent must follow |
| - | `CHANGELOG.md` | History of changes to the project and its docs |

## How to use with an AI coding agent
1. Put this whole folder in a new Git repository.
2. Tell the agent: "Read `AGENTS.md` and everything in `docs/` first. Do not write code yet."
3. Work through `docs/ROADMAP.md` one phase at a time, using the prompts in `docs/AI_PROMPTS.md`.
4. Phase 0 (source discovery) is a gate. Nothing else starts until it is done.

## Status
Asian Games 2026 (Aichi-Nagoya) medal data: 469 events, 59 disciplines in 49 sports, 1568 medals, reconciled with the official table (0 mismatches) by the report code. The ingestion foundation (Phase 2) is built for placings; the event-level analytics now read the database (`reporting.medal_facts`), and the committed `site/` and `reports/` were regenerated from the full capture on 2026-10-07 (PR #5), so the path from database to dashboard is end to end. Production (Neon) acceptance passed on 2026-10-11, 16 of 16 checks of `sie acceptance` (469 events, 1,568 placings, 470/469/629, 40 countries; output reported by the owner), see `ACCEPTANCE.md`.

| Part | State |
|------|-------|
| PostgreSQL schema, roles, migrations | done (Phase 1) |
| One ingestion path (`sie load-capture`, `sie import-csv`): raw store, quarantine, validator, `ingest_runs` | done, idempotent, reallocation-aware (ADR-021) |
| Entrants (names) stored | not built; the portal parser drops names on purpose |
| Event-level analytics read `v_medal_facts` (`sie.analytics.facts`) | done and regression-tested against the verified report; `site/` and `reports/` regenerated 2026-10-07 |
| Official-table report (`analytics/report.py`) | reads the official standings feed on purpose (it is the reference), not our data |
| Analytics (country, sport, gender, concentration, specialisation) | done |
| Static dashboard (`site/index.html`) and Excel/HTML reports (`reports/`) | done |
| Live fetching from the portal | built and running daily on a schedule (three consecutive scheduled runs succeeded 8 to 10 Oct, step level; logs not read); gated by `PORTAL_FETCH_ENABLED`; D1 cleared by the owner (ADR-031) |
| Scheduler, health alerts, conflict engine, hosted Postgres (Neon) | done (Phases 3 and 6); alert delivery to the real webhook not yet exercised |
| Encrypted backup with scratch-database restore test | done; scheduled and manual runs succeeded 10 and 11 Oct; restore with the owner's own key not yet done |
| Browser smoke test of the dashboard in CI | done (ADR-036); against the deployed site not yet run |
| Still open | see `ACCEPTANCE.md` section 8: deployed-site smoke test, webhook drill, key-based restore, ADR-032 sticky-resolution decision, Phase 7 |

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
