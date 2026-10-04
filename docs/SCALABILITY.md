# Scalability and Growth Plan

The honest starting point: **data volume will never be the problem.** One competition has about 470 events and roughly 1,500 to 2,000 placings. PostgreSQL handles millions of rows on a small plan. What actually grows is the number of *requirements*: more competitions, more metrics, an API, users and logins, a mobile or web front end. So scalability here means keeping the design easy to extend without rewrites.

## 1. Why PostgreSQL and not MongoDB
| Need in this project | PostgreSQL | MongoDB |
|----------------------|-----------|---------|
| Relations (event, placing, entrant, country, sport) with integrity | Foreign keys and constraints in the database | Joins and integrity are application code |
| "Only one current placing per slot" | Partial unique index | No direct equivalent |
| Reallocation as one atomic change | Multi-row transactions are the normal case | Possible, but not the natural model |
| Aggregations by country x sport x gender, ranks, shares | SQL `GROUP BY`, window functions, materialized views | Aggregation pipelines, clumsier for this |
| Raw JSON payloads | `JSONB` column | Natural fit |
| Free managed hosting that does not disappear | Neon, Supabase and others | Atlas free tier exists, but we would still need Postgres-style integrity |
MongoDB would only be considered for a very different workload (huge free-form documents without relations). We store raw payloads once, compressed, and keep selected fields as `JSONB`, so we get document flexibility inside Postgres.

## 2. Growth stages
| Stage | What is added | What changes in the design |
|-------|---------------|----------------------------|
| **1. Now** | One competition, batch pipeline, Streamlit dashboard | Nothing beyond the current docs |
| **2. More competitions** | Olympics, Commonwealth Games, national championships | New `competitions` row, new adapter and parser, reference rows. Core code does not change because every event, entrant, snapshot and run already carries a `competition_id` |
| **3. API layer** | Read-only `FastAPI` service, versioned (`/api/v1/...`), reading the `reporting` schema | The dashboard can switch from direct SQL to the API. A React front end on Cloudflare Pages becomes possible |
| **4. Users and roles** | Accounts, login, Admin / Analyst / Viewer | Add the auth module per `SECURITY.md` section 7 (Argon2id, server-side authorization, audit log). New tables `users`, `roles`, `audit_log`. The domain tables stay untouched |
| **5. Speed and caching** | Materialized views, cache headers, pagination | Metrics move from plain views to materialized views refreshed after each load. Add indexes guided by `EXPLAIN` |
| **6. Heavier pipelines** | More sources, athlete-level data, entries per sport | Queue and workers (for example Postgres-backed queue first, a broker later). Entries data unlocks true medal efficiency |
| **7. Beyond medals** | Other result types (times, scores, rankings) | New tables beside `placings`, same competition, event and country model |

## 3. Design rules that keep this possible
1. **Everything is competition-scoped.** No table or metric may assume a single competition.
2. **Layers do not leak.** Adapter -> parser -> normaliser -> validator -> loader -> analytics -> reporting -> presentation. A front end only reads the `reporting` schema or the API. It never touches operational tables.
3. **Single source of definitions.** Metrics are defined once in `ANALYTICS_SPEC.md` and implemented once. A dashboard or API only displays them.
4. **Config over code.** Sources, schedules, thresholds, free or paid hosting choices are environment variables or config files.
5. **Portable by `DATABASE_URL`.** No host-specific SQL. Only plain PostgreSQL features (partial indexes, `JSONB`, advisory locks, `NULLS NOT DISTINCT`, identity columns).
6. **Stateless services.** Nothing important lives on a web server's disk. State is in Postgres or object storage, so any service can be redeployed or scaled out.
7. **Contracts before code.** When the API arrives, write its contract first (`docs/API_SPEC.md`, OpenAPI; this file does not exist yet and is created only when the API stage starts), as `AGENTS.md` already requires for design changes.
8. **Every growth step is a decision record** in `DECISIONS.md`, with impact analysis, so changes do not silently break earlier assumptions.

## 4. What to do when a limit is hit
| Symptom | First response | Later response |
|---------|---------------|----------------|
| Database near the plan size | Check `raw_blobs` size. Move raw bytes to object storage (`RAW_STORE_BACKEND=s3`) | Paid Postgres plan |
| Dashboard slow | Materialized views, indexes, pagination | Cache layer, API with caching |
| Pipeline cannot run often enough on a free scheduler | Run it on a machine you control | Paid cron job or worker |
| Many viewers | Static export bundle served through Cloudflare | API with CDN caching |
| Free host change or shutdown | Restore the latest `pg_dump` on another host and change `DATABASE_URL` | Managed Postgres with point-in-time recovery |

## 5. Checklist before adding a requirement
- Which stage above is this?
- Does it need a new table, a new metric, a new source, or a new service?
- Does it break an invariant in `DOMAIN_MODEL.md` or a rule in `AGENTS.md`?
- What is the migration, the test, the rollback?
- What is the decision record?
