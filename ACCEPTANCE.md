# Acceptance evidence

An evidence ledger, not a plan. Each row says what was checked, with run IDs, commits and times (UTC)
taken from the GitHub API on 2026-10-10 (first pass 15:00-15:30 UTC, updated 17:00-18:00 UTC after PRs #17, #18, #19 and #16 were merged; backup and acceptance rows updated 2026-10-11 02:30 UTC). Statuses: **PASS**, **FAIL**, **NOT RUN**,
**BLOCKED**. A status is only PASS where the evidence below shows it ran and succeeded. Manual
workflow dispatches are never counted as unattended scheduled runs.

Run URLs have the form `https://github.com/yourentertainments10-coder/ResearchFlow/actions/runs/<id>`.

## Summary

| Area | Status | One line |
|------|--------|----------|
| Production acceptance (`sie acceptance`) | PASS, owner-reported | 11 Oct: 16 of 16 checks, `ok: true` (469 events, 1,568 placings, 470/469/629, 40 countries, 0 open conflicts, 0 disputed, 0 quarantined, 2 snapshots, last official run 1,048 minutes ago). Output pasted by the owner; **not run by this ledger's author**. It passed after PR #21 (seed loads `official_event_total`) and a `sie seed-reference` run with no migration |
| Official analysis on production data | PASS (workflow level) | Manual `analyze` run succeeded; counts not independently re-read |
| Three consecutive unattended scheduled refreshes | PASS (workflow level) | Runs on 8, 9, 10 Oct all `schedule`, all steps success; run logs not readable |
| `sie seed-reference` seeming hung on Neon | Diagnosed, cause on Neon unknown | PR #22 (ADR-038): reproduced on a local database as 361 round trips plus an unbounded lock wait; timeouts, heartbeat and `sie db-activity` added. The owner later reported the seed completed |
| Alert lifecycle, isolated | PASS (automated, local receiver) | PR #17 merged (`8e6cf8f`), CI green |
| Alert delivery to the real channel | NOT RUN | Needs the owner's `NOTIFY_WEBHOOK_URL` |
| Dashboard smoke test, local and CI | PASS | PR #19 merged (`4633cb8`), CI green with `REQUIRE_BROWSER=1` |
| Dashboard smoke test, deployed site | NOT RUN | Sandbox cannot reach the site |
| Cloudflare preview build | PASS on branches since the fix | PR #18 merged (`c2903f8`); branch builds on `91dbda8`, `5d254f7` and later succeed |
| Encrypted backup workflow | PASS (workflow level) | Manual run 38073289248 (10 Oct) and scheduled run 38097824935 (11 Oct, commit `f469d88`) succeeded in every step, including the encrypt-and-restore step and the ciphertext-only guard; artifact `sie-backup-encrypted` exists (ids 11678090578, 11686582418). One earlier manual run, 38072588281, failed in the encrypt-and-restore step (cause not read; later runs pass). Logs not read here |
| Isolated restore test in Actions | PASS (workflow level) | A step of the same runs; it exits non-zero on any failed check. Restore with the owner's own key from a downloaded artifact: **NOT RUN** |
| Exposure of an unencrypted dump | PASS (none found) | No dump artifact ever existed |

## 1. Official analysis (production database)

| Item | Evidence |
|------|----------|
| Run | `analyze` 38025265668, `workflow_dispatch` (manual), commit `151e74a`, 2026-10-10 04:47:06Z to 04:47:36Z, conclusion success |
| Steps | install, "Analyse and reconcile with the official table", "Show the manifest", "Keep the tables": all success |
| Artifact | `analysis` id 11659793555, 305,864 bytes, created 04:47:33Z, expires 2026-10-24 |
| Meaning | The workflow exits non-zero on any reconciliation mismatch, so success means none was reported |
| Not verified here | The artifact and logs cannot be downloaded from the build sandbox, so the counts (470/469/629, 469 events, 1,568 placings) were **not** re-read for this ledger. Read them from the run's "Show the manifest" step or the artifact |
| Not rerun | Deliberately, no reason to |

Status: **PASS** by step conclusions; counts **unverified by this ledger**.

## 2. Refreshes

Manual (does **not** count towards the unattended total):

| Run | Event | Commit | Started | Conclusion |
|-----|-------|--------|---------|------------|
| 38024790410 | workflow_dispatch | `151e74a` | 2026-10-10 04:38:49Z | success |
| 37577486509 | workflow_dispatch | `668989c` | 2026-10-07 05:40:33Z | failure (before the fixes that came later) |

Scheduled (`event = schedule`, unattended):

| # | Run | Commit | Started | Finished | Conclusion |
|---|-----|--------|---------|----------|------------|
| 1 | 37755055282 | `151e74a` | 2026-10-08 09:12:36Z | 09:18:49Z | success |
| 2 | 37910805452 | `151e74a` | 2026-10-09 09:22:03Z | 09:27:15Z | success |
| 3 | 38038839849 | `151e74a` | 2026-10-10 08:42:54Z | 08:56:27Z | success |

Every one of the three has all of these steps successful: prepare schema and reference data, close
stuck runs, "Refresh with locking and retries", restore alert state, "Health check and alerts" (which
exits 1 on any alert), save alert state. They are the only scheduled runs the `refresh` workflow has
had, so they are consecutive. The schedule's cron is 02:30 UTC; GitHub started them 6 to 7 hours late.

Status: **PASS at workflow level** (three consecutive scheduled successes, health check clean each time).

Limits: the job logs are not readable from the build sandbox, so this ledger did not confirm that each
run fetched the live portal rather than reporting "unchanged", nor read the `official: succeeded` line.
The refresh step exits 0 for success and for "skipped, another run holds the lock"; a skipped-lock
run is unlikely here (runs were hours apart) but not excluded by the evidence. Open any of the three
runs and read the "Refresh with locking and retries" step to confirm.

These runs happened **before** ADR-035 (PR #17): with the old workflow a failed refresh would have
skipped the health step. They succeeded, so the gap did not matter for them.

## 3. Alert and failure test

| Item | Status | Evidence |
|------|--------|----------|
| Isolated lifecycle test: outage, single delivery, persistence across processes, failed-channel retry, interrupted run, recovery, relapse, corrupt state | PASS | PR #17 merged as `8e6cf8f`; branch head `bb00448` (main merged in), CI runs 38065599678 (push) and 38065601766 (pull_request), both success; main CI 38066333962 success |
| Workflow runs health after a failed refresh | PASS (static test) | `tests/unit/test_refresh_workflow.py`, same CI runs; verified to fail against the old workflow |
| Delivery to the owner's real webhook | NOT RUN | The test receiver is a local HTTP server; `NOTIFY_WEBHOOK_URL` is the owner's. Manual drill: `docs/DEPLOYMENT.md` 7a (in PR #17) |
| Intentional failure of the production refresh | NOT RUN | Deliberately not done: production Neon is never broken for a test |

## 4. Dashboard

| Item | Status | Evidence |
|------|--------|----------|
| Smoke test on a locally built page and the committed `site/index.html` (7 routes, desktop and mobile, light and dark, console/network errors, figures vs data) | PASS | PR #19 merged as `4633cb8`; head `91dbda8`, CI runs 38066594386 and 38066599800 success with `REQUIRE_BROWSER=1` (a missing Chromium fails instead of skipping; also shown locally by hiding the browser); 11/11 also run locally in Chromium after merging main; main CI 38067177461 success |
| Test fails on a blank page, the PR #14 syntax error, console errors, external requests, mobile overflow, wrong figures | PASS | The nine negative cases in `tests/unit/test_dashboard_smoke.py`, same runs |
| JavaScript syntax check (PR #14) | PASS | `node --check` test, in CI on every run |
| Deployed site `https://researchflow.yourentertainments10.workers.dev/` | NOT RUN | Sandbox could not connect (curl HTTP 000, tried again 18:00Z). Run `python tests/dashboard_smoke.py --url https://researchflow.yourentertainments10.workers.dev/ --screenshots shots/` from a machine with internet |

## 5. Cloudflare Workers Builds

| Commit | Branch | Check result |
|--------|--------|--------------|
| `151e74a`, `ce438ad`, `b58aa6a`, `89df228`, `54a41b2` | `main` (merge commits) | success |
| `c1663f6`, `6746262`, `141d847`, `565a527`, `9238a9f`, `989fd19`, `725f8fc`, `e150c21`, PR #16 head | feature branches | failure, each in 0 s |
| `def9070` | `fix/wrangler-previews` (PR #18) | **success**, preview build URL `.../production/previews/fix-wrangler-previews/builds/7b22bb94-...` |

Branch builds ran `wrangler preview`, which stops unless `wrangler.jsonc` has a `previews` block;
`main` ran `wrangler deploy`, which does not need it. PR #18 adds `"previews": {}` and its own build is
the first branch build that passed, which is the evidence. The cause text is from PR #18's reading of the
wrangler source; the failed builds' own logs are only in the Cloudflare dashboard and were not read.
Update after the merge: PR #18 merged as `c2903f8` (CI 38063752753, Workers Build success). Every branch
head pushed after that and checked here has a successful Workers Build: `bb00448` (#17), `91dbda8`
(#19), `5d254f7` (#16). The earlier failed branch builds stay failed in history.
Status: **PASS** for branch previews since `c2903f8`; the preview command itself was exercised by
Cloudflare's own builds, whose logs are only in the Cloudflare dashboard and were not read here.

## 6. Backups

History of the scheduled `backup` workflow (all failed, all before any upload):

| Run | Commit | Started | Failed step | Upload step |
|-----|--------|---------|-------------|-------------|
| 37710966927 | `668989c` | 2026-10-08 01:03:20Z | "Dump and prove it restores" | not reached |
| 37868909701 | `151e74a` | 2026-10-09 01:15:38Z | same | skipped |
| 38011238542 | `151e74a` | 2026-10-10 00:57:35Z | same | skipped |

Cause for the 9 and 10 Oct runs, from the workflow and CLI sources (logs not readable; the 8 Oct run used an older commit and its cause was not established): the workflow called `sie backup --out`,
the option is `--out-dir`, so the command exited 2.

| Item | Status | Evidence |
|------|--------|----------|
| Unencrypted dump exposed | PASS (none) | Repository artifacts: only `analysis` (id 11659793555). No `sie-backup` artifact ever existed; the upload step never ran |
| Encryption and restore code, tests | PASS in CI and in the sandbox | PR #16 merged as `8d52bd3` (head `5d254f7` with main merged in; CI runs 38067606202 and 38067609002 success). `age` is installed in CI and `REQUIRE_AGE=1` makes a missing tool a failure. Sandbox: full suite passed, including an end-to-end encrypt, decrypt and `pg_restore` into a scratch database |
| Encrypted backup workflow succeeds in Actions | PASS (workflow level) | See summary row: runs 38073289248 and 38097824935. The keys were set by the owner |
| Isolated restore test in Actions | PASS (workflow level) | A step of those runs. Not done: download an artifact and run `sie restore-test FILE.dump.age --identity-file KEY` with the owner's own private key |
| Backup or restore acceptance | PARTIAL | Workflow-level PASS twice; complete once the key-based restore check above has been run and recorded |

## 7. Pull requests

| PR | Branch | State | Content |
|----|--------|-------|---------|
| #16 | `feature/backup-encryption` | merged `8d52bd3` | Backup encryption, scratch restore (ADR-034), plus the read-only `sie acceptance` command ported from #15 (ADR-037) |
| #17 | `feature/alert-failure-test` | merged `8e6cf8f` | Alert lifecycle test, health after failed refresh (ADR-035) |
| #18 | `fix/wrangler-previews` | merged `c2903f8` | Cloudflare preview fix |
| #19 | `feature/dashboard-smoke` | merged `4633cb8` | Browser smoke tests in CI (ADR-036) |
| #15 | `feature/production-acceptance` | closed, superseded | Overlapped #16 and #19; its acceptance command was kept in #16, the rest dropped (comment on the PR). Its own backup workflow had the same `--out` defect |
| #20 | `feature/acceptance-evidence` | open | This ledger |

## 8. Still pending, for the owner

1. Create the age key pair and set the variable and secret (`docs/DEPLOYMENT.md` "Backup encryption"). The private key stays offline apart from the secret.
2. Run the encrypted backup workflow once by hand, then wait for a scheduled run; record both here.
3. Run the deployed-site smoke test and record it. Run `sie acceptance` against production from a machine that holds `DATABASE_URL` (read-only) and record the exit code and the JSON.
4. Provide `NOTIFY_WEBHOOK_URL` and run the manual alert drill against a throwaway channel.
5. Open one of the scheduled refresh runs and confirm in its log that the portal was fetched.
6. Note: GitHub moves `ubuntu-latest` to Ubuntu 26 on 2026-10-19; the backup workflow installs the PostgreSQL client and `age` by apt, so check it after that date. Every run also warns that Node 20 actions are forced onto Node 24.
