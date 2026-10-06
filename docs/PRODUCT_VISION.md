# Product Vision

> Status legend used in this file: **Built** (in the repository and tested), **Planned** (designed here, not built), **Not planned yet** (an idea, no design commitment).

## 1. What ResearchFlow is

ResearchFlow (repository name; the code package is `sie`, "Sports Intelligence Engine") is meant to become a **competition-agnostic sports research and intelligence engine**: collect official results, keep them verified and traceable, compute deterministic analytics, and eventually answer constrained research questions with evidence.

**Asian Games 2026 (Aichi-Nagoya) is the first production dataset and the validation target. It is not the product boundary.** Nothing in the core data model, analytics or research logic may depend on it being the Asian Games.

## 2. What exists today (accurate status)

| Capability | Status |
|---|---|
| Event-level Asian Games 2026 medal data, reconciled to the official table (469 events, 1,568 medals, 0 mismatches) | Built |
| PostgreSQL schema scoped by `competition_id`, loader, raw store, quarantine | Built (Phase 1 and 2) |
| Deterministic analytics (shares, concentration, specialisation, gender) | Built as pandas reports; the canonical `v_medal_facts` path is **Phase 4, in progress** |
| Static dashboard, Excel and HTML reports | Built (ahead of the original Phase 5 plan) |
| A second competition | **Planned** (Phase 7) |
| Cross-competition analytics | **Planned** (Phase 7) |
| Research query layer (questions in, evidence-backed answers out) | **Planned**, after Phase 7 |
| LLM explanation layer | **Planned** (Phase 7), strictly constrained, see section 6 |

ResearchFlow does **not** currently answer arbitrary research questions. The dashboard is a fixed set of pages over verified data.

## 3. Multi-competition foundation (Planned)

`competition_id` is the boundary between competitions. Every competition-owned table already carries it (`DATABASE.md`).

```
ResearchFlow
├── Asian Games 2026      (built, first production dataset)
├── Asian Games 2022      (planned candidate)
├── Olympics 2028         (possible)
├── Commonwealth Games    (possible)
└── other supported competitions
```

### Extension contract

Adding a competition should require **only**:

1. **Competition metadata**: a `competitions` row (name, edition, dates, timezone, host).
2. **Source adapter**: fetch only, writes raw bytes to the raw store (`ARCHITECTURE.md` section 6).
3. **Parser/normaliser configuration**: turns that source's raw documents into the canonical placing rows. All source-specific quirks live here and nowhere else.
4. **Reference mappings**: that competition's source codes mapped to canonical country, sport and discipline IDs (`data/reference/`).
5. **Validation rules where needed**: for example the source's own official table to reconcile against.

It must **not** require changes to the analytics functions, the canonical schema or the dashboard logic. If a competition needs one, the contract is broken and the change is an architecture decision (ADR), not a quiet patch.

Canonical IDs, not names: sport names, country names, event names and source schemas will differ between competitions, so cross-competition work joins on canonical IDs and reference mappings, never on display strings.

### Proof before claim

The system is not described as multi-competition until a **second-competition fixture** proves, in tests, that:

- a second competition can be registered;
- its data enters the same canonical model;
- the same analytics functions run on it unchanged;
- all source-specific parsing stays inside the adapter/parser layer;
- Asian Games 2026 results are byte-for-byte unchanged.

This is an architectural proof, not a UI feature, and a small fixture is enough. We do not add a competition for demonstration.

## 4. Cross-competition analytics (Planned, Phase 7)

Only after the proof above: for example India 2022 vs 2026, women's performance across editions, sport-level change, concentration change, countries entering or leaving the medal table. These need normalised concepts (canonical country/sport IDs, comparable event definitions) and must say when two editions are not comparable (a sport added or dropped).

## 5. Research query layer (Planned, after Phase 7)

Goal: instead of navigating pages, a user asks a question such as "Which countries depend most on one sport?" or "Compare India and China." The layer **compiles the question into a deterministic plan** and executes it with the existing analytics:

```
question → intent → competition/data needed → filters
        → analytical operations → evidence → answer
```

### Internal query contract (concept only)

The plan is a typed, testable object. Conceptually it names: the competition(s), the entities being compared, filters, dimensions, metrics (from the canonical metric definitions in `ANALYTICS_SPEC.md`), a time range, and whether evidence is required. **The exact shape is not fixed.** It will be designed against the analytics API as it exists when Phase 4 is complete, and recorded in an ADR then.

Why a plan object: the same question must always compile to the same plan and produce the same numbers, so it can be unit-tested without any model.

### Constraints

- Start with a **small, enumerated set of supported question types**. No unrestricted "ask anything" agent.
- The layer **never invents numbers**. Every number comes from the canonical database or deterministic analytics.
- An unsupported or under-evidenced question gets an explicit "cannot answer from verified data", not a guess.

## 6. Evidence and provenance (Planned)

Five layers are kept separate and are never mixed in storage or in an answer:

| Layer | What it is | Example |
|---|---|---|
| Raw source data | Bytes exactly as fetched, with URL, time, hash | the portal's JSON response |
| Normalised data | Canonical rows after parsing and validation | a placing: event, country, medal |
| Analytical result | Deterministic computation over normalised data | India's share of medals in Athletics |
| External evidence | Information from outside sources, with source and retrieval date | a federation statement |
| Generated explanation | Language written about the above | "India's medals lean on Athletics" |

Source priority for evidence: (1) official structured source/API, (2) official web page or data, (3) reputable secondary source, (4) search/discovery result, (5) user-provided evidence. A higher tier wins a conflict; conflicts are reported, never hidden.

Today, layers 1 to 3 exist (`DATA_PIPELINE.md`, `DATABASE.md`). External evidence and generated explanation do not.

### Shape of a future answer

An answer separates **Verified facts** (directly in the database), **Derived analysis** (computed by ResearchFlow), **Interpretation** (reasoning from the first two) and **External evidence** (cited outside sources), and states uncertainty or conflicts. "India won 85 medals" is always accompanied by the underlying verified rows and their source.

## 7. Role of AI (Planned, constrained)

AI is an **explanation and orchestration layer, never the source of truth.**

```
question → query planner → research/data tools → canonical database
         → deterministic analytics → evidence retrieval
         → verified result → LLM explanation
```

An LLM must not: invent statistics, modify database facts, replace deterministic analytics, hide source conflicts, or present guesses as verified facts. If evidence is insufficient the system says so. Any generated text that contains a number is checked against the verified result before it is shown (the "number lock" in `ROADMAP.md`). This keeps the project principle "code computes, the LLM only explains".

## 8. Phasing

The roadmap numbering in `ROADMAP.md` is unchanged.

| Phase | Focus | Status |
|---|---|---|
| 4 | Analytics from `v_medal_facts`, canonical metrics, snapshots, change detection, exports, rule-based insights | **Current priority** |
| 5 | Dashboard, only where it serves the verified analytics model; no speculative AI UI | Mostly built, polish continues |
| 6 | Automation and operations: schedule, ingest, validate, reconcile, analytics, snapshot, publish, notify | Planned |
| 7 | Expansion: second competition (with the proof in section 3), cross-competition analytics, LLM explanation, multilingual reports, athlete-level data if available. Each item has its own acceptance criteria and is not done all at once | Planned |
| After 7 | Research query layer, starting with a constrained question set | Not planned yet beyond this contract |

Nothing in sections 3 to 7 is built by the work that produced this document; it only fixes the direction so later phases do not drift.
