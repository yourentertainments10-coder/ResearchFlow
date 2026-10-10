# Production acceptance

Everything here is a **procedure**. A box is ticked only by someone who ran the step and saw the result.
Nothing in this file is evidence that a step passed. Never paste `DATABASE_URL`, tokens or private keys
into an issue, a PR, a log or this file.

Status when written (2026-10-10), from what could be observed without access to Actions logs or Neon:

| Item | State |
|------|-------|
| PR #14 (dashboard JS fix) | merged, CI green (observed) |
| Live Cloudflare dashboard | **not verified** (section 1 is the procedure) |
| Neon counts (469 / 1,568) | **not verified** (section 2) |
| `analyze` workflow run | **not verified**; no artifact seen |
| Scheduled refresh | 2 successful scheduled runs seen at step level; 3 consecutive not yet confirmed (section 3) |
| Backup workflow | failing nightly, cause unknown (logs not reachable); no artifact exists |

## 1. Live dashboard smoke test

From any machine with network access:

```bash
pip install playwright && playwright install chromium
python scripts/smoke_dashboard.py https://<your-cloudflare-site>/
```

It opens 7 views (overview, countries, sports, gender, timeline, explorer, method) in desktop-light,
desktop-dark and mobile, and fails (exit 1) on a JS or console error, an empty view, the text
"Couldn't draw", horizontal overflow, or a missing "Validation evidence" block on the method view.
Pass = exit 0 and one `OK` line per view and context. Also open the site in a browser and check the
medal table is not empty. The same script runs on a file: `python scripts/smoke_dashboard.py site/index.html`.

## 2. Neon refresh and analyze

Names the workflows use (check they exist under Settings → Secrets and variables → Actions; look only
at the names, never reveal values):

| Kind | Name | Used by |
|------|------|---------|
| Secret | `DATABASE_URL` | refresh, analyze, backup |
| Variable | `PORTAL_FETCH_ENABLED` (must be `true` for scheduled runs) | refresh |
| Variable | `HTTP_USER_AGENT` (honest, with a contact) | refresh |
| Secret (optional) | `NOTIFY_WEBHOOK_URL` | refresh (alerts) |
| Variable | `BACKUP_AGE_RECIPIENT` (public key) | backup |
| Variable (optional) | `PG_MAJOR` (default 17) | backup |

Steps:
1. Actions → refresh → Run workflow (kind `portal`). Open the run: every step green, and read the
   `sie health` output.
2. Actions → analyze → Run workflow. Open the run: green, artifact `analysis` present.
3. Run the database check from a machine that has `DATABASE_URL` (read-only transaction, changes nothing):
   `sie acceptance`  (prints JSON, exit 0 only if all checks pass).
4. Download `analysis` and compare with the checklist.

Checklist (all must be true):

- [ ] `sie acceptance` exit 0: 469 events, 1,568 current placings (470 gold, 469 silver, 629 bronze),
      40 medal countries, 0 open conflicts, 0 disputed events, 0 unresolved quarantined rows, last
      successful official run under 26 h old, no run stuck in `running`, analytics snapshots exist.
- [ ] `manifest.json`: `events_total` 469, `events_completed` 469, `reconciliation` = `reconciled`
      (never `not_run`, never `mismatch`), `disputed_events` 0, `quarantined_unresolved` 0.
- [ ] `manifest.json` `snapshot`: a `fingerprint` and a `daily_snapshot_id`; a second `analyze` run with
      no data change gives the same fingerprint and `change_snapshot_id` null (no new change snapshot).
- [ ] Artifact contains the 12 CSV tables, `analysis.xlsx` and `manifest.json`.
- [ ] The analyze step did not fail on reconciliation (exit 1 means mismatch).
- [ ] Neon was not used as a test database: no test or fixture was run against it.

Caveat: the `analyze` workflow reconciles against the saved official fixture
(`tests/fixtures/sources/bornan/ALL_medals_standings.decoded.json`), a snapshot from the day it was
captured. It proves the loaded data equals that table, not that the live portal still agrees.

## 3. Scheduled reliability

A scheduled run and a manual dispatch are different things; only the `event` field tells them apart
(the run title looks the same):

```bash
gh api "repos/yourentertainments10-coder/ResearchFlow/actions/workflows/refresh.yml/runs?event=schedule&per_page=10" \
  --jq '.workflow_runs[] | [.run_number,.event,.conclusion,.created_at]|@tsv'
```

`event=schedule` is cron; `event=workflow_dispatch` is manual and **does not count**.

Three consecutive unattended runs:
- [ ] Take the three newest `event=schedule` runs; all `conclusion=success`, on three consecutive days
      (cron `30 2 * * *` UTC; GitHub may delay a run by minutes, and disables schedules in a repository
      with no activity for 60 days).
- [ ] No run in between with `conclusion=failure` or `cancelled`.
- [ ] After each, the `sie health` step printed no alerts.
- [ ] Nobody dispatched manually during the window in a way that could mask a missed schedule
      (compare run count against days).

Intentional failure and alert test (never against production Neon):
1. Create a Neon **branch** of the database (or a separate scratch project) and use its URL.
2. On a machine: `DATABASE_URL=<branch> PORTAL_FETCH_ENABLED=true HTTP_USER_AGENT="<real UA>"
   HTTP_TIMEOUT_SECONDS=0.001 NOTIFY_WEBHOOK_URL=<a test endpoint you own> sie scheduled-run portal`
   then `sie health`.
3. Expect: fetch retried 3 times, run recorded `failed`, `sie health` exit 1, one alert delivered.
4. Run `sie health` again: no second delivery within `ALERT_RENOTIFY_MINUTES` (de-duplication).
5. Rerun without the timeout override: recovers, alert clears.
6. Delete the Neon branch.

The same behaviour is covered by automated tests on the embedded test database (`tests/integration`);
those tests do not replace step 2 on real infrastructure.

## 4. Encrypted backup and restore

Why: the repository is public, and artifacts of a public repository are downloadable by anyone. The old
`backup.yml` would have published a plain dump (including raw portal responses). No artifact exists
(the job has been failing), so nothing was exposed as far as can be observed; check Actions → Artifacts.
Anything named `sie-backup` that exists is plaintext: delete it.

Setup (once, on a trusted machine):
```bash
pip install -e ".[backup]"
sie backup-keygen --out ~/secure/sie-backup.age-key     # prints only the public key
```
- Set the public key as repository **variable** `BACKUP_AGE_RECIPIENT`.
- Keep the private key file offline (password manager or an encrypted drive) in at least two places.
  It is never a repository secret, never in the workflow, never in an artifact. Losing it means the
  backups cannot be read; leaking it exposes every backup made for that key. To rotate: generate a new
  key, change the variable; old backups still need the old key until they age out.
- Set `PG_MAJOR` if Neon runs a different Postgres major than 17.

How the job protects the data: refuses to start without a recipient; restore-tests the plaintext in a
scratch database, encrypts (age, public-key), deletes the plaintext; a guard fails the job if any file
other than `*.dump.age` / `*.manifest.json` exists or any file starts with `PGDMP` or contains
`AGE-SECRET-KEY-`; only then it uploads (retention 14 days, last 14 backups kept).

Restore verification (do this once after the first successful run, then quarterly):
```bash
# download the artifact, then on a machine with Postgres client tools and a scratch-capable server:
DATABASE_URL=<server where a scratch database may be created, NOT production> \
  sie restore-test sie-asiad-2026-<stamp>.dump.age --identity ~/secure/sie-backup.age-key
```
Pass = exit 0: encrypted file hash matches the manifest, decrypts, restores, alembic revision, row counts
and raw-blob SHA-256 match. A wrong key or a changed file must fail (covered by tests).

- [ ] Open the failed backup run's log and record the real error; the cause is unknown from here.
      One unverified hypothesis: a `pg_dump` client older than the Neon server (fixed by `PG_MAJOR`).
- [ ] After setting the variable, dispatch `backup` once: green, artifact `sie-backup-encrypted` has only
      `.dump.age` and `.manifest.json`.
- [ ] `sie restore-test ... --identity` passes on that artifact.
- [ ] Neon also keeps point-in-time history on its own; this backup is the independent copy.
