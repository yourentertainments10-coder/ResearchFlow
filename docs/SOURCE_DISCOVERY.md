# Source Discovery (Phase 0)

Status: **complete except the portal's terms of use.** The data API was located, its encoding identified (ordinary zlib compression, sections 8 and 9), and event-level data was confirmed from an owner capture (section 10). The one open item is the portal's own terms of use, which blocks automated fetching in Phase 3 but nothing before it.
Checked: 2026-10-04, using page fetches and web search from the Claude workspace. This workspace cannot run a browser, inspect network calls or reach arbitrary sites from its shell, so the checks below are limited to what a page fetch shows.

## 1. Competition facts
| Fact | Value | Evidence |
|------|-------|----------|
| Name | 20th Asian Games, Aichi-Nagoya 2026 | official site title |
| Opening ceremony | 2026-09-19 | OCA page `oca.asia/games/111-aichi-nagoya-2026.html` |
| Closing ceremony | 2026-10-04 | same page |
| Sports and disciplines | 47 listed by the OCA page | same page |
| Medal events | **Not confirmed.** The ChatGPT chat quoted 43 sports and 469 events. Neither figure is verified: the OCA page counts 47 "sports/disciplines", which may be a different level of counting. | Record the official event total in `competitions.official_event_total` only after it is read from an official source |
| Timezone | Asia/Tokyo | host country |

Time matters: the Games end on the day of this check. A complete, final dataset becomes available instead of a moving one, which makes reconciliation easier. It also means results pages may change layout after the Games.

## 2. Candidate sources
### A. Official results portal: `results.asiangames2026.org` (also `gameshub.asiangames2026.org`)
| Checklist item | Finding |
|----------------|---------|
| Owner | Games organisers (linked from the official site "Schedules & Results"). Software shown as "Bornan Web Results" (vendor site: `bornan.sport`) |
| robots.txt | `/robots.txt` returns **404** on `results.asiangames2026.org`. No crawling rules are published. That is not permission by itself |
| Terms of use | No terms found on the portal. Terms exist only on the main site (see B) |
| Access method | **JavaScript single-page app.** The server returns an empty shell ("doesn't work properly without JavaScript"). Routes use `#/...` hash paths |
| Data endpoint | **Unknown.** Not visible in a page fetch. A single-page app like this normally loads JSON from an API; finding it needs the browser's Network tab |
| Event-level data (sport, discipline, event, gender, medallists) | Very likely, since the portal serves live results and medals, but **not confirmed** |
| Official medal table (for reconciliation) | Very likely, **not confirmed** |
| Update frequency, timestamps | Unknown |
| Stability risk | Medium: structure may change after the Games |
| Fixtures saved | None yet |

### B. Main official site: `www.aichi-nagoya2026.org`
| Checklist item | Finding |
|----------------|---------|
| robots.txt | Allows everything except `/wp-admin/` (admin-ajax allowed). Sitemap at `/wp-sitemap.xml` |
| Terms | "Reproduction or transfer in any form without permission is prohibited, except where permitted by the Copyright Act for personal use or citation." No clause about automated access, scraping or data reuse. The disclaimer says the organiser accepts no liability for use of the information |
| Content | WordPress site: sports, venues, news. The sitemap lists `sport`, `venue`, `ajde_events` and `discipline` types. **No results or medal pages.** Useful only for reference data (sport and discipline lists) |
| Event-level medal data | No |

### C. Wikipedia (`2026_Asian_Games_medal_table`, per-sport pages such as `Archery_at_the_2026_Asian_Games`)
| Checklist item | Finding |
|----------------|---------|
| Content | Per-sport pages list every event with gender and format (individual, team, mixed) |
| Freshness | **Not reliable from this check.** The fetched medal table showed China at 11 golds with no update date, and the archery event tables had no medallists filled in. Either the fetched copy was old or the pages lag |
| Licence | Creative Commons attribution and share-alike. Reuse needs attribution |
| Role | Cross-check and event-list reference only. Not a primary source |

### D. News and sports sites (Olympics.com, NDTV, others)
Olympics.com blocks our fetch tool through robots rules, so it is not to be automated. News articles are human-readable summaries with unknown update times. **Cross-check only**, per `DATA_PIPELINE.md` section 1.

## 3. What this means
1. The only source that can realistically give complete event-level data is **A**.
2. **Whether it can be collected automatically, and under which terms, is not yet known.** It needs two things only the owner can do: find the data endpoints, and read whatever terms the portal itself shows.
3. Everything after collection (cleaning, loading, analytics, dashboard) does not depend on that answer, because the manual CSV import path is first-class (`DATA_PIPELINE.md` section 9).

## 4. Owner step: find the data endpoints (about 10 minutes)
In Chrome or Edge, with no extensions needed:
1. Open `https://results.asiangames2026.org/` and look at the page footer or menu for any terms or "legal" link. Copy its text (it decides what automation is allowed).
2. Press F12, choose the **Network** tab, then the **Fetch/XHR** filter. Reload the page.
3. Click **Medals** and then one sport (for example Archery) and one event with a podium. Watch for requests that return JSON.
4. For each interesting request: right-click, **Copy**, then **Copy URL** and **Copy response**. Do not send a HAR export: it also contains your cookies and headers.
5. Send me: the URLs, and the response bodies for (a) the country medal table, (b) the list of events of one sport, (c) the result of one finished event with its medallists.

With those samples I can write the parser and the adapter against real data, tested from saved fixtures, and read the portal's own terms to decide whether automation is acceptable. If the portal forbids automation, the fallback below applies and nothing is bypassed.

## 5. Fallback if no usable endpoint or terms forbid it
- Export or copy the results tables per sport into CSV (format in `DATA_PIPELINE.md` section 9). 47 sports is a few hours by hand, or less if the portal offers downloads or print views.
- Use Wikipedia only to cross-check lists of events and winners, with attribution.
- The pipeline from import onward is identical, so analytics quality does not depend on how the rows arrived.

## 6. Decision (provisional, resolves D1 only when step 4 is done)
| Item | Decision |
|------|----------|
| Primary source | `results.asiangames2026.org`, **pending endpoint discovery and terms review** |
| Fallback | Manual CSV import (always available) |
| Cross-check | Wikipedia per-sport pages (with attribution), news sites for spot checks |
| Reference data | Sport and discipline lists from the main site's sitemap types and the OCA page; confirm the official event total before analytics are published |
| Not used | Olympics.com and any site whose robots rules or terms forbid automation |

## 7. Open items for the final Phase 0 sign-off
- [x] Owner supplies endpoint samples (sections 8 to 10)
- [ ] Owner supplies the portal's terms text (section 4, step 1). **Still open; it gates automated fetching in Phase 3**
- [x] Event total recorded: 469 in the portal's event list (section 10). Loading it into `competitions.official_event_total` is part of Phase 2
- [ ] robots/terms decision recorded here and in `DECISIONS.md` (D1): robots is recorded (404 on both hosts), the terms part waits for the owner (section 10)
- [x] Fixtures saved under `tests/fixtures/sources/<source>/`
- [ ] Whether a `canonical_bytes()` rule is needed for volatile fields (`DATA_PIPELINE.md` section 4)

## 8. Findings from the owner's network capture (2026-10-04)
**Verified** (from the owner's DevTools screenshot and copied request URLs):
- The portal's data comes from a separate host, `back.results.asiangames2026.org`, under the path prefix `/s/AG2026/en/`. Requests are XHR from the app's `index.*.js`.
- URL patterns seen (`SWM` = swimming, `ALL` = all sports, `IND` = India):
  - `/s/AG2026/en/{DISC}/disc/data`
  - `/s/AG2026/en/{DISC}/entries/event/{EVENT_CODE}` (example code `W.50MFR-------------`)
  - `/s/AG2026/en/{DISC}/medals/standings`
  - `/s/AG2026/en/{DISC}/medals/discipline`
  - `/s/AG2026/en/{DISC}/reports/result-book`
  - `/s/AG2026/en/ALL/medals/standings/{NOC}`
- Other requests seen by name only: `config`, `standings`, `params`, `latest`, `multi-medallists`, a country file (`IND`), and one unit result (`ARCMCTEAM------IND01`, size 6.1 kB). Their URLs were not captured.
- All of these answered 200 (some 304, cached) within about 11 to 302 ms. The medal-standings response was about 3 kB.
- `https://back.results.asiangames2026.org/robots.txt` returns 404, so no crawling rules are published on that host either.
- The owner could not use "Copy response" in DevTools.

**Verified from the Claude workspace:** a page fetch of three of these URLs (`ALL/medals/standings`, `SWM/medals/discipline`, `SWM/disc/data`) returned a body that the fetch tool reported as undecodable binary, not JSON or text.

**Not yet known (do not assume):**
- How the body is encoded. Candidates: ordinary compression the browser undoes silently (gzip, brotli, deflate), a binary format (for example MessagePack or protobuf), or encryption or obfuscation that the app's JavaScript reverses.
- What the portal's own terms say about reuse.

**Reading of the event code** (a hypothesis from two samples, to be confirmed against decoded data): the code appears to start with a gender letter (`W.` in `W.50MFR...`), followed by an event identifier padded with dashes, and discipline codes are three letters (`SWM`, `ARC`).

**Next step** (owner): for the `ALL/medals/standings` request, send (a) the response headers, especially `content-type` and `content-encoding` (DevTools, Network, click the request, Headers tab, Response Headers), and (b) the saved response file (open the URL in a browser tab and use Save page as, or Save as from the request). With the file I can inspect the bytes directly.

**Boundary:** if the bytes turn out to be ordinary compression or a standard binary format, parsing them is normal engineering. If they are encrypted or obfuscated so that only the portal's own app can read them, this project will **not** extract a key from the app's code to read them, because `AGENTS.md` rule 9 forbids bypassing access controls. In that case the options are the manual CSV path (section 5), or asking the organisers or the Olympic Council of Asia for a data export or written permission.

**Update, same day (owner pasted the response text from DevTools):** the pasted text begins with the byte `0x78` followed by `0xD5`. `0x78` is the usual first byte of zlib data, but `0x78 0xD5` is not a valid zlib header, and none of zlib, raw deflate or gzip decoded the text. **This proves nothing either way**: copying binary through a text clipboard changes or drops bytes, so only a lossless copy (base64, or a saved file) can answer the question. Entropy of the sample was 7.1 bits per byte, which is consistent with compressed or encrypted data and cannot tell the two apart. Still open: response headers and a lossless copy.

## 9. Resolution: how the payload is encoded (2026-10-04)
The owner's lossless copy (base64 of the 3167 response bytes) settled section 8.

**Verified:**
- The response header says `Content-Type: application/json; charset=utf-8`, but the body is not JSON.
- The body is a zlib stream written out as text: each byte became one character and the text was sent as UTF-8. Reversing that (`UTF-8 -> one byte per character -> zlib -> JSON`) decodes it with no key. This is why browsers show garbage, why "Copy response" fails, and why page-fetch tools report corrupted binary.
- Decoded size 14,492 bytes. Result: a JSON list of 40 country records for `ALL/medals/standings`, each with `Org` (3-letter code), `OrgDesc` (country name), rank fields (`Rk`, `RkTotal`, ties in `RkEq`) and `Count` with `ME_GOLD`, `ME_SILVER`, `ME_BRONZE` and `total`, each split into `M`, `W`, `X` (Men, Women, Mixed) and `total`.
- The table is internally consistent (M+W+X = total and G+S+B = total on every row). The sums across the 40 countries at capture time: 470 gold, 469 silver, 629 bronze, 1,568 medals. Top rows: CHN 169/89/83 (341), JPN 83/95/91 (269), KOR 39/43/68 (150), IND 21/27/37 (85, rank 4), which matches a news report of India finishing 4th with 85 medals.
- Because the decoder needs no key and the portal's own app uses the same open URLs, decoding is ordinary parsing and is implemented in `src/sie/sources/bornan/decode.py`, tested on the real capture (`tests/fixtures/sources/bornan/`).

**What this endpoint gives and does not give:**
- It gives gender (M/W/X) by country but **not sport**. The country x sport x gender analysis needs the per-sport endpoints (`{DISC}/medals/standings`, `{DISC}/medals/discipline`) or event-level data.
- 470 gold against 469 silver is consistent with one gold tie or a data timing difference. This is a guess and is not recorded as a fact. The official total of medal events is still not confirmed (the chat's 469 is unverified; the portal's own event list will give the count).

**Still open before Phase 0 sign-off:**
- [ ] Portal terms of use text (owner)
- [ ] Samples, captured the same way, of: `SWM/medals/discipline`, `SWM/disc/data`, one `entries/event/...` URL, `config`, `params`, `latest`, `multi-medallists`, and the unit result `ARCMCTEAM------IND01` (the last four need their full URLs from the Network tab)
- [ ] Whether events/units carry gold, silver and bronze countries directly (needed for placings), and how team events and double bronze appear

(Sections 8 and 9 are kept as the historical record. Section 10 resolves most of the items above.)

## 10. Event-level capture and Phase 0 outcome (2026-10-04)
The owner supplied a second capture: one JSON file holding 14 decoded responses, taken 2026-10-04 at 11:47 UTC, on the last day of the Games. Trimmed fixtures are in `tests/fixtures/sources/bornan/` (the README there lists what was dropped).

### Verified from the capture
| Question | Finding |
|----------|---------|
| Official event list | `ALL/disc/data` lists **59 disciplines and 469 events**: Men 221, Women 207, Mixed 21, Open 20. None is marked para. 156 are team events (`IsTeam`). This matches the 469 quoted in the earlier chat, now read from the portal's own list. A programme total does not by itself prove that every event has produced medals |
| Per-event medal rows | `{DISC}/medals/discipline` returns **one row per placing**: `Medal` (`ME_GOLD`, `ME_SILVER`, `ME_BRONZE`), `Order`, `Org` (3-letter code), `OrgDesc`, `Event`, `EventDesc`, `Gender`, `DateRaw` (with a +09:00 offset), `Name`, `Type`, `Reg`, and `Members` for teams. `ALL/medals/discipline` returned an empty list, so collection needs one request per discipline (59 requests, about two minutes at the 2-second rule) |
| Slots and ties | `Order` is the slot. A tie shows as `Order` 2 on the tied medal, with the next medal absent. Swimming, Men's 100m Breaststroke: two golds (CHN and JPN), no silver. Swimming, Men's 800m Freestyle: one gold, two silvers (JPN and KOR), no bronze. Nothing is inferred; it is in the rows |
| Team events | One row per team (`Type` T): `Org` is the country, `Name` is the country name, `Reg` is a team code such as `ARCMCTEAM------IND01`, and `Members` lists the athletes. This matches the counting rule of one placing per team (`DOMAIN_MODEL.md` section 4) |
| Event codes | The `Event` of a medal row is the event key plus `.` plus a phase code. Seen: `----` (single-phase events) and `FNL-` (archery finals). The first letter of an event key is M, W, X or O (Men, Women, Mixed, Open) and equals the row's `Gender` on all 153 rows |
| Reconciliation | Counting medal rows by country, medal and gender gives **exactly** the portal's own standings for Swimming (8 countries) and Archery (7 countries): 0 mismatches, ties included. Phase 3 reconciliation will run this same check for every discipline |
| The 470 and 469 puzzle | Section 9 noted 470 golds against 469 silvers. With 469 events, the two swimming ties give that pattern: the gold tie adds one gold and removes one silver, the double silver adds one silver, so 470 gold and 469 silver. This fits. It is not proof for other disciplines, and bronze (629) was not re-derived |

### Personal data
The responses carry athlete birth dates, including athletes who are minors, and complete participant lists. Decision: the pipeline never stores birth dates, does not use the `entries/...` endpoints, and the committed fixtures have birth dates removed. Medallist names are public results and are kept (`SECURITY.md` section 1). Every parser for this source must drop birth dates and must not copy `entries` data.

### Not yet verified (a sample is needed for each; do not assume)
- How double bronze appears in combat sports such as Judo, Taekwondo and Boxing. `Order` 2 on Bronze is expected, but no such sample exists yet.
- Ties in team events, how pair events and doubles are typed (whether `IsTeam` is true for them), and how Open events (Esports) appear in medal rows.
- Phase codes other than `----` and `FNL-`.
- Whether the portal removes or rewrites a row when a medal is reallocated, and how a withheld medal is shown.
- `config.storage` (2026-10-04T03:19:55+00:00) looks like a data timestamp. This is a hypothesis; it could become `source_updated_at`.

### Decision for D1 (Phase 0 outcome)
| Item | Decision |
|------|----------|
| How event-level data is obtained | Primary: the portal's JSON API, using `ALL/disc/data` (event list), `{DISC}/medals/discipline` (placings), and `ALL/medals/standings` with `{DISC}/medals/standings` (official table, for reconciliation). Fallback: manual CSV import, always available |
| robots.txt | 404 on both `results.asiangames2026.org` and `back.results.asiangames2026.org`. No crawling rules are published. That is not permission |
| Terms of use | **Not found.** Not recorded anywhere we can read. Under `AGENTS.md` rule 9 this must be settled by the owner (read the portal's footer or legal link, or ask the organisers or the Olympic Council of Asia in writing) before any automated fetching. Until then, data enters through manual import of owner captures |
| Effect on the roadmap | Phases 2, 4 and 5 are unaffected. Phase 3 can build the parser, the raw store and the reconciliation on the saved fixtures now. Only the live adapter that fetches from the portal waits for the terms decision |


## 11. Full capture and reconciliation (2026-10-04, 16:21 UTC)
The owner ran the crawl snippet: `{DISC}/medals/discipline` and `{DISC}/medals/standings` for all 59 disciplines (2-second gap between requests).

| Check | Result |
|-------|--------|
| Medal rows | **1568**, equal to the official `ALL/medals/standings` total. By medal: 470 gold, 469 silver, 629 bronze, all equal to the official table |
| Events | **469** distinct events with medal rows, equal to the 469 events of `ALL/disc/data`; every event has a gold. 0 event keys outside the official list |
| Country totals | 40 countries, 0 differences against the official table |
| Per-discipline, country, medal and gender | 0 mismatches over 59 disciplines, once the gender rule below is applied |
| Ties and double bronze | Seen in 22 disciplines (`Order` 2). Combat sports show two bronzes (`Order` 1 and 2). Swimming has the one gold tie and one double silver |

**Gender rule (found by reconciliation, now in the parser).** The `Gender` field of a medal row is the *athlete's*. For Open events (20 of them: all 11 Esports and 7 Equestrian events, one of two Artistic Swimming events, one of 11 Taekwondo events) it can be M or W (23 rows). The official table takes the event's gender from the first letter of the event code and counts Open events under Mixed. Using the row field gave 107 mismatches in ELS, EQU, SWA and TKW; using the event-code letter gave 0. This is the rule in `DOMAIN_MODEL.md` section 4.6 (gender belongs to the event). Reports show Open events inside Mixed, as the official table does.

Still open: the portal's terms of use (gates the live adapter, not manual import), reallocation and withheld-medal behaviour (needs a later capture to compare).

## 12. Terms of use: none found (2026-10-04)
The owner looked on the results portal for a Terms or Legal link and there is none on the page. No robots.txt either (section 10). Absence of published terms is not permission, so the rules stay conservative:
- No automated fetching from the portal by the pipeline until the organisers confirm it in writing (`AGENTS.md` rule 9). Data enters through a capture the owner runs in their own browser, which reads the same public URLs the portal's own page reads, with a 2-second gap.
- The raw capture (athlete names, team members) is kept out of Git. Git carries only `reports/placings.csv` (country, sport, event, gender, medal, slot: no person data) and the reports built from it.
- Output credits the source ("Official results portal, AG2026") and is for non-commercial analysis.
