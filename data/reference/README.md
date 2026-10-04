# Reference data (PROVISIONAL starter files)

These CSVs are loaded by `sie seed-reference`. They are a starting point, **not verified against the
official source**. Phase 0 (see `docs/SOURCE_DISCOVERY.md`) must confirm or replace them before real
data is loaded. Unknown values in incoming data are quarantined, never auto-created, so a gap here
shows up as a quarantine row for you to resolve, not as silent wrong data.

| File | Status |
|------|--------|
| `countries.csv` | The 45 members of the Olympic Council of Asia, with NOC codes. Region uses the UN geoscheme and is informational only (no v1 metric uses it). Confirm the participating list and spellings in Phase 0 |
| `country_aliases.csv` | Common alternative spellings and ISO codes. Each code and name is also an alias automatically. Add whatever your source uses |
| `sports.csv`, `disciplines.csv`, `sport_aliases.csv` | **Provisional.** Seeded from the sport names mentioned in the planning chat (themselves unverified). The Games reportedly have 43 sports; the real list and the sport/discipline hierarchy come from the official source in Phase 0. `double_bronze` is only set where certain (Boxing) |
| `gender_aliases.csv` | Maps spellings to Men / Women / Mixed / Open. Matching ignores case and punctuation, so `Men's` and `Mens` are the same key |
| `competitions.csv` | Dates from a secondary source (Wikipedia, 19 Sep to 4 Oct 2026). `official_event_total` is deliberately left empty until the official figure is confirmed |
