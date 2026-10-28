# ResearchFlow: General Research Engine Vision

**Status:** Product-direction document, written 2026-10-28  
**Implementation status:** Partially implemented on the Phase 4 feature branch; this is not a claim that the complete vision is already built.  
**Related documents:** PRODUCT_VISION.md, PHASE4_WEB_RESEARCH_DESIGN.md, EVIDENCE_LEDGER_DESIGN.md, DATASET_ANALYTICS_DESIGN.md

## 1. Product goal

ResearchFlow should grow into a general-purpose research and data analytics engine with a workflow similar in spirit to AI-assisted web research:

> A user asks a question. ResearchFlow finds relevant public sources, reads what it is permitted to access, records evidence and provenance, collects usable structured data, analyses it reproducibly, and returns a cited answer. Suitable datasets can be reused for follow-up questions.

The goal is not simply to download one CSV or return a list of URLs. It is to connect discovery, retrieval, evidence, data preparation, analysis, reporting and reuse into one reliable workflow.

ResearchFlow does not need to reproduce the private internals of ChatGPT, Gemini or another product. It should implement its own transparent, testable workflow using sources and services it is allowed to access.

This document records the intended broader research-engine direction. It does not claim that the current code already supports arbitrary web questions. It also does not silently replace existing product decisions or phase numbering. Where this broader direction changes the current sports-focused product vision or roadmap, reconcile that deliberately in the product documents and an ADR.

## 2. Non-negotiable constraints

1. **Zero paid API/model dependencies by default.** Do not call paid search, data or model services. Do not automatically fall back to a paid service when a free source is unavailable or a quota is exhausted.
2. **Free does not mean unlimited.** Record whether a source is free, keyless, registration-based, quota-limited or manual-only. Disclose limits. A free provider may still have rate limits or usage terms.
3. **Respect access rules.** Follow robots.txt, source terms, published API rules, licence conditions and rate limits. Never bypass logins, paywalls, CAPTCHAs, HTTP 403 restrictions or other access controls.
4. **No invented facts or numbers.** Every reported number must come from an identified source dataset or a reproducible deterministic calculation. Distinguish observed facts, derived analysis, interpretation and unverified claims.
5. **Provenance is mandatory.** Keep the source URL, retrieval time, relevant response metadata, content/hash identity where applicable, licence/terms information, transformations and evidence locations.
6. **Permission-aware retention.** Do not archive every page indiscriminately. Store only what source policy and licence permit; otherwise keep the allowed metadata/excerpt or use the content for the current run only.
7. **Fail honestly.** If no relevant, permitted, usable source is found, say what was tried and why it failed. Ask for a URL/file or present the supported limitation instead of fabricating an answer.
8. **Security checks apply to every discovered host.** HTTPS, public-address/SSRF checks, validated redirects, robots rules, rate limiting, timeouts, response-size limits and content-type limits remain required.

## 3. Intended end-to-end workflow

~~~text
User question
    |
    v
Understand the request
(topic, geography, date/year, entities, desired evidence and metrics)
    |
    v
Discover sources using permitted free methods
(catalogues, documented public APIs, allowed website discovery)
    |
    v
Classify and rank leads
(official source / dataset / report / academic paper / general reference)
    |
    v
Check terms, licence, robots, safety, relevance and freshness
    |
    v
Fetch permitted pages or data
(HTML, CSV, JSON, open APIs, supported documents)
    |
    v
Extract evidence and/or normalise structured tables
(preserve source and row/field provenance; flag gaps and conflicts)
    |
    v
Save only permitted data with scope and expiry
    |
    v
Run deterministic, testable analysis
    |
    v
Produce cited answer/report, chart if useful, and explicit limitations
    |
    v
Reuse suitable saved evidence/data on follow-up questions
~~~

A source lead is not evidence by itself. A fetched page is not automatically a trustworthy fact. A table is not automatically suitable for the question. These stages must remain distinguishable in both code and user-facing output.

## 4. Discovery strategy: broaden coverage without paid fallback

There is no assumption that an unlimited, general-purpose, live-web search API is available for free. ResearchFlow should use a layered discovery strategy.

### 4.1 Prioritised discovery methods

1. **Official public-data catalogues and government APIs.** Prefer primary sources for official statistics, elections, public finance, health, education, transport and similar public datasets. Validate API terms, quota, access requirements and licence for each adapter.
2. **Other documented free public catalogues and open APIs.** Examples may include research metadata, international organisations or subject-specific open datasets. Each provider must be reviewed before it is enabled.
3. **Permitted website-level discovery.** Where a site allows it, use its search interface/API, sitemap, RSS/Atom feed, index or documented download links. Apply the same safety and robots rules as for all other sources.
4. **Open crawl indexes or archives.** A resource such as Common Crawl may provide useful historical discovery or page snapshots. Treat these as snapshots, not proof of a page's current state; access and usage conditions still apply.
5. **User-supplied URL or file.** If permitted free discovery is unavailable or insufficient, request a specific URL or file and clearly label this as a manual fallback.

Do not scrape search-engine result pages or use an unapproved paid endpoint as an improvised general-web search system. Do not recursively crawl arbitrary links without an explicit design, safety limits and permission checks.

### 4.2 Provider registry requirements

Every provider/adapter must declare:

- provider name, purpose and host(s);
- kind of result returned: dataset, paper/reference, webpage or API result;
- documented access mode: free keyless, free with registration/key, quota-limited or manual-only;
- terms/usage reference, last verification date and known quota/rate limit;
- relevant licence metadata behaviour;
- permitted redirect hosts, if any, narrowly scoped to that provider;
- expected response formats and failure conditions;
- whether it is live-verified or only covered by simulated tests.

Any source with unclear terms or access conditions remains disabled/manual-only until reviewed. If a provider is blocked by its robots rules, do not bypass the restriction just because it is official or useful.

### 4.3 Discovery results are leads, not answers

For each lead, show or retain:

- title and URL;
- source/provider and authority category;
- declared data format when known;
- publication/update date where available;
- relevance reasoning;
- geography/year/topic match or mismatch;
- licence status where known;
- robots status: not checked, allowed, or disallowed;
- limitations that remain unverified.

Do not call a lead verified or fetchable simply because it was returned by a catalogue.

## 5. Relevance: select sources that can answer the question

Matching a few keywords is not enough. Before collection, assess the lead against the important dimensions of the user's question:

- **Topic:** Does it cover the actual subject, not just similar words?
- **Geography:** Does it concern the requested country, region or population?
- **Time:** Does it cover the requested date, year or period?
- **Data shape:** Is it a structured dataset with useful fields, an explanatory page, an academic paper, or merely a reference?
- **Coverage:** Does it contain the requested entities, rows, dates and variables?
- **Authority and provenance:** Is it a relevant primary source, a reliable secondary source, or unclassified?
- **Permission and access:** May the system retrieve and retain the required material?

Examples:

- A US 2011 election dataset must not be used to answer a UK 2024 election question.
- An academic paper about election manifestos may be useful background, but it is not automatically constituency-level election data.
- An official dataset with no stated year or geography must carry that uncertainty forward; the system must not silently infer it.
- A PDF/report can be a valid reference, but must not be represented as a machine-readable table unless the system successfully extracts and validates one.

When no eligible dataset remains, stop the quantitative analysis and explain why. Do not lower the quality threshold silently just to produce an answer.

## 6. Reading, extraction and evidence records

ResearchFlow should support permitted public content including HTML pages, CSV, JSON and documented open APIs. Additional document formats should be added only when parsing and verification are tested.

For a retrieved source, record what is permitted and useful:

- requested URL and final URL;
- retrieval timestamp and HTTP/content-type metadata;
- content hash or version identity where applicable;
- title and source/provider identity;
- licence/terms provenance and retention decision;
- extracted facts/table rows with source locators, such as a JSON pointer, row/column, table cell or character range;
- parser and normalisation steps;
- refusals, missing fields, rejected rows, contradictions and unresolved limitations.

Do not confuse the following evidence layers:

1. **Raw source:** the source response or permitted reference to it.
2. **Normalised data:** parsed rows and fields, with provenance and quality flags.
3. **Analytical result:** deterministic calculations over specified data.
4. **External evidence:** sourced statements that provide context or corroboration.
5. **Generated explanation:** wording that explains the sourced result and its limitations.

Retain raw copies only when policy permits. If only an excerpt or metadata may be retained, make that limitation explicit.

## 7. Dataset store, reuse and analysis

A saved dataset should have a stable identity and manifest recording its source, retrieval time, licence, schema, collection/normalisation information, quality flags, project scope and expiry.

Reuse a dataset only after checking:

- project/scope access;
- expiry and freshness;
- topic, geography and time-period coverage;
- required columns and data types for the requested operation;
- source licence and retention eligibility;
- known quality flags, missing values and conflicts.

If a saved dataset is not sufficient, seek more permitted data. Stale data may only be used under an explicit, disclosed fallback policy; otherwise ask for fresh data or report that the evidence is insufficient.

Prefer deterministic, reproducible operations for filtering, aggregation, comparisons, statistics and charts. Missing data and conflicting source values must be reported, not silently filled or resolved. Keep metric definitions unambiguous; for example, legislative seats won, votes received and governments formed are different quantities and must not be combined.

## 8. Natural-language question planning

The long-term experience should accept a natural-language question without requiring the user to know internal CLI operation names or column names.

The planner must translate the question into an explicit, inspectable plan, including:

- subject, geography and time range;
- entities and dimensions;
- requested measure and its definition;
- data coverage/columns required;
- filters, joins and deterministic operations;
- evidence and citation requirements;
- uncertainty or disambiguation needed before analysis.

Start with a small, tested set of supported question types. The plan must be testable and should use canonical metric definitions. If the question is ambiguous—for example, "how many times has a party won?"—ask which metric is intended or state the chosen definition. Do not let an LLM invent a metric or choose arbitrary columns without validation.

An LLM or hosted model is not required for the first working research loop. If an AI planner/explainer is proposed later, it must not add paid dependencies without explicit owner approval, and code remains the authority for fetched data, computations, validation and numeric answers.

## 9. Answer/report contract

A successful answer should include, as applicable:

- a direct answer to the question;
- the metric definition and relevant time/geographic scope;
- citations to the actual sources used, not just discovery results;
- the data sources and collection/retrieval dates;
- reproducible calculations and a clear description of the input dataset;
- a chart or table where it adds value;
- missing-data, licence, freshness and source-availability caveats;
- conflicts between sources and whether/how they were compared;
- explicit gaps where a claim remains unverified.

The system must not produce a confident numeric report if it has no usable data. It should give an honest no-data result and explain the next safe step.

## 10. Current implementation status (as of 2026-10-28)

This section is a snapshot and should be updated as implementation changes.

### Present in the Phase 4 branch

- Safe HTTP fetching with HTTPS, public-IP/SSRF checks, redirect validation, robots.txt checks, pacing, timeouts, response caps and refusal handling.
- A provider registry with explicit active/disabled status, a keyless OpenAlex adapter, and a CKAN adapter retained for test/possible future use.
- Discovery ranking and explicit lead limitations, including topic/geography/year mismatches and a distinction between papers and structured data leads.
- Collection of supported CSV, JSON and HTML tables.
- Project-scoped, expiring dataset storage with licence/policy checks, cache and dataset reuse checks.
- Deterministic analysis, report/chart generation, conflict disclosure and licence provenance.
- CLI entry points under the research command group.

### Verified versus not verified

- PR #31 is the active implementation branch. Automated CI runs are simulated-network checks and do not prove that a live source works; the branch's CI must be checked again after each new commit.
- CI uses simulated network tests. A passing CI run does not prove live source availability.
- Live OpenAlex discovery has returned academic references, not suitable structured election datasets. It remains active for scholarly context only.
- The live data.gov.uk CKAN request was redirected to ckan.publishing.service.gov.uk. The observed robots.txt for that host disallows /api/ and declares Crawl-Delay: 10; the API request was refused. The production catalogue row is now explicitly disabled and the redirect allowance removed. Disabled providers are reported without making network requests.
- A real CSV fetch reached the collection/save path on Windows, but saving failed with OSError: [Errno 9] Bad file descriptor during os.fsync. A Windows portability fix has since been pushed and covered in CI; a successful save on the actual Windows machine still needs confirmation.
- The full live workflow—relevant data source discovered, lawfully fetched, stored, analyzed, reported and then reused—has **not yet been marked PASS**.
- PR #31 remains Draft and unmerged. The live workflow gate remains open because no registered structured-data provider has passed the controlled live smoke test.

### Known not-yet-complete capabilities

- General-purpose, permitted web search across arbitrary topics is not yet implemented as a universal search service. The current catalogue list is small and OpenAlex primarily supplies academic works.
- Additional free public catalogue/site adapters need to be evaluated and implemented.
- The collected research datasets are not yet fully linked into the evidence ledger as required for the broader vision.
- Metric definitions and natural-language question-to-analysis planning remain unfinished.
- The live-source smoke test and Windows save verification remain outstanding.

## 11. Delivery plan and acceptance gates

### Gate A — reliable foundations

- Ensure Windows and supported operating systems can save and atomically publish datasets.
- Make provider failure, quota exhaustion, timeout, robots refusal, redirect refusal and storage errors clear to users.
- Honor Crawl-delay and minimum per-host pacing. Unit tests explicitly cover a 10-second Crawl-delay and fail-closed behaviour above the 60-second safety cap.
- Keep all tests, lint and formatting checks green.

### Gate B — relevant free-source discovery

- Keep the blocked data.gov.uk API route disabled for automated discovery unless a permitted endpoint is proven.
- Add documented free sources that return useful structured datasets, not only papers or snippets.
- Test country/year/topic mismatch, academic-reference handling, format declarations, provider failure and licence provenance.
- Clearly state which sources require registration or have quotas.

### Gate C — complete end-to-end live smoke test

In a machine where outbound access is permitted, demonstrate a realistic question from start to finish:

1. discover a genuinely relevant structured-data source;
2. fetch it without violating robots.txt, source terms or rate limits;
3. parse and validate rows;
4. save a permitted dataset and manifest;
5. run a meaningful deterministic analysis;
6. produce a report citing the actual source and disclosing limitations;
7. ask a suitable follow-up and show that the saved data is reused correctly.

If any stage fails, report the exact blocker. Never label the live smoke test PASS merely because a discovery command returned URLs.

### Gate D — broader research experience

- Add more permitted discovery methods and source adapters.
- Connect external claims and dataset provenance to the evidence ledger.
- Add a constrained natural-language query planner and validated metric definitions.
- Expand supported question types gradually, with end-to-end tests and explicit acceptance criteria.

A gate is complete only when its evidence exists. Design documentation, simulated tests and green CI are necessary but not substitutes for live verification.

## 12. Out of scope unless explicitly approved

- Paid search, paid data providers, paid model APIs, or automatic paid fallback.
- Bypassing robots.txt, authentication, paywalls, CAPTCHAs, source blocking or rate limits.
- Unlimited crawling or indiscriminate archiving of entire websites.
- Claiming that a research paper proves a numerical answer when its underlying data was not collected and validated.
- Automatic production deployment or scheduled collection before the owner approves it.
- Claiming full ChatGPT/Gemini parity; the target is a transparent, useful research workflow built within ResearchFlow's constraints.

## 13. Definition of success

ResearchFlow meets this vision progressively when it can take a realistic question, independently discover a relevant permitted public source, collect evidence/data, preserve provenance, run reproducible analytics, return a cited and limitation-aware answer, and reuse the collected dataset on a follow-up—all without paid services or unsafe access shortcuts.

The promise is not "always find an answer." The promise is: **search the available permitted sources intelligently, explain what the evidence supports, and be honest when the data is not there.**
