# Product Requirements

## 1. Problem
Medal tallies show only totals per country. They do not show *which sports* produce a country's medals, how men and women contribute, or how concentrated or diversified a country is. Answering this needs event-level data (every event with its gold, silver and bronze winners), plus a repeatable way to collect, clean and analyse it as a competition is still running.

## 2. Goal
Build a system that, with minimal manual work, collects event-level results, keeps them accurate and traceable, computes a full set of analytics, and presents them in an interactive dashboard and exportable reports.

## 3. Users
| User | Needs |
|------|-------|
| Owner / analyst (primary) | Run the pipeline, explore the dashboard, export tables and charts |
| Viewer (secondary, optional later) | Read-only dashboard |

There is no login in v1 (see `SECURITY.md`).

## 4. Scope
**In scope (v1):** one competition (Asian Games 2026), event-level medal data, Men/Women/Mixed, daily and intraday refresh, full analytics, dashboard, CSV/Excel export, data-quality reporting.

**Out of scope (v1):** athlete-level biographies, betting or prediction models, live commentary, user accounts, mobile app, paid data feeds.

**Later (v2+):** other competitions, athlete-level analytics, LLM written summaries, Hindi/English report generation.

## 5. Functional requirements
| ID | Requirement | Acceptance criteria |
|----|-------------|---------------------|
| FR-01 | Discover and document data sources | Phase 0 report lists each candidate source, access method, update frequency, terms/robots status, and chosen primary source |
| FR-02 | Collect event-level results | For a given day, the collector fetches all finished events and stores raw responses with URL, time and hash |
| FR-03 | Store raw data immutably and versioned | Raw files are never modified. Same URL and same hash logs an unchanged fetch; same URL and a new hash creates a new raw version (`DATA_PIPELINE.md` section 4) |
| FR-04 | Parse into structured rows | Each finished event yields gold, silver and bronze entries with country, sport, discipline, event, gender |
| FR-05 | Normalise names | Country, sport, discipline and gender values map to canonical forms via reference tables; unknown values are quarantined |
| FR-06 | Validate | Rules in `DATA_PIPELINE.md` run on every load; failures reported |
| FR-07 | Reconcile with official medal table | Country totals from event data equal the official table, or differences are listed with a reason |
| FR-08 | Manual import fallback | A CSV in the documented format can be imported with the same validation as automated data |
| FR-09 | Idempotent updates | Running ingestion twice leaves the database unchanged the second time |
| FR-10 | Daily snapshots | Analytics tables are snapshotted per refresh so changes can be compared over time |
| FR-11 | Country x Sport x Gender analytics | Full matrix for all countries, not only the top 10 |
| FR-12 | Strength and weakness profile per country | Metrics and tiers per `ANALYTICS_SPEC.md` |
| FR-13 | Women, Men, Mixed analysis | Same metrics available per gender category, with women's share per country |
| FR-14 | Concentration and diversification metrics | HHI, effective number of sports, top-3 share, RCA |
| FR-15 | Efficiency metrics | Conversion rate against events available, market share per sport |
| FR-16 | Change detection | "What changed since the last snapshot": new medals, rank moves, new sports |
| FR-17 | Data completeness indicator | Dashboard shows % of events completed overall and per sport; metrics on partial data are flagged |
| FR-18 | Dashboard | Filters: country, sport, gender, medal type, date range; pages listed in `ARCHITECTURE.md` |
| FR-19 | Export | Any table can be exported as CSV; a full Excel workbook export is available |
| FR-20 | Automated refresh | Scheduled runs without manual intervention; failures notify the owner |
| FR-21 | Auto-generated insights | Template-based text statements built from computed numbers (no LLM required) |
| FR-22 | Optional LLM explanation | If an API key is configured, an LLM can rewrite computed insights; it receives only computed tables and must not add numbers |
| FR-23 | Freshness-aware conflict handling | Source disagreements follow the policy table in `DATA_PIPELINE.md` section 7; disputed events are flagged and counted on the dashboard |

## 6. Non-functional requirements
| ID | Requirement |
|----|-------------|
| NFR-01 | Traceability: every medal row links to its source document |
| NFR-02 | Reproducibility: rebuilding the database from stored raw data yields identical analytics |
| NFR-03 | Free to run: no paid service is required for the core system |
| NFR-04 | Respect for sources: robots.txt honoured, rate-limited, cached |
| NFR-05 | Reliability: a failed run never corrupts existing data (transactional loads) |
| NFR-06 | Observability: structured logs and a run history table |
| NFR-07 | Performance: full analytics refresh completes in under 60 seconds on a laptop for ~500 events |
| NFR-08 | Testability: core logic covered by automated tests; adapters tested on saved fixtures |
| NFR-09 | Extensibility: adding a new competition or source requires a new adapter and reference data, not core changes |
| NFR-10 | Secrets never committed |

## 7. Key domain rules (must be handled correctly)
Formal definitions of *event*, *medal placing*, *entrant* and *country medal* are in `DOMAIN_MODEL.md`. The rules below use those terms.
- Each event normally has one Gold, one Silver and one Bronze placing. Combat sports often award **two bronzes**; ties can produce shared placings (`DOMAIN_MODEL.md` section 4).
- **Team events** count as one medal for the country, not one per athlete.
- **Mixed** events are their own gender category and never merged into Women or Men.
- **Open / unspecified** gender events exist (for example some equestrian or sailing classes) and need an `Open` category.
- Medals can be **reallocated** later (doping, disqualification). The history must be preserved and the current state must be correct.
- A "sport" can contain several **disciplines** (for example Aquatics contains swimming and diving). Analysis must be possible at both levels. Source naming differs, so mapping is required.
- Neutral or refugee participants may appear under non-country codes. Handle through reference data, not special-case code.

## 8. Success criteria
1. For all finished events, event-level totals reconcile with the official medal table.
2. The owner can open the dashboard and answer "which sports power country X, and how do its women compare" in under a minute.
3. A fresh machine can rebuild everything from the repo and stored raw data with documented commands.
4. The pipeline runs unattended for 3 consecutive days of the competition without manual fixes.

## 9. Assumptions and open questions
| # | Item | Resolution plan |
|---|------|-----------------|
| A1 | An official or public source exposes event-level results in a machine-readable form | Verified in Phase 0. If not, use manual CSV import plus the best public pages |
| A2 | Event totals and sport lists quoted in secondary chats are unverified | Re-derive from the source in Phase 0 |
| A3 | Terms of use of the chosen source allow this kind of collection | Check and record in Phase 0. If not, use manual import |
| Q1 | Dashboard hosted publicly or kept private? | Owner decision before Phase 5 |
| Q2 | Is an LLM explanation layer wanted? | Optional, Phase 7 |
