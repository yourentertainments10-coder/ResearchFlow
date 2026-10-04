# Sports Intelligence Engine (SIE)

A self-running data system that collects multi-sport competition results from the web, stores them as clean event-level data, runs deterministic analytics, and shows the findings on a dashboard. First target: **Asian Games 2026 (Aichi-Nagoya)**. The design is competition-agnostic, so Olympics, Commonwealth Games or any other multi-sport event can be added later by writing one new source adapter and parser.

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
Planning reviewed (docs-0.2.0). Implementation not started. The design-freeze checklist in `docs/ROADMAP.md` (Phase 1) must pass before migration 001 is written.
