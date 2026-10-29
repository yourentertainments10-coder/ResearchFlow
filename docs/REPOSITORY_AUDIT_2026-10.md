# Repository audit — 2026-10-30

## Scope
Baseline: `main` at `9f115f34483d6c3d005682ed8c34bac09c167746`. The recursive tree contained 291 entries (not truncated) and 30 Markdown files. This is a GitHub metadata/content audit; the current environment could not clone the repository locally, so no local full test run is claimed. No files are deleted by this audit.

## Verified current state
- `main` remains primarily the Asian Games sports pipeline and static HTML dashboard.
- `src/sie/datasets/` and `src/sie/datasets_cli.py` provide generic deterministic one-file analytics as a library/CLI; the visual CSV upload workspace is not built.
- `src/sie/web/dashboard.html` is sports-specific. A static page cannot directly invoke the Python engine without an API/serving boundary.
- Phase 4 research remains separate on `feature/phase-4-web-research` / draft PR #31. Its own design docs state live structured-data access and target-Windows save/reuse have not passed acceptance. Simulated CI does not prove live-source success.
- PR #35 merged as `f2011cf64ea4a249219e93de3325133b4c4e440a` after CI test job, Ruff lint and format steps passed. It fixes a false null warning and adds regression tests; it does not complete the workspace.
- PR #33 source policy/product direction, PR #34/#36 status corrections and PR #37 roadmap update were merged. These did not merge Phase 4 code.

## Branch audit

### Behind main, zero ahead commits — archive/delete candidates only after reference checks
`docs/generalisation-audit`, `feature/acceptance-evidence`, `feature/alert-failure-test`, `feature/analytics-from-facts`, `feature/analyze-workflow`, `feature/answer-contract`, `feature/backup-encryption`, `feature/dashboard-smoke`, `feature/datasets-library`, `feature/datasets-library-impl`, `feature/evidence-ledger`, `feature/phase-0-signoff`, `feature/phase-2-canonical-loader`, `feature/phase-3-conflict-policy-v2`, `feature/phase-3-project-health`, `feature/phase-4-analyze`, `feature/phase-6-ops-foundation`, `feature/portal-fetcher`, `fix/acceptance-official-total`, `fix/dashboard-js-apostrophe`, `fix/declare-report-deps`, `fix/wrangler-previews`, `copilot/fix-refresh-job-failure`, `diag/seed-reference-timeouts`.

### Diverged branches — do not merge/delete blindly
- `feature/phase-4-web-research`: substantial unmerged code; preserve pending separate live-source and Windows acceptance gates. PR #31 remains draft.
- `docs/product-direction-alignment`: diverged because it was stacked on Phase 4 and contains code plus docs. PR #32 was closed; do not merge this branch. The docs-only equivalent was ported to main through PR #33.
- `docs/product-direction-main`: docs ported via PR #33; historical branch.
- `docs/status-accuracy-followup`: changes merged via PR #34; historical branch.
- `docs/acceptance-ledger-status`: correction merged via PR #36; historical branch.
- `docs/roadmap-current-status`: update merged via PR #37; historical branch.
- `docs/phase-4-web-research-design`: unique design snapshot; reconcile against canonical Phase 4 design before archive.
- `docs/status-update`, `docs/sync-discovery-pipeline`: stale, diverged documentation snapshots; do not merge, compare unique content before archive.
- `feature/phase-2-import`, `feature/phase-3-conflict-policy`, `feature/phase-3-portal-adapter`: historical diverged code branches; verify unique commits against merged PRs before archive.
- `feature/production-acceptance`: diverged acceptance/backup branch; verify unique commits against later merged PRs before archive.
- `fix/dataset-validation-warning-main`, `fix/datasets-false-null-warning`: fix merged through PR #35; historical branches.
- `main`: default branch.

## Documentation disposition

| File(s) | Action | Reason |
|---|---|---|
| `README.md` | Keep/update | Entry point/current status; keep claims aligned to implementation. |
| `AGENTS.md` | Keep | Mandatory engineering and approval gates. |
| `docs/WORKSPACE_PRODUCT_DIRECTION.md` | Keep, canonical | Approved end goal, CSV journey, source permissions and alternatives. |
| `docs/PRODUCT_REQUIREMENTS.md` | Keep/update | Add measurable UI/API criteria after serving architecture is approved. |
| `docs/ROADMAP.md` | Keep/update | Current sequence plus sports project history; reconcile old phase statuses over time. |
| `docs/ARCHITECTURE.md` | Keep/update | Current sports pipeline is documented; workspace/API boundary remains undecided. |
| `docs/DATASET_ANALYTICS_DESIGN.md` | Keep | Generic engine operations, determinism and CLI/library contract. |
| `docs/PHASE4_WEB_RESEARCH_DESIGN.md` | Keep/update | Design for unmerged research code; live acceptance open. |
| `docs/FREE_SOURCE_CATALOGUE.md` | Keep/update | Provider status and constraints; revalidate before enabling. |
| `docs/SECURITY.md` | Keep/update | Access, privacy, source policy and safe alternatives. |
| `docs/EVIDENCE_LEDGER_DESIGN.md`, `docs/EVIDENCE_RETENTION.md` | Keep both | Provenance/claim ledger differs from retention/deletion rules; cross-link. |
| `docs/TESTING.md`, `ACCEPTANCE.md` | Keep both | Test strategy differs from run-by-run acceptance evidence. |
| `docs/DECISIONS.md` | Keep | Historical ADRs including superseded decisions are important context. |
| `docs/GENERALISATION_AUDIT.md`, `docs/GENERAL_RESEARCH_ENGINE_VISION.md`, `docs/PRODUCT_VISION.md` | Keep as historical/domain context | Preserve audit evidence and sports-specific requirements; link to canonical workspace direction. |
| `docs/SOURCE_DISCOVERY.md` | Keep as historical source investigation | Asian Games source record, not global source catalogue. |
| `docs/PHASE3_PROJECT_HEALTH_DESIGN.md`, `docs/ANSWER_CONTRACT_DESIGN.md` | Keep | Separate project-health/retention and number-lock responsibilities. |
| `docs/ANALYTICS_SPEC.md`, `docs/DATA_PIPELINE.md`, `docs/DATABASE.md`, `docs/DOMAIN_MODEL.md`, `docs/DEPLOYMENT.md`, `docs/SCALABILITY.md` | Keep | Distinct domain, data, operations and scaling responsibilities. |
| `docs/AI_PROMPTS.md`, `CHANGELOG.md` | Keep/update | Agent workflow and chronological change history; avoid duplicating the roadmap. |
| `data/reference/README.md`, fixture READMEs | Keep | Local data/fixture-specific responsibilities. |

## Discrepancies and unresolved decisions
1. The product goal is a visual workspace, but current UI is a sports-specific static HTML page and generic analytics is library/CLI-first.
2. Static frontend and Python engine need a defined serving/API boundary plus storage and retention plan. Do not imply static HTML can execute Python directly.
3. Natural-language planning is not implemented; it must emit validated supported operation specs. Calculations remain deterministic.
4. Phase 4 source catalogue says no controlled live structured-data smoke test has passed.
5. Old sports-phase statuses in `ROADMAP.md` are historical; top-level current milestone should remain the canonical status summary.
6. Branch names and green checks alone do not prove completion; verify PR merge state and CI for exact head SHA.

## Recommended sequence
1. Check Markdown links and status claims across all root/docs Markdown files.
2. Decide the CSV workspace serving architecture: simplest free-first local Python web app or separate frontend + Python API, with explicit deployment/storage boundaries. Do not add dependencies until justified.
3. Update `PRODUCT_REQUIREMENTS.md`, `ARCHITECTURE.md`, `TESTING.md`, `DEPLOYMENT.md` and `ROADMAP.md` with measurable UI/API acceptance gates.
4. Implement a vertical slice: upload → profile → multiple charts/tables → one validated deterministic refinement → CSV/XLSX export.
5. Run end-to-end tests and preserve sports regression tests; then extend supported operations, source fallback controls and research integration.
6. Archive stale branches only after unique commits and open PRs are reconciled. No mass deletion of Markdown; current docs have distinct useful roles.
