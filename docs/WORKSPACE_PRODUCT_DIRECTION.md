# ResearchFlow Product Direction: General Research + Visual Analytics Workspace

**Status:** Owner-approved product direction, 2026-10-29. This document is the current product-level source of truth for the broader experience. It does not claim the full experience is implemented.

## 1. Product goal

ResearchFlow should become a free-first, privacy-conscious research and data-analysis workspace. It should help a user move from a question or dataset to a reproducible analysis, visual findings, cited evidence and portable outputs.

The product is **not a chat-window clone**. The main experience is a visual analysis workspace: KPI cards, charts, tables, filters, data-quality notes and findings are the primary content. Natural language is a control for creating or changing the analysis, not a replacement for the workspace.

The existing Asian Games sports engine remains the first domain-specific workflow and must keep its verified outputs. Generic data analysis and research are added alongside it without silently breaking the sports path.

## 2. First complete journey

Build and verify one end-to-end journey before broadening automatic web research:

1. The user uploads a CSV file (then add Excel/JSON where supported).
2. ResearchFlow safely reads the file and shows a profile: row/column counts, inferred types, missing values, duplicates, suspicious values and likely date/numeric fields.
3. It runs a small, documented set of deterministic analyses and creates several useful charts, summary cards, tables and findings.
4. The user asks for a change in natural language, such as “compare sales by month” or “show this as a bar chart.”
5. A planner converts that request into a validated, inspectable analysis specification. Unsupported or ambiguous requests ask a question or explain the limitation.
6. Deterministic code performs the calculation. The interface updates the existing workspace, preserving relevant filters and history where possible.
7. The user can inspect the inputs, metric definitions, transformation log, data-quality warnings and result provenance.
8. The user exports the chart and data/report.

**Acceptance gate:** a fresh test file must pass upload → profile → multiple visualisations → natural-language refinement → deterministic re-computation → verification → export. Tests must cover malformed files, missing values, unsafe inputs, unsupported requests, and reproducibility. A screenshot or passing unit tests alone is not enough; the complete journey must be demonstrated.

## 3. Main product areas

- **Projects:** create, reopen and manage separate workspaces.
- **Data Sources:** uploads, permitted URLs/APIs, source status, retrieval time, licence/terms status and data freshness.
- **Analysis Workspace (primary):** KPI cards, charts, tables, filters, data quality, findings and a natural-language analysis control on the same page.
- **Research Reports:** cited answers, methods, source conflicts, gaps and saved/exportable reports.
- **Settings:** privacy, source-selection defaults, fallback preference, retention and export options.

Keep the workspace as the primary screen. Use subtle, purposeful transitions when charts or panels change; respect reduced-motion preferences and avoid animation that delays or hides results.

## 4. Natural-language controls must not invent results

The planner may propose only supported, typed operations and chart specifications. Validate the plan against the actual columns, types, permissions and operation registry before execution. Use deterministic code for all numeric results. The language model, if added later, may help interpret a request and explain computed findings; it is not the source of truth.

If a request is ambiguous, ask a concise clarification. If the data cannot answer it, say why. Never silently fabricate, estimate, substitute a different metric or use a mismatched dataset.

## 5. Source-selection and fallback behaviour

The user can work in one of two modes:

### Selected-source mode
The user selects or approves specific sources. ResearchFlow must use those sources only unless the user has already enabled global fallback. If the selected source does not contain suitable data, show which source was checked and what was missing, then ask whether global discovery may be used. Do not silently switch sources.

### Global-discovery mode
The user allows ResearchFlow to discover other eligible sources. ResearchFlow can search multiple suitable sources without asking at every step, while still showing the sources used, important limitations and conflicts. A remembered/global-fallback preference must be explicit, visible and changeable in settings.

The permission to use another source is a **user preference for software behaviour**. It does not override that source's access rules, terms, licence, privacy restrictions or retention limits.

## 6. Access, licence and retention are different checks

Before fetching or reusing a source, distinguish:

- **User source choice:** has the user allowed this source or global discovery?
- **Technical access:** can the permitted endpoint be reached without bypassing access controls?
- **Source policy/terms:** does the source permit the intended automated access?
- **Licence and intended use:** may the data be analysed, retained, transformed, exported or redistributed?
- **Privacy and security:** is the data public, private, personal or otherwise restricted?

A public URL does not automatically mean unrestricted access, storage or redistribution. A robots.txt rule is not a licence or access authorisation; it is one signal that the crawler must respect. A user click cannot override a source restriction.

- If automated access is disallowed, a login/paywall/CAPTCHA is required, or the host blocks the request, do not bypass it. Explain the blocker and offer a permitted alternative source or manual upload.
- If analysis is permitted but raw-data redistribution is restricted, allow only the permitted analysis and do not put restricted raw data in public exports. Export rules must also check whether derived results may be shared.
- If the licence is unclear, do only processing for which the basis is clear. Pause broader retention or redistribution until the policy is clarified, or use a clearly permitted alternative.
- On 403, 429, CAPTCHA, paywall or other access restriction, stop that route; do not rotate identities, proxy around the block or retry aggressively. Retry only transient failures under bounded backoff and the source's rate limits.


## 6A. Required permission checks and permitted alternatives

Run and record these as three separate checks for every source and intended use. Do not collapse them into one generic "permission" flag:

1. **User authorisation / source choice:** Has the user selected or approved this source, or explicitly enabled global discovery? In selected-source mode, do not silently switch sources. If the selected source is unsuitable, explain what was checked and what is missing, then ask whether global discovery is allowed. A previously enabled, visible and changeable global-fallback preference permits searching other eligible sources without asking each time.
2. **Automated-access permission:** Do the endpoint's documented API rules, terms and applicable access controls permit this automated request? A public URL or user approval does not override source rules. If automation is disallowed, or login/paywall/CAPTCHA/robots/terms restrictions or a blocking response prevent access, do not bypass them. Stop the blocked route and explain the specific reason.
3. **Use, retention and redistribution permission:** Does the source's licence or policy permit the intended analysis, retention, transformation, export, sharing and redistribution? These are distinct rights. Record the licence/policy evidence and uncertainty; do not infer unrestricted reuse from public availability.

### Alternatives when a check fails

- **User/source choice unavailable:** Stay within the selected sources and report the gap. Ask for approval to use global discovery; if that preference is already enabled, search other eligible sources and disclose each source used. Offer manual upload or a user-supplied URL where appropriate.
- **Automated access unavailable:** Prefer an official documented API or permitted download; validate an alternative documented endpoint or supported CSV/JSON/HTML-table format; paginate according to documentation; use bounded backoff only for transient failures and within published limits; compare another eligible source only when the user's source mode permits it; or ask the user to upload a file / provide a permitted URL. Never evade login, paywall, CAPTCHA, 403/429 blocks, robots rules or rate limits with identity rotation, proxies or aggressive retries.
- **Raw-data redistribution unavailable:** Do not include restricted raw rows/files in exports or public shares. If the licence permits it, offer derived aggregate statistics, charts, brief attributed facts and source links, with methods and limitations. Check derived outputs too; aggregation does not automatically make redistribution lawful. If the licence is unclear, limit processing to the clearly permitted purpose, pause broader retention/sharing, and ask for clarification or switch to a clearly permitted alternative dataset.
- **No suitable permitted source:** State that a reliable result cannot be produced from the sources available. Show the missing coverage, conflict or freshness issue; request an approved file/source or narrow the question. Do not invent values, silently substitute a metric/dataset, or present a partial result as complete.

For every attempt, distinguish source discovery, access success, parse/validation success, freshness, scope coverage and export permission. Record provenance and limitations. Alternatives improve the chance of success but do not guarantee every request can be fulfilled.

## 7. Maximise successful data retrieval—without promising the impossible

The system should make a strong, measured effort to find usable data: prefer official APIs and downloads, support documented CSV/JSON/HTML table formats, paginate correctly, retry transient errors with bounded backoff, cache permitted results, validate alternate endpoints published by the same source, and compare other eligible sources when global discovery is allowed. It should expose precise failure reasons and support manual file/URL input.

It must not promise that every request will succeed. Do not defeat access controls or ignore source rules to make a success metric look better. Measure discovery coverage, successful fetch rate, parse/validation rate, freshness and end-to-end completion separately so failures can be improved honestly.

## 8. Privacy and data handling

- Keep uploaded files local to the user's environment in a local-first deployment where possible.
- In a hosted deployment, clearly disclose where files are processed and stored, isolate projects/users server-side, apply size limits and safe parsing, and provide deletion/retention controls.
- Never send private uploads to public websites, unrelated providers or model APIs without explicit disclosure and consent.
- Do not include private or licence-restricted raw data in public links or exports. Use scoped access for saved dashboards and reports.
- Preserve source, timestamp, hash/version, transformation history, licence/terms evidence and limitations wherever applicable.

## 9. Exports and collaboration

Support, as each format is implemented and tested:
- chart export (PNG/SVG where available);
- table/data export (CSV and Excel);
- report export (HTML/PDF);
- saved workspace/dashboard with explicit view/edit permissions.

Exports must be generated from the actual computed result, include relevant metric/source notes, and respect privacy, licence, project scope and retention rules. Do not describe an export format as supported until it has an acceptance test.

## 10. Delivery sequence

1. **Documentation alignment and architecture audit:** reconcile the existing sports-first documents with this approved product direction; preserve domain-specific specs and historical ADRs where they remain useful.
2. **CSV journey:** reliable upload, profiling, deterministic initial analysis, multiple charts, natural-language refinement through validated operations, verification and export.
3. **Workspace UI:** Projects, Data Sources, Analysis Workspace, Reports and Settings; accessible, responsive, non-chat-first presentation.
4. **Source permission and fallback controls:** selected-source mode, global discovery, explicit fallback preference, per-source policy state and safe failure messages.
5. **Free-first web research:** add documented permitted sources and APIs, live end-to-end acceptance, provenance, licence-aware retention and cross-source conflict handling.
6. **Connect research to the workspace:** research results become cited datasets/claims that can power reproducible charts and follow-up analysis.
7. **Hardening:** end-to-end tests, privacy/isolation tests, export tests, Windows support, accessibility, performance and documented operational limits.

No paid search/data/model provider or automatic paid fallback is introduced by default. Any exception requires a separate explicit owner decision.

## 11. Current implementation status must be stated honestly

The existing main branch is primarily the Asian Games sports pipeline and fixed dashboard. Generic dataset analytics and evidence-ledger components exist in the repository history/branches, and the Phase 4 web-research implementation is on a draft, unmerged branch. The broader visual workspace, universal natural-language planner and complete CSV-to-export journey are **not yet complete**. Simulated CI does not prove a permitted live-source workflow works. See the implementation-specific design documents and PR status for current verification evidence.

## 12. Definition of done

A capability is done only when its code, docs and tests agree; it has deterministic and security-focused tests; its end-to-end acceptance path has been demonstrated; failure states are visible; and no claim of live access, privacy, export or cross-source support is made without evidence.