# Prompts for an AI Coding Agent

Copy a prompt, paste it into your coding agent, review the result against the acceptance criteria in `ROADMAP.md`, then move on. Do not skip the review step.

## 0. Kickoff (always first)
```
You are working on the Sports Intelligence Engine. Read AGENTS.md and every file in docs/ completely.
Do not write any code yet. Reply with:
1. Your understanding of the project in 10 lines.
2. Any contradictions, gaps or risks you see in the documents.
3. Questions you need answered before Phase 0.
4. Your proposed order of work.
```

## 1. Phase 0: source discovery
```
Do Phase 0 from docs/ROADMAP.md for the Asian Games 2026 (Aichi-Nagoya).
Find the official results and medal table sources. For each candidate fill the checklist in
docs/DATA_PIPELINE.md section 2. Check robots.txt and terms of use. Look for JSON endpoints used by the site.
Save trimmed samples as fixtures. Do not bypass any access control.
Output docs/SOURCE_DISCOVERY.md and a recommendation. Do not write pipeline code.
If event-level data cannot be obtained automatically, say so clearly and propose the manual path.
```

## 2. Phase 1: skeleton
```
Implement Phase 1 exactly as in docs/ROADMAP.md, following docs/ARCHITECTURE.md folder layout and docs/DATABASE.md schema.
Do not write migration 001_initial_schema until the design-freeze checklist in docs/ROADMAP.md (Phase 1) is confirmed with me.
Implement placing versioning, raw versioning and snapshot identity exactly as in docs/DATABASE.md.
First list the files you will create and the dependencies you will add with reasons. Wait for my approval.
Then implement, with tests. Show the commands and their output proving the acceptance criteria.
```

## 3. Phase 2: import and loader
```
Implement Phase 2. Follow docs/DATA_PIPELINE.md sections 5, 6 and 9 precisely.
Use the terms and counting rules in docs/DOMAIN_MODEL.md. Implement reallocation exactly as in docs/DATABASE.md section 6 and test it.
Create the golden dataset under tests/fixtures/golden/ with the tricky cases listed in docs/TESTING.md.
Write tests first, then the code. Unknown names must be quarantined, never auto-created.
Prove idempotency by running the import twice.
```

## 4. Phase 3: adapter
```
Implement Phase 3 for the source chosen in docs/SOURCE_DISCOVERY.md.
The adapter only fetches; write the parser as a separate pure component (docs/ARCHITECTURE.md section 6).
Implement raw version semantics (docs/DATA_PIPELINE.md section 4) and the conflict policy table (section 7).
Rate limit, cache, honest User-Agent, hash-based change detection, raw store as in DATA_PIPELINE.md.
Parse only from saved fixtures in tests. If the structure is not what you expect, fail loudly; never guess.
Add reconciliation against the official medal table.
```

## 5. Phase 4: analytics
```
Implement Phase 4 using docs/ANALYTICS_SPEC.md as the only definition of metrics.
Each metric is a pure function with a docstring referencing the section. Compute expected values by hand
for the golden dataset and test against them. Add the invariant tests. Division by zero returns null.
Do not change any formula without telling me first.
```

## 6. Phase 5: dashboard
```
Implement Phase 5 with Streamlit and Plotly per docs/ARCHITECTURE.md section 7.
No metric formulas in the dashboard; call analytics functions or read snapshot tables.
Show completeness, reconciliation status and small-sample warnings on every relevant page.
```

## 7. Phase 6: automation
```
Implement Phase 6 per docs/DEPLOYMENT.md. Create the scheduled workflow, failure notification, staleness alert
and the daily source health check. Document exactly where data is stored between runs and why.
```

## 8. Add a feature or change (template)
```
Change request: <describe>.
Do NOT code yet. Provide: requirement restatement, assumptions, impact analysis (database, pipeline,
analytics, dashboard, tests, docs), whether any rule in AGENTS.md or any decision in docs/DECISIONS.md conflicts,
the design, the test plan, and a rollback plan. Wait for approval.
```

## 9. Self-review (after any implementation)
```
Stop coding. Review your implementation against: requirements, docs/ARCHITECTURE.md, docs/ANALYTICS_SPEC.md,
security rules, idempotency, error handling, logging, tests, backward compatibility, and the AGENTS.md checklist.
List every problem you find, with file and line. Do not fix anything yet.
```
Then:
```
Fix only the listed problems. No unrelated changes. Run the relevant tests and show the output.
```

## 10. Bug report (template)
```
Bug: <what happened>. Expected: <expected>. Evidence: <logs/run id/sample>.
First reproduce it with a failing test. Then explain the root cause. Then fix it minimally and show the test passing.
If the cause is a source structure change, update the fixture and the parser and tell me what changed.
```

## 11. Data-quality investigation
```
Reconciliation shows a mismatch for <country>/<medal>. Investigate using raw versions, placing_history, source_observations and source_conflicts.
Report the likely cause (timing, shared medal, reallocation, parsing error, source error) with evidence.
Do not change data until I approve the fix.
```
