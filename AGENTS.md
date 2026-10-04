# AGENTS.md - Engineering Rules

These rules apply to every AI agent and every human contributor. If a request conflicts with a rule, **stop and report the conflict before implementing.**

## 1. Before writing code
1. Read `README.md` and all files in `docs/`.
2. Restate the requirement and list assumptions.
3. Do an impact analysis: database, pipeline, analytics, dashboard, tests, docs.
4. Propose the design and the test plan. Wait for approval on anything architectural.

## 2. Non-negotiable rules
1. **No silent architecture changes.** Do not change database technology, folder layout, metric definitions, source priority, or dependencies without an approved decision (`docs/DECISIONS.md`).
2. **Deterministic analytics.** All numbers come from SQL/pandas over database rows. Never hardcode medal numbers. Never take numbers from an LLM or from prose.
3. **Traceability.** Every placing must link to a raw version (URL, retrieval time, content hash). Manual CSV imports are stored as raw documents too.
4. **Raw data is immutable.** Save what was fetched exactly as received. Parsing and cleaning work on copies and never overwrite raw files.
5. **Idempotent ingestion.** Running the same ingestion twice must not create duplicates or change results.
6. **Database changes need migrations.** Never edit the schema by hand, and never edit a migration that has been applied: add a new one. PostgreSQL is the database in every environment.
7. **Parametrised SQL only.** No string-built queries.
8. **Secrets stay out of Git.** Use `.env` (ignored) and `.env.example` (committed, no real values).
9. **Respect sources.** Check `robots.txt` and terms of use, identify the client honestly, rate-limit (at most 1 request per 2 seconds per host by default), cache, and back off on errors. Prefer official APIs and downloadable files over scraping HTML. If a site disallows automated access, do not bypass it. Use manual CSV import instead.
10. **Validate before loading.** Data failing validation goes to a quarantine table with a reason. It is never silently dropped or silently loaded.
11. **No new dependency without justification** (why needed, maintenance status, licence, alternatives). Prefer the standard library and existing dependencies.
12. **Never fabricate data.** If data is missing, report it as missing and show completeness metrics. Do not fill gaps with estimates unless the metric is explicitly labelled as an estimate.
13. **Use the domain terms precisely.** *Event*, *medal placing*, *entrant* and *country medal* mean exactly what `docs/DOMAIN_MODEL.md` says. Do not use bare "medal" where the distinction matters.
14. **Fetch and parse are separate.** Source adapters only fetch. Parsing lives in per-source parsers that are pure functions over saved raw data.
15. **Source conflicts follow the policy table** in `docs/DATA_PIPELINE.md` section 7. Never overwrite an accepted value with a stale or lower-confidence claim, and never keep a possibly stale value without flagging it.
16. **No migration 001 before the design-freeze gate** in `docs/ROADMAP.md` (Phase 1) is confirmed.

## 3. Code standards
- Python 3.11+, type hints on all public functions, `ruff` for lint and format, `pytest` for tests.
- Small modules with one responsibility. No business logic inside the dashboard layer, inside source adapters (fetch only) or inside parsers (parse only).
- Structured logging (JSON lines): timestamp, run_id, component, level, message, plus context fields. Never use bare `print` in library code.
- Errors: raise specific exceptions. Parsers fail loudly on unexpected page or API structure instead of guessing.
- Configuration through a single settings module reading environment variables. No magic constants inside code.

## 4. Workflow for every change
```
Requirement -> Clarify -> Impact analysis -> Design -> Test plan
-> Implement -> Run tests -> Self-review -> Update docs -> Commit
```
- Branches: `main` (always working), `feature/<name>`, `fix/<name>`.
- Commits are small and describe why, not just what.
- Do not commit data dumps larger than 5 MB, or any secrets.

## 5. Self-review checklist (run before saying "done")
- [ ] Meets the requirement and acceptance criteria
- [ ] Tests added or updated and passing
- [ ] Idempotency checked
- [ ] No hardcoded numbers, paths or secrets
- [ ] Logging and error handling in place
- [ ] Metric definitions still match `docs/ANALYTICS_SPEC.md`
- [ ] Schema change has a migration and `docs/DATABASE.md` is updated
- [ ] No unrelated changes in the diff
- [ ] Docs and `CHANGELOG` updated

## 6. Definition of done
A task is done only when the code is merged-ready, tests pass, docs match behaviour, and the acceptance criteria in `docs/ROADMAP.md` for that task are demonstrably met (show command output or a screenshot).

## 7. When unsure
Ask. State the options, give a recommendation, and wait. Do not choose silently.
