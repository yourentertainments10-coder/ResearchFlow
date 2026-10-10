# Acceptance evidence

An evidence ledger, not a plan. Each row says what was checked, with run IDs, commits and times (UTC)
taken from the GitHub API on 2026-10-10 15:00-15:30 UTC. Statuses: **PASS**, **FAIL**, **NOT RUN**,
**BLOCKED**. A status is only PASS where the evidence below shows it ran and succeeded. Manual
workflow dispatches are never counted as unattended scheduled runs.

Run URLs have the form `https://github.com/yourentertainments10-coder/ResearchFlow/actions/runs/<id>`.

## Summary

| Area | Status | One line |
|------|--------|----------|
| Official analysis on production data | PASS (workflow level) | Manual `analyze` run succeeded; counts not independently re-read |
| Three consecutive unattended scheduled refreshes | PASS (workflow level) | Runs on 8, 9, 10 Oct all `schedule`, all steps success; run logs not readable |
| Alert lifecycle, isolated | PASS (automated, local receiver) | PR #17, unmerged |
| Alert delivery to the real channel | NOT RUN | Needs the owner's `NOTIFY_WEBHOOK_URL` |
| Dashboard smoke test, local and CI | PASS | PR #19, unmerged |
| Dashboard smoke test, deployed site | NOT RUN | Sandbox cannot reach the site |
| Cloudflare preview build | FAIL on branches, fix proposed | Cause confirmed by PR #18's own build |
| Encrypted backup workflow | BLOCKED | Never ran; PR #16 unmerged; needs key variable and secret |
| Isolated restore test in Actions | BLOCKED | Same |
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
| Isolated lifecycle test: outage, single delivery, persistence across processes, failed-channel retry, interrupted run, recovery, relapse, corrupt state | PASS | PR #17 (unmerged), commit `725f8fc`, CI runs 38060849980 (push) and 38060882323 (pull_request), both success |
| Workflow runs health after a failed refresh | PASS (static test) | `tests/unit/test_refresh_workflow.py`, same CI runs; verified to fail against the old workflow |
| Delivery to the owner's real webhook | NOT RUN | The test receiver is a local HTTP server; `NOTIFY_WEBHOOK_URL` is the owner's. Manual drill: `docs/DEPLOYMENT.md` 7a (in PR #17) |
| Intentional failure of the production refresh | NOT RUN | Deliberately not done: production Neon is never broken for a test |

## 4. Dashboard

| Item | Status | Evidence |
|------|--------|----------|
| Smoke test on a locally built page and the committed `site/index.html` (7 routes, desktop and mobile, light and dark, console/network errors, figures vs data) | PASS | PR #19 (unmerged), commit `e150c21`, CI runs 38062429252 and 38062457458, both success with `REQUIRE_BROWSER=1`; also 11/11 run locally in Chromium |
| Test fails on a blank page, the PR #14 syntax error, console errors, external requests, mobile overflow, wrong figures | PASS | The nine negative cases in `tests/unit/test_dashboard_smoke.py`, same runs |
| JavaScript syntax check (PR #14) | PASS | `node --check` test, in CI on every run |
| Deployed site `https://researchflow.yourentertainments10.workers.dev/` | NOT RUN | Sandbox could not connect (HTTP 000). Run `python tests/dashboard_smoke.py --url <site>` from a machine with internet |

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
Status: **FAIL on every branch until PR #18 merges; production deploys unaffected.**

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
| Encryption and restore code, tests | PASS in CI and in the sandbox | PR #16 (unmerged), head `69fef31`, CI runs 38027123870 and 38027371348 success; sandbox: 11 crypto unit tests and an end-to-end encrypt, decrypt and `pg_restore` on PostgreSQL 16 with schema, row-count and raw-blob checks |
| Encrypted backup workflow succeeds in Actions | **BLOCKED** | PR #16 not merged; the owner has not created the age key pair or set `BACKUP_AGE_RECIPIENT` and `BACKUP_AGE_IDENTITY`. The workflow was never run |
| Isolated restore test in Actions | **BLOCKED** | Same; it is a step of that workflow |
| Backup or restore acceptance | NOT COMPLETE | Not to be marked done until one encrypted backup run and its restore step succeed |

## 7. Open pull requests (none merged by this work)

| PR | Branch | Content |
|----|--------|---------|
| #16 | `feature/backup-encryption` | Backup encryption, scratch restore (ADR-034 there) |
| #17 | `feature/alert-failure-test` | Alert lifecycle test, health after failed refresh (ADR-035) |
| #18 | `fix/wrangler-previews` | Cloudflare preview fix |
| #19 | `feature/dashboard-smoke` | Browser smoke tests in CI (ADR-036) |
| #15 | `feature/production-acceptance` | Overlaps #16 and #19 (see below) |

**Conflict to resolve before merging:** PR #15 (opened 04:23Z by another session) also implements
encrypted backups (it also calls it ADR-034, edits `backup.yml`, `backup.py`, `cli.py`, `config.py`),
a dashboard smoke script and a `docs/ACCEPTANCE.md`. It and #16 cannot both merge as they are. One
design has to be chosen.

## 8. Still pending, for the owner

1. Choose between #15 and #16 for backups, then create the age key pair and set the variable and secret (`docs/DEPLOYMENT.md` section 3a in #16).
2. Run the encrypted backup workflow once by hand, then wait for a scheduled run; record both here.
3. Merge #18 so branch builds pass; run the deployed-site smoke test and record it.
4. Provide `NOTIFY_WEBHOOK_URL` and run the manual alert drill against a throwaway channel.
5. Open one of the scheduled refresh runs and confirm in its log that the portal was fetched.
6. Note: GitHub moves `ubuntu-latest` to Ubuntu 26 on 2026-10-19; the backup workflow installs the PostgreSQL client and `age` by apt, so check it after that date. Every run also warns that Node 20 actions are forced onto Node 24.
