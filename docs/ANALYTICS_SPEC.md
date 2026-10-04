# Analytics Specification

All metrics are computed by code from `v_medal_facts`. Nothing here is ranked by opinion. Every metric has a formula, an input grain and a sample-size warning rule.

## 1. Notation
- `c` country, `s` sport (or discipline), `g` gender category (Men / Women / Mixed / Open / All)
- `G, S, B` gold, silver, bronze counts; `T = G + S + B`
- `T_c` all medals of country `c`; `T_s` all medals awarded in sport `s`; `T_all` all medals awarded
- `E_s` number of events in sport `s` (known from the events table; use the official total when available, otherwise events seen)
- `Completed_s` completed events in sport `s`

Medals are counted as **country medals**: one per current medal placing, credited to that placing's country (`DOMAIN_MODEL.md`). A team medal counts once. A country holding two placings in one event has two country medals.

## 2. Grains (every metric must be available at each)
1. Country
2. Country x Sport
3. Country x Sport x Gender
4. Country x Discipline (and x Gender)
5. Sport and Sport x Gender (all countries)
6. Competition overall

## 3. Base metrics
| Metric | Formula |
|--------|---------|
| Gold / Silver / Bronze / Total | counts |
| Points 3-2-1 | `3G + 2S + 1B` (documented weighting; configurable) |
| Gold ratio | `G / T` |
| Podium rank | sort by G, then S, then B (the standard official order); also rank by Total and by Points |

## 4. Share and dependency
| Metric | Formula | Reading |
|--------|---------|---------|
| Sport share of country | `share_cs = T_cs / T_c` | How much of the country's medals one sport supplies |
| Gender share of country | `T_cg / T_c` | Women's, men's, mixed contribution |
| Women's medal share | `T_c,Women / (T_c,Men + T_c,Women + T_c,Mixed + T_c,Open)` | Also shown excluding Mixed |
| Market share in sport | `T_cs / T_s` | How much of a sport's medals one country holds |
| Gold market share | `G_cs / G_s` | Dominance at the top of the podium |

## 5. Concentration and diversification (per country)
| Metric | Formula |
|--------|---------|
| HHI | `sum_s (share_cs)^2`, range (1/N, 1) |
| Effective number of sports | `1 / HHI` |
| Top-1 share, Top-3 share | share of the largest 1 or 3 sports |
| Sport coverage | count of sports with `T_cs >= 1`; also count with `G_cs >= 1` |
| Shannon entropy (optional) | `-sum share * ln(share)` |
Interpretation labels (configurable thresholds): HHI above 0.25 = concentrated; 0.10 to 0.25 = moderate; below 0.10 = diversified. State the thresholds in the UI.

## 6. Specialisation (revealed comparative advantage)
`RCA_cs = (T_cs / T_c) / (T_s / T_all)`
- RCA > 1: the country gets more of its medals from sport `s` than the average country does, so it is specialised there.
- RCA < 1: under-represented.
- Shown at sport and sport x gender level. Hide cells where `T_cs < 2` (small sample).

## 7. Opportunity and efficiency
| Metric | Formula | Notes |
|--------|---------|-------|
| Event conversion rate | `T_cs / Completed_s` | Medals per completed event. Can exceed 1 because up to 3 to 4 medals per event exist, but one country normally holds at most 1 per event (team) or a few in sports with multiple entries |
| Gold conversion rate | `G_cs / Completed_s` | Share of events in the sport won by the country |
| Podium sweep count | events where one country took 2 or more podium places | Only where the sport allows it |
| Completion-adjusted view | metrics shown with `Completed_s / E_s` | Prevents conclusions from partial data |
True "medal efficiency" (medals per entry) needs entry data. Entries are not in v1 scope. Do not label conversion rate as efficiency against entries.

## 8. Strength tiers per country (descriptive, not judgemental)
Using Pareto ordering of the country's sports by points:
- **Core**: sports that together make the first 60 percent of the country's points
- **Secondary**: the next 30 percent
- **Tail**: the last 10 percent
- **No medals**: sports where the country has zero medals
- A sport is additionally tagged **Specialised** if `RCA >= 1.5` and `T_cs >= 3`, and **Gold-heavy** if gold ratio is at least 0.5 with `T_cs >= 3`.
"Weak" in this project means *no medals or tail-only in a sport where other countries medal*, shown with the sport's size (events available). The UI never uses the word "weak" without that context.

## 9. Gender analysis
For each country and sport:
- Men / Women / Mixed counts side by side.
- Women's strongest sports: ordered by women's points, with women's market share and RCA within women's events.
- Gender gap: `Women_points / (Women_points + Men_points)`; 0.5 is parity, shown only where combined total is at least 3.
- Mixed and Open are reported separately and never added silently to Women or Men.
Cross-country: women-only versions of rankings, concentration and RCA.

## 10. Time-based (needs snapshots)
Snapshots follow the identity rules in `DATABASE.md` section 8: a `change` snapshot only when the data fingerprint or the analytics version changes, plus one `daily` snapshot per competition-timezone day. Every snapshot records the analytics version.
- Rank trajectory per country per snapshot.
- Medals gained since the previous snapshot, by sport and gender.
- Sports newly medalled by a country.
- Completion progress per sport.

## 11. Data-quality aware output
Every table and chart carries: snapshot time, % events completed (overall and for the sport), reconciliation status, and a "partial data" flag when `Completed_s / E_s < 1`. Metrics based on fewer than 3 medals are shown but greyed, with the note "small sample".

## 12. Outputs
**Tables (CSV and Excel sheets):** `country_summary`, `country_sport`, `country_sport_gender`, `sport_summary`, `gender_summary`, `concentration`, `rca`, `conversion`, `tiers`, `changes`, `reconciliation`, `quarantine`.

**Charts:** medal table bar, country x sport heatmap, stacked share bars, concentration scatter (effective sports vs total medals), RCA bubble chart, men vs women diverging bars, trajectory lines, sport market-share treemap.

**Insight text (rule-based templates, no LLM needed):**
- "{country}: {share:.0%} of medals come from {sport}; effective number of sports is {n:.1f} ({label})."
- "{country} specialises in {sport} (RCA {rca:.1f}); {gold} golds from {total} medals."
- "{country}'s women contribute {share:.0%} of medals, led by {sport}."
Optional LLM step may only rephrase these generated sentences, with the numbers locked.

## 13. Implementation rules
- Each metric is a pure function `f(df) -> df` in `analytics/metrics.py`, with docstring citing this document.
- Division by zero returns null, not zero.
- Invariants tested: sum of country-sport totals equals total medals in `v_medal_facts`; shares per country sum to 1; sum over genders equals the All value.
- Changing a formula needs an update to this document and a decision record.
