# Domain Model

Four concepts that every table, metric and conversation must keep apart. Most analytics bugs come from mixing them up.

## 1. Definitions
| Concept | Meaning | Identity | Stored in |
|---------|---------|----------|-----------|
| **Event** | One contested unit of competition that produces a podium, for example "Women's Compound Team" in Archery. Carries sport, discipline, gender (Men / Women / Mixed / Open), participation (Individual / Pair / Team), date and status | competition + discipline + name + gender | `events` |
| **Medal placing** | One slot on an event's podium: (Gold, 1), (Silver, 1), (Bronze, 1), and (Bronze, 2) when two bronzes are awarded. A placing is a position. It is not a person and not a country. It has versions over time (reallocation) | event + medal + slot (+ validity period) | `placings` |
| **Entrant (athlete/team)** | The athlete, pair or team that occupies a placing, and the single country it represents | competition + country + kind + name | `entrants` |
| **Country medal** | A current placing credited to a country. This is the unit that medal tables and all analytics count | = a current placing together with its country | view `v_medal_facts` |

## 2. How they relate
```
Event 1 ---- 0..n Placing        (version rows; at most one current per medal slot)
Placing n ---- 1 Country         (the country credited)
Placing 0..1 ---- 1 Entrant      (who stood on the podium; may be unknown in some sources)
Entrant n ---- 1 Country
Country medal = current Placing + Country      (a view, not a table)
```
A typical podium has 3 placings; combat sports have 4 (two bronzes). Ties change the slot counts according to the sport's rules.

## 3. Worked examples
**Team event.** "Women's Compound Team" (Archery, Women, Team):

| Placing | Entrant | Country | Country medal |
|---------|---------|---------|---------------|
| Gold 1 | India women's compound team (three archers) | IND | 1 gold for IND |
| Silver 1 | Korea team | KOR | 1 silver for KOR |
| Bronze 1 | China team | CHN | 1 bronze for CHN |

Three archers on a team still make one entrant, one placing and one country medal.

**Sweep.** An individual event where one country takes gold and silver has two placings, two entrants and two country medals for that country.

## 4. Counting rules
1. Medal totals are the number of country medals (current placings).
2. Team or pair event: one placing, one entrant, one country medal, whatever the number of athletes.
3. A country holding two placings in one event has two country medals. The sweep metric counts events where one country holds two or more placings.
4. Two bronzes: Bronze slots 1 and 2, both counted.
5. Ties: stored as the source gives them (for example two Gold placings in slots 1 and 2), flagged `is_tie`. The next medal is omitted where the sport's official rule says so. A tie never overwrites another placing.
6. Gender, discipline and participation belong to the event, never to the entrant. Women's analysis filters on event gender. An athlete's personal gender is not stored in v1.
7. If a source credits one placing to more than one country, the row is quarantined as `MULTI_COUNTRY_ENTRANT` until the owner records the official rule in `DECISIONS.md`.
8. A vacant or withheld placing has no row. The event stays `completed` with fewer placings and carries a note.
9. Reallocation: the old placing version is closed (`is_current = 0`, `valid_to` set) and kept. A new version is created. Only current versions are counted. Procedure: `DATABASE.md` section 6.
10. What a source *claimed* (a source observation) is not a placing. Placings hold accepted values. Observations are evidence (`DATA_PIPELINE.md` section 7).

## 5. Where each concept is used in analytics
| Input | Counts |
|-------|--------|
| `T_cs` and every medal count | country medals |
| `E_s` (events available) | events |
| `Completed_s` | events with status `completed` |
| Podium sweep | events where a country holds two or more current placings |
| Women's, Men's, Mixed share | country medals grouped by event gender |
| Entries (not in v1, needed for true efficiency) | entrants |

## 6. Invariants (each has an automated test)
- **I1**: every current placing has exactly one country. If it has an entrant, the entrant's country equals the placing's country.
- **I2**: for each event there is at most one current placing per (medal, slot).
- **I3**: total country medals equals the number of current placings.
- **I4**: an event's gender is never derived from or changed by its entrants.
- **I5**: two current placings of the same country in one Team event require two distinct entrants. Otherwise the second is a duplicate.
- **I6**: every closed placing has `valid_to` set and counts in no metric.

## 7. Vocabulary in code
Classes: `Event`, `Placing`, `Entrant`, `CountryMedal`. Columns: `event_id`, `placing_id`, `entrant_id`, `country_id`. Never use bare "medal" as a table or class name. `medal` is the *type* attribute (Gold, Silver, Bronze) of a placing.
