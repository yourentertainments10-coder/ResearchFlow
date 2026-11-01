# Repository audit — 2026-10-30

> **Audit snapshot, not a live status page.** Branch names and ahead/behind comparisons were captured at the time of writing. Recheck current branch/PR state before closing or deleting branches; current product direction is in [WORKSPACE_PRODUCT_DIRECTION.md](WORKSPACE_PRODUCT_DIRECTION.md) and current delivery gates in [ROADMAP.md](ROADMAP.md).

## Scope and method

Audit baseline: `main` at `9f115f34483d6c3d005682ed8c34bac09c167746`. Recursive Git tree returned 291 entries, `truncated=false`, including 30 Markdown files. This is a repository/branch comparison audit, not a claim that every runtime acceptance test was executed locally. The environment used for this audit could not clone GitHub directly, so file contents, tree metadata, branch comparisons, PR metadata and GitHub Actions results were checked through the GitHub integration.

No files are deleted by this audit. Historical and domain-specific documents are retained until references and ownership are reconciled.

## Verified current state

- `main` remains primarily the Asian Games sports pipeline and static HTML dashboard.
- `src/sie/datasets/` and `src/sie/datasets_cli.py` provide a generic deterministic one-file analytics library and CLI (CSV/Excel/JSON table scope as documented). There is no generic visual CSV upload workspace yet.
- `src/sie/web/dashboard.html` is the sports dashboard. A static HTML page alone cannot invoke the Python dataset engine without a defined API/hosting boundary.
- Phase 4 research code is still separate on `feature/phase-4-web-research` / draft PR #31. It adds evidence web fetching and research discovery/collection/report code, but its own docs explicitly say live structured-data source acceptance and target-Windows save/reuse have not passed. Simulated-network tests do not establish live-source success.
- PR #35 (false null warning in grouped analytics) merged as `f2011cf64ea4a249219e93de3325133b4c4e440a` after CI's test job, Ruff lint and format steps passed. It adds regression tests. This is a correctness fix, not UI completion.
- PR #33 product direction/source permissions and PR #34/#36/#37 documentation status updates have been merged. No Phase 4 code was merged as part of those documentation PRs.

## Branch comparison results

A branch that is behind `main` with zero ahead commits has no unique commits relative to current main; it is an archive/delete *candidate*, not an automatic deletion decision. Diverged branches require checking their unique commits and PR purpose before closure/deletion.

### Behind main, zero ahead commits — archive/delete candidates after references are checked

`docs/generalisation-audit`, `feature/acceptance-evidence`, `feature/alert-failure-test`, `feature/analytics-from-facts`, `feature/analyze-workflow`, `feature/answer-contract`, `feature/backup-encryption`, `feature/dashboard-smoke`, `feature/datasets-library`, `feature/datasets-library-impl`, `feature/evidence-ledger`, `feature/phase-0-signoff`, `feature/phase-2-canonical-loader`, `feature/phase-3-conflict-policy-v2`, `feature/phase-3-project-health`, `feature/phase-4-analyze`, `feature/phase-6-ops-foundation`, `feature/portal-fetcher`, `fix/acceptance-official-total`, `fix/dashboard-js-apostrophe`, `fix/declare-report-deps`, `fix/wrangler-previews`, `copilot/fix-refresh-job-failure`, `diag/seed-reference-timeouts`.

### Diverged branches — do not merge/delete blindly

- `feature/phase-4-web-research`: contains a substantial unmerged research implementation; preserve pending its separate live-source and Windows acceptance gates. PR #31 remains draft.
- `docs/product-direction-alignment`: diverged from main because it was based on the Phase 4 branch and contains Phase 4 code as well as docs. PR #32 was closed; do not merge this branch. Its approved documentation-only equivalent was ported to main through PR #33.
- `docs/product-direction-main`: documentation work was ported through PR #33, but the branch is now behind/diverged; not needed as a merge source.
- `docs/status-accuracy-followup`: its status fixes were merged via PR #34; historical branch only.
- `docs/acceptance-ledger-status`: its acceptance-ledger correction was merged via PR #36; historical branch only.
- `docs/roadmap-current-status`: roadmap update merged via PR #37; historical branch only.
- `docs/phase-4-web-research-design`: unique doc branch, but it is not a current merge source; reconcile with the canonical Phase 4 design before archive.
- `docs/status-update` and `docs/sync-discovery-pipeline`: stale, diverged docs/source-discovery snapshots. Do not merge; compare their unique content with current `DATA_PIPELINE.md`, `SOURCE_DISCOVERY.md` and `CHANGELOG.md` before archive.
- `feature/phase-2-import`, `feature/phase-3-conflict-policy`, `feature/phase-3-portal-adapter`: historical code branches, diverged and behind main; their accepted implementation is represented by later merged work. Preserve until unique commits are checked against the relevant merged PRs.
- `feature/production-acceptance`: historical branch with acceptance/backup implementation; later PRs exist, but it is diverged. Verify its unique commits against merged PRs before archive.
- `fix/dataset-validation-warning-main` and `fix/datasets-false-null-warning`: the fix is merged through PR #35; these are historical branches.
- `main`: current default branch.

## Documentation responsibilities and initial disposition

| Document(s) | Recommendation | Reason / next action |
|---|---|---|
| `README.md` | Keep/update | Entry point and current status are aligned; continue to keep implementation claims current. |
| `AGENTS.md` | Keep | Mandatory engineering and approval gates. |
| `docs/WORKSPACE_PRODUCT_DIRECTION.md` | Keep (canonical product goal) | Approved end-state, CSV-first journey, source permissions and alternatives. |
| `docs/PRODUCT_REQUIREMENTS.md` | Keep/update | Add measurable UI/API acceptance criteria after the hosting/API design is approved; keep sports-domain rules distinct. |
| `docs/ROADMAP.md` | Keep/update | Canonical delivery order; sports-phase history still useful but status dates need periodic reconciliation. |
| `docs/ARCHITECTURE.md` | Keep/update | Accurate for current sports pipeline, but generic workspace/API and hosting boundaries are not designed yet. |
| `docs/DATASET_ANALYTICS_DESIGN.md` | Keep | Generic engine operations, deterministic output and CLI/library contracts. |
| `docs/PHASE4_WEB_RESEARCH_DESIGN.md` | Keep/update | Canonical design for unmerged research code; live acceptance remains open. |
| `docs/FREE_SOURCE_CATALOGUE.md` | Keep/update | Provider-by-provider status and limitations; revalidate sources when implementation is resumed. |
| `docs/SECURITY.md` | Keep/update | Source permission, privacy, access restrictions and safe alternatives. |
| `docs/EVIDENCE_LEDGER_DESIGN.md`, `docs/EVIDENCE_RETENTION.md` | Keep both | Related but distinct: provenance/claim ledger vs retention and deletion rules. Cross-link; do not merge merely because both mention evidence. |
| `docs/TESTING.md`, `ACCEPTANCE.md` | Keep both | Test strategy vs run-by-run acceptance evidence ledger. |
| `docs/DECISIONS.md` | Keep | Historical ADRs, superseded records and accepted decisions; don't delete historical decisions. |
| `docs/GENERALISATION_AUDIT.md`, `docs/GENERAL_RESEARCH_ENGINE_VISION.md`, `docs/PRODUCT_VISION.md` | Keep, label as historical/domain context | These contain useful audit evidence and sports-specific vision; link them to the canonical workspace direction rather than merging their distinct content into one huge document. |
| `docs/SOURCE_DISCOVERY.md` | Keep as historical source investigation | Specific Asian Games source discovery, not a global source catalogue. |
| `docs/PHASE3_PROJECT_HEALTH_DESIGN.md` | Keep | Project-health/retention implementation design is a distinct capability. |
| `docs/ANSWER_CONTRACT_DESIGN.md` | Keep | Number-lock contract is distinct from the product-level natural-language planner. |
| `docs/ANALYTICS_SPEC.md`, `docs/DATA_PIPELINE.md`, `docs/DATABASE.md`, `docs/DOMAIN_MODEL.md`, `docs/DEPLOYMENT.md`, `docs/SCALABILITY.md` | Keep | Separate operational, domain and scaling responsibilities. |
| `docs/AI_PROMPTS.md`, `CHANGELOG.md` | Keep/update | Agent instructions and chronological change history; don't duplicate the full roadmap here. |
| `data/reference/README.md`, fixture READMEs | Keep | Local to their data/fixture responsibilities. |

## Discrepancies and unresolved decisions

1. The product goal is a visual workspace, but the current dashboard is a sports-specific static HTML page and the generic dataset engine is library/CLI-first. A serving/API boundary must be selected before connecting browser uploads to the Python engine.
2. The current deployable dashboard uses static HTML/Cloudflare workflow, while the generic engine runs Python. Deployment of the generic UI/API and storage/retention must be designed as a cohesive path; don't imply a static page can execute Python directly.
3. Natural-language planning is not implemented. It must produce a validated supported operation spec; all calculations remain deterministic.
4. The Phase 4 source catalogue currently reports no controlled live structured-data source smoke test passing. Don't claim automated research works end-to-end.
5. Some old phase headings/statuses in `ROADMAP.md` refer to the original sports project. Keep them as historical project history but make the current top-level milestone/status the single current-status summary.
6. Branch names alone do not establish completion. Use PR merge status and exact head CI evidence, not green checks from an earlier commit.

## Recommended next sequence

1. Finish checking relative Markdown links and status claims in all root/docs Markdown files.
2. Confirm the CSV workspace serving architecture: (A) local Python web app backed by `sie.datasets`, or (B) separate frontend and Python API with explicit deployment and storage plan. Prefer the simplest free-first path that can be tested end to end; do not add dependencies until justified.
3. Update `PRODUCT_REQUIREMENTS.md`, `ARCHITECTURE.md`, `TESTING.md`, `DEPLOYMENT.md` and `ROADMAP.md` with the chosen UI/API and measurable acceptance gates.
4. Implement a small vertical slice: upload → profile → two or more charts/tables → one validated deterministic refinement → CSV/XLSX export.
5. Test it end-to-end and preserve existing sports regression tests. Then extend supported operations, source fallback controls and research integration.
6. Archive stale branches only after their unique commits and open PRs are reconciled. Do not mass-delete Markdown files; this audit has found distinct useful roles for the existing design documents.
