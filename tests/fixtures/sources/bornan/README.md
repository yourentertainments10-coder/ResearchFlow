# Fixtures: results portal API (vendor "Bornan Web Results")

Real responses captured by the project owner from the public results portal on 2026-10-04,
about 11:55 IST (06:25 UTC), using the browser's own request (see docs/SOURCE_DISCOVERY.md section 8).

| File | What it is |
|------|-----------|
| `ALL_medals_standings.raw` | The response body exactly as received: 3167 bytes of UTF-8 text. `GET https://back.results.asiangames2026.org/s/AG2026/en/ALL/medals/standings`, `Content-Type: application/json; charset=utf-8` |
| `ALL_medals_standings.decoded.json` | The same payload after decoding (zlib, then JSON), 14,492 bytes. Used to check the decoder |

A second capture was supplied by the owner on 2026-10-04 at 11:47 UTC (about 17:17 IST), as one JSON file of
decoded responses. The files below are trimmed from it. They are not byte-for-byte responses.

| File | Source request (under `/s/AG2026/en/`) | Trimming |
|------|----------------------------------------|----------|
| `ALL_disc_data.trimmed.json` | `ALL/disc/data` (59 disciplines, 469 events) | Kept discipline code and name and, per event, `EvKey`, `Order`, `Desc`, `IsTeam`, `IsPara`. Dropped `Days`, `Locations`, `Extensions` and the capability flags |
| `SWM_medals_discipline.json` | `SWM/medals/discipline` (123 placing rows) | `BirthDate` removed, also inside team `Members` |
| `ARC_medals_discipline.json` | `ARC/medals/discipline` (30 placing rows) | `BirthDate` removed, also inside team `Members` |
| `SWM_medals_standings.json` | `SWM/medals/standings` | None |
| `ARC_medals_standings.json` | `ARC/medals/standings` | None |

Deliberately not stored: `entries/event/...` (full participant lists with birth dates, many of them minors, and
not needed for medal analysis), `schedule/...` and `config` (not used yet).

The captures are snapshots taken while the Games were ending, not final data.
Facts only (events, placings, country medal counts). Birth dates are never kept (docs/SOURCE_DISCOVERY.md
section 10). Add new captures next to these files and say how they were trimmed.
