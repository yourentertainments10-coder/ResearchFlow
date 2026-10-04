# Reference data (PROVISIONAL starter files)

These CSVs are loaded by `sie seed-reference`. They are a starting point, **not verified against the
official source**. Phase 0 (see `docs/SOURCE_DISCOVERY.md`) must confirm or replace them before real
data is loaded. Unknown values in incoming data are quarantined, never auto-created, so a gap here
shows up as a quarantine row for you to resolve, not as silent wrong data.

| File | Status |
|------|--------|
| `countries.csv` | The 45 members of the Olympic Council of Asia, with NOC codes. Region uses the UN geoscheme and is informational only (no v1 metric uses it). Confirm the participating list and spellings in Phase 0 |
| `country_aliases.csv` | Common alternative spellings and ISO codes. Each code and name is also an alias automatically. Add whatever your source uses |
| `sports.csv`, `disciplines.csv`, `sport_aliases.csv` | **Verified against the official portal (2026-10-04, `docs/SOURCE_DISCOVERY.md` section 10).** All 59 portal disciplines are listed under 49 sports: Aquatics (Swimming, Diving, Artistic Swimming, Water Polo), Cycling (Road, Track, Mountain Bike, BMX Racing, BMX Freestyle), Gymnastics (Artistic, Rhythmic, Trampoline) and Canoe (Sprint, Slalom) group several disciplines; every other discipline is its own sport. Basketball and 3x3, Volleyball and Beach Volleyball, Baseball and Softball are kept separate because the repository does not show the official grouping (ADR-019). `double_bronze` is true for the 19 sports where the official data has two bronze placings in an event (`reports/placings.csv`, slot 2); Athletics has one such row, which is a tie, not a double-bronze sport |
| `gender_aliases.csv` | Maps spellings to Men / Women / Mixed / Open. Matching ignores case and punctuation, so `Men's` and `Mens` are the same key |
| `competitions.csv` | Dates from a secondary source (Wikipedia, 19 Sep to 4 Oct 2026). `official_event_total` is deliberately left empty until the official figure is confirmed |
