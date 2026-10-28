# Free-source catalogue for ResearchFlow

**Reviewed:** 2026-10-28  
**Policy:** ADR-045. Free access only by default. This shortlist is a set of candidates, not a promise that every record or endpoint is unrestricted. Recheck terms, licence, authentication and quota before each adapter is implemented.

## Discovery-first candidates

| Candidate | Best fit | Access / cost notes | How ResearchFlow should use it |
|---|---|---|---|
| [Open Government Data Platform India](https://data.gov.in/) and [Government Open Data License - India](https://data.gov.in/Godl) | Indian government statistics, open datasets and public services | The portal says published resources are licensed under G.O.D.L.-India, with attribution requirements. The portal may be under maintenance; endpoint access, dataset-specific notes and API-key requirements must be checked per resource. | Top-tier discovery candidate for Indian public data. Prefer the publishing ministry/agency's own dataset/API and record attribution, URL, retrieval time and the applicable licence. |
| [Data.gov.uk API documentation](https://guidance.data.gov.uk/get_data/api_documentation/) | UK government dataset catalogue | Keyless CKAN metadata search was implemented, but a live request redirected to `ckan.publishing.service.gov.uk`, whose robots.txt disallows the `/api/` path for this client (owner-observed 2026-10-25). | **Disabled for automated discovery.** Do not retry through another route or bypass robots. Re-enable only after the publisher confirms a permitted endpoint and a live smoke test passes. |
| [OpenAlex API](https://help.openalex.org/api/) and [authentication/rate limits](https://help.openalex.org/api/authentication/) | Scholarly works, authors, institutions and topics | Basic API use is free without a key; a free key increases the allowance. Larger-scale use may be paid, so the application must never use a paid option or automatic paid fallback. | Optional specialised adapter for research/science queries, not a universal web search. Use conservative quotas, cache, and keep provider-reported usage metadata where available. |
| [Wikimedia API access policy](https://www.mediawiki.org/wiki/Wikimedia_APIs/Access_policy), [current rate limits](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits) | Encyclopaedic context and cross-checks | Public APIs are accessible, but usage is rate-limited; limits are being rolled out/adjusted in 2026. Include a descriptive User-Agent with contact information, cache responses and respect Retry-After. Content licences vary by project. | Tier-3 secondary/reference material. Never use Wikipedia/Wikimedia to displace an available official primary record. Verify every dataset/page's attribution and licence. |
| [Common Crawl](https://commoncrawl.org/get-started), [index FAQ](https://commoncrawl.org/faq) | Historical URL discovery and archived public-web content | The corpus and HTTP(S) downloads are free to access, but the index API is rate-limited. The archive is a sample of the web, can be stale, and is not a live general-purpose search engine. Avoid cloud processing paths that may incur external infrastructure costs. | Optional later-stage discovery source. Use the public HTTPS index conservatively, cache queries, record crawl date, and then re-fetch the live source under ResearchFlow's web policy before treating it as current evidence. |
| [APISetu](https://apisetu.gov.in/aboutus) | Indian government APIs and services | The portal says published APIs are free to public whenever possible; that wording is not a guarantee that every API is free or available anonymously. Authentication, consent, pricing and terms can vary by API. | Use only after checking the chosen API's individual conditions. Any endpoint whose cost/access terms are unclear remains disabled. |
| [Kaggle](https://www.kaggle.com/docs/api) | Public community datasets | Public visibility does not mean every dataset has a reusable licence or unrestricted automated download. Account/API credentials, dataset-specific licence, publisher conditions and current quota need to be checked. | Initially accept user-selected/downloaded datasets as file imports. Add automated catalogue/download integration only after confirming free access and licence handling; never infer permission from a public listing alone. |

## Explicit exclusions and guardrails

- Do **not** depend on Google Custom Search JSON API for new setup: [Google documents that it is closed to new customers](https://developers.google.com/custom-search/v1/overview), and excess use is paid. Existing customers' free quota is not a safe baseline for a new project.
- Do not scrape a search engine's results HTML as an unofficial workaround unless its published terms and automated-access policy explicitly permit the intended use. Lack of an official API does not itself grant permission.
- No paid API, paid search provider, billable model, premium plan, paid dataset download or paid proxy may be called silently or as fallback. A provider that reaches its free quota must fail closed with a clear notice.
- A free API key is acceptable only if getting/using it does not require a paid plan or payment commitment. Keep keys out of Git and logs. Never store them in dataset evidence.
- An HTTP 200 response does not establish truth or a licence. Preserve source URL, retrieval timestamp, publisher, content hash, licence/terms reference, dataset freshness and any extraction limitations.
- Keep requests polite: honour robots.txt and source terms, use a truthful contact-bearing User-Agent, cache repeat requests, respect rate limits and Retry-After, and stop on access refusal. Never bypass login, paywalls, CAPTCHAs or other controls.

## Recommended implementation order

1. Implement catalogue adapters against documented free metadata APIs first, starting with a CKAN-compatible adapter that is configured only with a known public catalogue URL and validated access policy.
2. Add the Indian OGD portal/API adapter after confirming the selected endpoint's key requirements, terms and licence.
3. Add topic-specific free providers only where they add clear value (for example OpenAlex for scholarly research); use Wikimedia as a secondary reference and Common Crawl only for archived URL discovery.
4. Keep a JSON leads-file and CSV/JSON/XLSX upload path as a no-cost fallback.
5. Feed retrieved public CSV/JSON/HTML tables into the existing datasets library, preserving per-row provenance and quarantining malformed/conflicting values. Run deterministic analysis locally with the existing Python/pandas/SQL stack, cache the dataset, and reuse it for follow-up questions.
6. Verify each adapter's free-tier boundary with tests: no payment calls, no automatic provider switching to paid endpoints, clear handling for quota exhaustion, caching, duplicate idempotency, provenance, and source-policy violations.

A free-only engine will not find every source for every topic. It must disclose coverage gaps and invite a manual source/file when a free source is unavailable rather than inventing evidence or charging for access.

## Implementation status (2026-10-28, ADR-047)
| Candidate | Status | Why |
|---|---|---|
| data.gov.uk (CKAN) | **Implemented but disabled** | The API redirects to `ckan.publishing.service.gov.uk`; its robots.txt disallows `/api/`. The provider remains in code but is blocked by the production registry, and no request is sent while disabled. |
| OpenAlex | **Active**, keyless only | Returns scholarly works, not a general structured-dataset search. Keyless use has a daily budget and HTTP 429 stops that provider for the run; no paid fallback. |
| [catalog.data.gov](https://catalog.data.gov/) | Not enabled | Its robots.txt specifies a 10-second crawl delay. The [current official Catalog API guide](https://resources.data.gov/catalog-api/) says the new v4 API replaces the prior CKAN API and requires an api.data.gov key; the old CKAN API is read-only for existing integrations. It therefore does not qualify for this keyless registry. |
| open.canada.ca / data.europa.eu | Not enabled | Automated access and API path compatibility have not been confirmed end to end. Do not enable based on a CKAN-compatible interface alone. |
| data.gov.in | Not enabled | API use and its terms need confirmation for the selected endpoint; user-downloaded files remain supported. |
| Wikimedia, Common Crawl, APISetu, Kaggle | Not enabled | Each candidate needs an individual policy, endpoint, licence and rate-limit review. |
| General web search | None added | No currently verified keyless provider meeting the documented permission and access rules is enabled. |

No replacement source has passed the controlled live smoke test. The current active provider is useful for paper references only; structured-data discovery is therefore not yet operational from registered catalogues.

