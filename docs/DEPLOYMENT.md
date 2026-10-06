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

Status of the pipeline workflow: `.github/workflows/refresh.yml` exists and runs on demand only (`workflow_dispatch`), ingesting a file through the scheduler (lock, retries, stuck-run recovery). It has no cron until the portal's terms are cleared (D1); see `DATA_PIPELINE.md` section 12.

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
