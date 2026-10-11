# Generalisation audit: from a sports engine to a general research and analytics engine

Status: **audit only, nothing implemented.** Date: 2026-10-11, repository state `main` at `617f8f9`.
Written for the owner's stated goal (below). Decisions needed are in ADR-039 (Proposed) in `DECISIONS.md`.

## 1. The goal, as the owner described it
One input box. The user asks anything (an Asian Games analysis, a sales CSV, an EV market question, a
technology explained). The system decides what is needed, collects data from uploads, the web, APIs and
documents, validates it, and answers with evidence: traceable calculations, cited sources, stated
conflicts, stated limits, follow-up conversation, plans for big tasks. Three modes in one interface:
data analytics, deep research, general questions. Asian Games is the first use case, not the boundary.

## 2. Where this stands against ADR-024
ADR-024 (2026-10-05) says the product is a competition-agnostic **sports** research engine, with a later
research query layer limited to an enumerated set of questions, and AI that only explains verified
results. The goal above is wider on three axes: any domain (not only sports), any input (uploads and web,
not one portal and a fixed CSV), and an open-ended conversational entry. Under `AGENTS.md` rule 1 and
the "no silent architecture changes" rule this needs an approved decision first (ADR-039).

## 3. What is already generic (reuse as the foundation)
Checked by reading the modules: these mention a competition at most, never medals or placings.

| Component | Where | Why it is reusable |
|-----------|-------|--------------------|
| Raw evidence store: URL, retrieval time, content hash, immutable, versioned | `pipeline/raw.py`, tables `raw_documents`, `raw_fetches`, `raw_versions`, `raw_blobs` | Any fetched or uploaded document can be stored the same way |
| Failure contract and retry rules | `pipeline/failure.py` | Categories are about fetch/parse/validate, not sport |
| Scheduler with per-source advisory lock and stuck-run recovery | `pipeline/scheduler.py`, `db/locks.py` | Source-agnostic |
| Freshness, health, alerts, de-duplication, notification channels | `pipeline/freshness.py`, `health.py`, `alerts.py`, `alert_state.py`, `notify.py` | Keyed by source and `competition` id only; "competition" would need to become a generic project/dataset id |
| Run history, observation of sources | `ingest_runs`, `pipeline/observe.py` | Same |
| Encrypted backup, restore proof, publish bundle | `ops/backup*.py`, `ops/publish.py` | Database-level, not domain-level |
| Settings, structured logging | `config.py`, `logging_setup.py` | Generic |
| Engineering rules (traceability, no fabrication, idempotence, quarantine, parametrised SQL) | `AGENTS.md` | These are the product's quality promise; they apply to any domain |
| Concepts: conflict policy, quarantine, reconciliation against an official figure, snapshots | `pipeline/conflicts.py` (policy), tables `quarantine`, `reconciliation_results`, `analytics_snapshots` | The ideas are generic; the implementations are not (next section) |

## 4. What is tightly coupled to sports and medals
| Component | Coupling |
|-----------|----------|
| Schema | `sports`, `disciplines`, `events`, `entrants`, `placings`, `placing_history`, `countries`, `country_aliases`, `sport_aliases`; `competitions` carries `official_event_total` |
| Source contract | `sources/base.py::ParsedResult` has fields `sport, discipline, event, gender, medal, country, entrant, slot, is_tie` |
| Normalise and validate | `pipeline/normalise.py`, `validate.py`, `models.py::Reason` (medal, country, sport, gender, slot reasons) |
| Load | `pipeline/load.py`, `db/placings.py`, `db/conflicts.py` (conflicts are about a placing's country) |
| Analytics | `analytics/*` (standings, event completion, medal mix, gender, insights) and the twelve-table spec in `ANALYTICS_SPEC.md` |
| Sources | `sources/bornan/*` (one portal), `sources/manual/parser.py` (the medal CSV format) |
| Outputs | `dashboard.py`, `web/dashboard.html`, `site/`, `reports/`, `ops/acceptance.py` (fixed expected medal figures) |
| Domain language | `DOMAIN_MODEL.md` (event, medal placing, entrant, country medal) |

In short: the operating layer (ingestion plumbing, scheduling, health, alerts, backups) is generic or nearly so; the data
model, loader, analytics and every presentation are sports-specific.

## 5. What is missing for the goal
| Capability | Today | Note |
|------------|-------|------|
| Universal input box and conversation | none | Needs a backend; the site is static |
| Intent and task planner | none | ADR-024 point 3: start with a small enumerated set of plans, not an open agent |
| User uploads (CSV, Excel, PDF, JSON) with profiling | only the medal-format CSV | A generic tabular dataset model is needed (profile, types, cleaning with a log) |
| Generic analytics over arbitrary tables | none | Must stay deterministic (pandas or SQL), numbers never from a model |
| Web research: search, fetch, extract, cite | one fixed portal fetcher | `AGENTS.md` rule 9 (robots, rate limit, honest client) already governs this |
| Evidence ledger for claims (source, quote, time, confidence) | raw store only | A claim table is needed; web claims are *cited evidence*, not verified data |
| Cross-source conflict reporting for non-sports claims | placing conflicts only | Generalise the policy to claims |
| LLM layer (plan, summarise, explain) with number lock | none (Phase 7 planned) | Model never computes or edits facts (ADR-024 point 5) |
| Report and chart generation per question | fixed dashboard | |
| Follow-up memory | none | Conversation state tied to stored findings |
| Cost, quota and abuse controls | none | New once a paid model or search API is involved |
| Authentication and per-user data separation | none (public static site) | Needed as soon as users upload files |

## 6. How to add it without breaking production
1. Keep the sports path and its tests as they are; they become the first "domain pack". The Asian Games
   outputs must stay byte-identical, which the existing golden and acceptance tests already check.
2. Add new modules beside the old ones (no rename of tables or folders). A generic `dataset`/`claim`
   model is new tables through new migrations (rule 6), not edits of 001 to 004.
3. Introduce the generic layer in the order that costs nothing first (section 8).
4. Prove generality the way ADR-024 asks: a second, non-sports fixture runs through the same generic
   path while the Asian Games outputs stay unchanged.

## 7. Cost, stated plainly
Free or already free: GitHub Actions on a public repository, Cloudflare static hosting, Neon free plan
within its limits, `age`, open-source libraries, deterministic analytics on a user's file.
Likely paid or limited: a hosted language model (billed by use); a web search API (small free quota,
then paid); an always-on chat backend (free tiers exist with limits). Official sites and open APIs can
be fetched directly at no cost. These are categories, not prices: current prices and free limits were
not checked for this audit and must be looked up when a provider is chosen.

## 8. Suggested order (each step is its own proposal and PR, none started)
1. Decide scope (ADR-039).
2. Generic tabular analytics for an uploaded file: profile, clean with a log, deterministic calculations,
   downloadable results. No model, no cost, no network. This also forces the generic data model.
3. Generic claim and evidence ledger; generalise freshness, conflicts and health from `competition` to a
   project id.
4. Web research with citations, within `AGENTS.md` rule 9, using official sites and open APIs first.
5. Language-model layer: planner and explainer behind a number lock, with a spend limit.
6. Conversation and backend, with authentication.
