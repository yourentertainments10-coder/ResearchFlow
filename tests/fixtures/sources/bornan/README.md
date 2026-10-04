# Fixtures: results portal API (vendor "Bornan Web Results")

Real responses captured by the project owner from the public results portal on 2026-10-04,
about 11:55 IST (06:25 UTC), using the browser's own request (see docs/SOURCE_DISCOVERY.md section 8).

| File | What it is |
|------|-----------|
| `ALL_medals_standings.raw` | The response body exactly as received: 3167 bytes of UTF-8 text. `GET https://back.results.asiangames2026.org/s/AG2026/en/ALL/medals/standings`, `Content-Type: application/json; charset=utf-8` |
| `ALL_medals_standings.decoded.json` | The same payload after decoding (zlib, then JSON), 14,492 bytes. Used to check the decoder |

The raw file is a snapshot taken while the Games were ending, not final data.
Facts only (country medal counts). Do not edit these files; add new captures next to them.
