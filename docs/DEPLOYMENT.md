# Deployment and Automation

Decision record: ADR-015 (PostgreSQL), ADR-016 (hosting split). Hosting terms change often. The facts below were checked on **2026-10-02** against the providers' own pages; re-check before relying on them.

## 1. Target architecture
```
 GitHub Actions (cron)  ----\                          /----> Streamlit dashboard (Render web service)
 or Render cron (paid)       \                        /          connects as read-only role
                              v                      /
                      Pipeline  ---- writes ---->  PostgreSQL (Neon free, permanent)
                       |                           ^   schema public (pipeline) / reporting (readers)
                       v                           |
        raw bytes: RAW_STORE_BACKEND = db | s3 | fs     nightly pg_dump  ---->  off-host backup

 Cloudflare in front: DNS, CDN, WAF, optional access control.   Later: FastAPI on Render + React on Cloudflare Pages.
```
Everything talks to the database through one variable, `DATABASE_URL`. The code does not know which host runs Postgres.

## 2. What each provider is actually good for (checked 2026-10-02)
| Provider | Verified facts | Use it for | Do not use it for |
|----------|----------------|-----------|-------------------|
| **Neon** (free plan) | Permanent plan, no card. 1 GB per project. 100 compute-hours per project per month. Compute suspends after 5 minutes idle (cannot be turned off). Only 6 hours of history/restore on the free plan | Production Postgres for this project | Relying on its restore window as a backup |
| **Supabase** (free) | 500 MB database. Free projects are paused after 1 week of inactivity. No automatic backups on free | Optional alternative host | A database that must survive quiet weeks |
| **Render** (free) | Free web service spins down after 15 minutes without traffic (about 1 minute to wake), 750 free instance hours per workspace per month, ephemeral disk, no persistent disk. **Free Postgres expires 30 days after creation (14-day grace, then deleted), 1 GB, no backups.** Cron jobs and background workers are not listed as free types | Dashboard web service; later the API. A paid Postgres or cron job is an option | Production database on the free plan (it will be deleted) |
| **Cloudflare Workers** (free) | 100,000 requests per day, 10 ms CPU per invocation. No Python pipeline fits that | DNS, CDN, WAF, Pages for a static or React front end, optional access control in front of the dashboard | Running the collection pipeline |
| **GitHub Actions** | Scheduled workflows (cron) and a Postgres service container for CI. Check current free minutes and limits for your repository type. Scheduled workflows can run late, and GitHub disables schedules in public repositories after a long period without repository activity (verify the current period) | Scheduled pipeline runs (state lives in Postgres, so the empty runner is fine), CI, nightly backup, daily source health check | A database |

Sources: Render free-tier docs (`render.com/docs/free`), Neon pricing (`neon.com/pricing`), Supabase pricing (`supabase.com/pricing`), Cloudflare Workers pricing docs.

## 3. Recommended free setup
1. **Database:** Neon free Postgres. Use the pooled connection string for the dashboard and the direct string for migrations. Create roles `sie_owner`, `sie_pipeline`, `sie_reader` (`DATABASE.md` section 13).
2. **Pipeline:** a GitHub Actions workflow on a schedule (every 15 to 30 minutes while the competition runs, daily after). It runs `sie run` with the pipeline credentials from repository secrets. State is in Postgres, so nothing is lost between runs. If you prefer a machine you control (your computer, Raspberry Pi), the same command works from cron or Task Scheduler.
3. **Raw bytes:** `RAW_STORE_BACKEND=db` (gzip in `raw_blobs`). Hash dedup keeps it small. Watch the 1 GB limit on the Page "Data quality". If it approaches 60 percent, switch to `s3` (any S3-compatible object store; check the free allowance of the one you choose).
4. **Dashboard:** Render free web service (accepts the 15-minute sleep) or Streamlit Community Cloud. It connects as `sie_reader`. Cloudflare sits in front for DNS and CDN.
5. **Backups (mandatory):** a nightly Actions job runs `pg_dump`, compresses, encrypts and uploads it off-host (an object store, or download it to your own computer on a schedule). Keep the last 14 dumps. Neon's 6-hour history is not a backup plan.
6. **Notifications:** workflow failure emails, plus `NOTIFY_WEBHOOK_URL` (Telegram, Discord or email) from the pipeline for staleness and conflicts.

Cost note: this setup uses free plans only, so it has limits and can change. If a limit blocks you, the smallest paid upgrade is usually a paid Postgres plan. Nothing in the code has to change.

Status of the pipeline workflow: `.github/workflows/refresh.yml` fetches the official portal daily (02:30 UTC) through the scheduler (lock, retries, stuck-run recovery), then runs `sie health`. The schedule only runs when the repository variable `PORTAL_FETCH_ENABLED` is `true`; `HTTP_USER_AGENT` (variable) must carry a contact address. It can also be started by hand to ingest a file. See ADR-031 and `DATA_PIPELINE.md` section 12.

Alerts: `sie health` (JSON, exit 1 on any alert) is meant to run after each scheduled refresh; add `SCHEDULED_SOURCES` to the environment. Only the log channel exists; `NOTIFY_WEBHOOK_URL` is not wired yet.

### Remaining production setup (Phase 6 leaves these to the owner)
Nothing below is configured by the code or the workflows; no credentials or storage have been created.
1. **`DATABASE_URL` secret.** Set it as a GitHub Actions repository secret (or the hosting provider's secret store). `backup.yml` and `refresh.yml` read it. Use a direct (non-pooled) connection; the backup restore test needs a role that can `CREATE DATABASE`. Until it is set, the scheduled `backup` workflow will fail each night.
2. **Durable alert state.** The workflow keeps the state in an Actions cache, which can be evicted (unused entries expire after about 7 days), after which open alerts are re-sent once. `sie health` keeps its de-duplication state in a JSON file (`ALERT_STATE_PATH`, default `DATA_DIR/alert_state.json`). On an ephemeral runner or container that file is lost on every deploy or restart and every active alert is sent again. Point `ALERT_STATE_PATH` at storage that persists, or move the state to a database table in a later migration (ADR-030).
3. **Backup encryption key pair (required before `backup.yml` can succeed).** The workflow fails closed without it and never uploads a plaintext dump. See section 3a below: generate a key pair, set the repository variable `BACKUP_AGE_RECIPIENT` (public) and the secret `BACKUP_AGE_IDENTITY` (private), and keep an offline copy of the private key. Off-host object storage is still optional; the encrypted artifact (14 days) is the stopgap.
4. **Repository variables for the scheduled refresh (D1 is cleared, ADR-031).** Set `PORTAL_FETCH_ENABLED` to `true` to switch the schedule on (any other value pauses it) and `HTTP_USER_AGENT` to something like `SIE-research/0.1 (contact: you@example.org)` with a real contact address. Optional secret `NOTIFY_WEBHOOK_URL` delivers alerts; without it they go to the job log and the failed job email.
5. **Phase 4 acceptance on the production database.** After the first portal refresh has filled the database, run the `analyze` workflow (manual). It runs `sie analyze --official` against `DATABASE_URL`, takes the snapshots and uploads all tables; it fails on any reconciliation mismatch.
6. **First live run.** The fetcher has not been run against the live portal (the build sandbox cannot reach it). Run `sie fetch-portal --out data/capture.json` from your computer first, check the file, then `sie scheduled-run portal`, then switch the schedule on. 

### 3a. Backup encryption (ADR-034)
**Why.** The repository is public and Actions artifacts of a public repository can be downloaded by anyone signed in to GitHub. A database dump contains the full dataset and raw evidence, so it must never be uploaded in the clear.

**Audit result (2026-10-10).** `backup.yml` had never produced an artifact: every scheduled run on 8, 9 and 10 Oct failed at `sie backup --out backups` (the CLI option is `--out-dir`, exit 2) and the upload step was skipped. The only artifact in the repository is `analysis` (public derived tables from `sie analyze`, expires 2026-10-24). So **no unencrypted dump was exposed**. The old workflow would have uploaded one had the flag been right; that path is removed.

**How it works now.** `sie backup --encrypt-to <age recipient> --require-encryption` writes the dump into a private temporary directory, encrypts it with `age` to the public recipient, and removes the plaintext. The manifest records the plaintext hash and the ciphertext hash. The workflow then decrypts with the private identity, restores into a scratch PostgreSQL service container (never the production database), compares schema revision, row counts and re-hashes the raw blobs, and only then uploads `*.dump.age` and `*.manifest.json` as artifact `sie-backup-encrypted`. A guard step refuses to upload anything else or any file without the age header.

**One-time setup (owner).**
```
age-keygen -o backup-identity.txt        # prints the public key (age1...)
```
1. Repository variable `BACKUP_AGE_RECIPIENT` = the `age1...` public key.
2. Repository secret `BACKUP_AGE_IDENTITY` = the whole contents of `backup-identity.txt` (the `AGE-SECRET-KEY-...` line).
3. Keep an offline copy of `backup-identity.txt` (password manager). Without it the artifacts cannot be decrypted. Delete the local file when stored.

**Restore by hand.**
```
age -d -i backup-identity.txt -o db.dump  sie-backup-<ts>.dump.age
sie restore-test sie-backup-<ts>.dump.age --identity-file backup-identity.txt --scratch-url <scratch db url>
pg_restore --no-owner -d <empty database> db.dump
```
`--scratch-url` is refused if it equals `DATABASE_URL`. Never restore-test into production.

**Rotation.** Generate a new pair, replace the variable and the secret, keep the old identity offline until the artifacts it protects (14 days) have expired.

**Residual risks.** Anyone who can edit workflows on the default branch or read Actions secrets can obtain the private identity; keep write access minimal and require review for `.github/workflows/`. Artifacts are encrypted but still hold the full dataset, so loss of the key means loss of the backup, not exposure.

**Existing artifacts.** No plaintext dump exists. The `analysis` artifact holds only public derived data and can be left to expire or deleted in Settings, Actions. If a plaintext dump is ever found, delete it, rotate the database password, and treat the contents as disclosed.

## 4. Environments
| Env | Database | Purpose |
|-----|----------|---------|
| dev | Postgres in Docker (`docker compose up db`) or `pgserver` | Development |
| test / CI | Postgres service container, schema from Alembic | Automated tests |
| prod | Neon Postgres | Scheduled runs and dashboard |

Prod secrets live only in the hosting providers' secret stores (GitHub secrets, Render environment). `.env` is for local use and is never committed.

## 5. Local start
```
git clone <repo> && cd sie
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
docker compose up -d db
alembic upgrade head
sie seed-reference          # countries, aliases (sports come from Phase 0)
sie import-csv data/manual/<file>.csv
sie analyze
streamlit run dashboard/app.py
```

## 6. Release checklist
- [ ] All tests and quality gates green (including migrations on a fresh Postgres)
- [ ] Migration rehearsed on a restored copy of production
- [ ] `pg_dump` taken and a restore test is current
- [ ] Secrets configured, reader role verified read-only
- [ ] Source health check passes
- [ ] Reconciliation status reviewed
- [ ] `CHANGELOG` updated, version tagged

## 7. Monitoring
- Run history from `ingest_runs` on the Data quality page.
- Failure notification: workflow failure email and webhook.
- Staleness alert: if the newest successful run is older than twice the schedule interval.
- Source structure alert: daily health check (`TESTING.md`).
- Database size and raw-blob size on the Data quality page, to catch free-plan limits early.

## 8. Rollback
- Code: revert the commit and redeploy.
- Schema: `alembic downgrade -1` if the migration supports it, else restore the pre-migration dump.
- Data: restore the latest dump, or rebuild from the raw versions with the previous code version.
- Never edit production data by hand. Use a migration or a reviewed CSV import.

## 9. After the competition ends
Run a final complete ingestion, reconcile against the official final table, freeze the data (tag a release, keep a final dump), and archive the raw versions. The dashboard then serves the final dataset. Later competitions reuse the same system with a new `competition_id` and, if needed, a new adapter and parser (`SCALABILITY.md`).
