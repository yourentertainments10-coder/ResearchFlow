# Documentation map and maintenance rules

This file is the navigation index for ResearchFlow documentation. **Use it before editing or removing a document.** The product-level direction is `WORKSPACE_PRODUCT_DIRECTION.md`; implementation-specific designs and accepted ADRs remain authoritative for their own technical details.

## Read first

1. [Workspace product direction](WORKSPACE_PRODUCT_DIRECTION.md) — canonical product goal, first complete journey, source modes, permissions and definition of done.
2. [README](../README.md) — setup, current product status and top-level links.
3. [Engineering rules](../AGENTS.md) — mandatory workflow, approvals, testing and definition of done.
4. [Roadmap](ROADMAP.md) — delivery sequence and current gates.
5. [Architecture](ARCHITECTURE.md) — implemented architecture and boundaries.
6. [Testing strategy](TESTING.md) and [acceptance evidence ledger](../ACCEPTANCE.md) — how tests are run vs evidence from specific runs.

## Product and design

| Document | Responsibility / status |
|---|---|
| [Workspace product direction](WORKSPACE_PRODUCT_DIRECTION.md) | **Canonical current product goal.** Visual general-research and analytics workspace; CSV-first journey. |
| [Product requirements](PRODUCT_REQUIREMENTS.md) | Original product and domain requirements; keep sports-specific requirements distinct from workspace requirements. |
| [Product vision](PRODUCT_VISION.md) | Sports-domain vision and earlier product/phase context; not the canonical overall product direction. |
| [General research engine vision](GENERAL_RESEARCH_ENGINE_VISION.md) | General research engine technical vision and constraints; design context, not proof of completed features. |
| [Generalisation audit](GENERALISATION_AUDIT.md) | Historical gap audit. Retain as evidence of how scope evolved; do not use as the live status page. |
| [Dataset analytics design](DATASET_ANALYTICS_DESIGN.md) | Generic file analytics library/CLI design and implementation notes. |
| [Phase 4 web research design](PHASE4_WEB_RESEARCH_DESIGN.md) | Research pipeline design and acceptance gaps for the unmerged Phase 4 work. |
| [Free-source catalogue](FREE_SOURCE_CATALOGUE.md) | Provider-by-provider eligibility/status; candidates are not guaranteed available. |
| [Answer contract design](ANSWER_CONTRACT_DESIGN.md) | Number-lock and deterministic answer contract. |
| [Project health design](PHASE3_PROJECT_HEALTH_DESIGN.md) | Project-scoped freshness and health design. |

## Architecture, data and operations

| Document | Responsibility |
|---|---|
| [Architecture](ARCHITECTURE.md) | Components, data flow, stack and future boundaries. |
| [Database](DATABASE.md) | Schema and persistence details. |
| [Domain model](DOMAIN_MODEL.md) | Core domain concepts. |
| [Data pipeline](DATA_PIPELINE.md) | Ingestion, conflict handling, refresh and operational pipeline. |
| [Analytics specification](ANALYTICS_SPEC.md) | Defined metrics, invariants and analytics behaviour. |
| [Deployment](DEPLOYMENT.md) | Hosting and deployment operations. |
| [Scalability](SCALABILITY.md) | Scaling assumptions and path. |
| [Security](SECURITY.md) | Secrets, access controls, privacy and source permissions. |
| [Evidence ledger design](EVIDENCE_LEDGER_DESIGN.md) | Evidence/source/claim provenance. |
| [Evidence retention](EVIDENCE_RETENTION.md) | Retention, expiry, purge and data lifecycle rules. |
| [Source discovery](SOURCE_DISCOVERY.md) | Historical Asian Games source investigation, not the global provider catalogue. |

## Governance and quality

| Document | Responsibility |
|---|---|
| [Architecture decisions](DECISIONS.md) | Append-only decision history, including superseded ADRs. Do not delete historical decisions simply because they were superseded. |
| [Testing strategy](TESTING.md) | Test layers, commands and acceptance requirements. |
| [Acceptance evidence](../ACCEPTANCE.md) | Specific acceptance run IDs/results; distinct from the general testing strategy. |
| [AI prompts](AI_PROMPTS.md) | Guidance for coding agents. |
| [Changelog](../CHANGELOG.md) | Chronological record of changes, not a substitute for the roadmap. |
| [Reference data notes](../data/reference/README.md) | Provenance and maintenance for reference datasets. |

## Document lifecycle rules

- A feature being planned is not evidence it is implemented. State implementation status with a branch/PR and test evidence when relevant.
- Keep one canonical product direction, one current roadmap, one test strategy and one acceptance evidence ledger. Other documents may describe historical context or a specific subsystem.
- Cross-link related documents instead of copying the same long requirements into several files.
- Keep historical ADRs, source investigations, acceptance records and domain specifications unless there is evidence they are redundant and no longer referenced.
- Before deleting a Markdown file, search all relative links, README references, code comments, open PRs and historical rationale. Prefer marking a document historical/superseded and linking the canonical replacement when it contains unique information.
- Do not describe the workspace as complete until the CSV upload → profiling → charts/tables → validated deterministic refinement → export acceptance journey passes.
