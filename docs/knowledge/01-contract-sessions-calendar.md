# 01 · VN30F1M contract, sessions and the tape-derived calendar

> **Desk relevance.** Every later number — fees, basis, bars, roll exclusions, holdout ranges —
> inherits the calendar. A wrong holiday or a bar that straddles the lunch break silently
> corrupts a backtest.

## Contract facts (HNX derivatives, KRX era)

| Item | Value |
|---|---|
| Underlying | VN30 index (HOSE) |
| Multiplier | 100,000 VND per index point |
| Tick | 0.1 point |
| Daily limit | ±7 % of the reference price |
| Initial margin (VSDC) | a parameter — 17 % in the cost model; VSDC has moved it between 13 % and 24 % |
| Last trading day | third Thursday of the contract month, moved to the previous trading day on a holiday |
| Final settlement price | simple average of VN30 over the last 30 minutes, excluding the 3 highest and 3 lowest values |

| Session | Time (ICT) | Orders |
|---|---|---|
| Opening periodic auction | 08:45–09:00 | ATO, LO (no cancel) |
| Continuous, morning | 09:00–11:30 | LO, MOK, MAK, MTL |
| Lunch break | 11:30–13:00 | — |
| Continuous, afternoon | 13:00–14:30 | LO, MOK, MAK, MTL |
| Closing periodic auction (ATC) | 14:30–14:45 | ATC, LO (no cancel) |

The futures open 15 minutes before the cash market, whose own opening auction runs 09:00–09:15 —
so the VN30 index has **no prints before 09:15**, and any futures-vs-index series really starts at
09:15. Source for the session and settlement rules: a broker's KRX-era derivatives rulebook
(VPBank Securities user guide, checked 2026-09-23); the project's session-phase module encodes the
same ladder (pre-open < 08:45 ≤ ATO < 09:00 ≤ continuous AM < 11:30 ≤ lunch < 13:00 ≤ continuous
PM < 14:30 ≤ ATC < 14:45 ≤ closed). One older constant in the source tree labelled the opening
auction 09:00–09:15 (the cash-market time); it is not used by the modules distilled here.

## The session has two closes

PRE_REGISTRATION amendment A16 measured it: 14:31–14:44 prints almost nothing, the 14:45 closing
cross carries about **49×** the volume of a normal minute, the daily close equals the 14:45 cross on
98 % of sessions, and the median gap between the last continuous print (14:30) and the cross is
1.5 points — larger than the conservative cost bar on 53 % of sessions. A strategy that "exits at
the close" must say which close it means.

## A calendar derived from the tape, not typed from a website

`historical/research_lab/calendar_vn.py` obtained every holiday **by subtraction**: the VN30F1M M1
table held 2,197 session dates (2017-11-06 → 2026-08-28); the 103 weekdays missing from it were each
classified — 99 statutory holidays or official swap days, 4 holes in the tape on days the market
traded. Where a published rule and the data disagreed, the data won, and the two disagreements are
annotated at their dates. The sets are committed constants with an import-time self-check, so an
unreachable database can never yield an empty calendar.

Two functions matter downstream:

- `session_adjacency(d_from, d_to)` — calendar nights between two sessions;
- `gap_kind(d_from, d_to)` — overnight / weekend / holiday gap, so overnight statistics are
  **stratified** instead of mixing a one-night gap with a five-session Tet closure.

## Bars must respect four boundaries

The continuous session has hard breaks at the open, 11:30, 13:00 and the closing auction. A bar that
spans a break mixes two price-formation regimes. The project's information-bar module (not
distributed here: it depends on a JIT compiler) measured it on the 2017–2023 dev history: of 23,223 naive volume bars, 9.7 % were contaminated
(3.8 % spanned an overnight gap, 5.9 % the lunch break) and the worst "bar" covered 9 days across
the 2022 Tet holiday. The information bars force-close at all four boundaries and flag the
truncated bar `partial=True` instead of dropping it.

## Code, tests, demo

| What | Where |
|---|---|
| Calendar | `historical/research_lab/calendar_vn.py` |
| Expiry rule | `historical/research_lab/daily.py::third_thursday` |
| Synthetic market on the same calendar | `samples/synthetic/session_grid.py` |
| Demo | `demos/03-basis-screen.py` (expiry markers and roll exclusion on the real 2021 calendar) |

## Limits

The calendar is verified through 2026-08-28 and provisional to 2027-12-31; later dates need a
re-derivation against the tape (`verify_against_clickhouse`).
