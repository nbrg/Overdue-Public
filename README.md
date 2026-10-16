# Overdue — Yellow Card Watch

[![tests](https://github.com/nbrg/Overdue-Public/actions/workflows/tests.yml/badge.svg)](https://github.com/nbrg/Overdue-Public/actions/workflows/tests.yml)

A Python/pandas tool that flags football players who are **overdue a booking**: players who
normally pick up cards at a steady rate but have gone an unusually long run of games without one.
It turns a store of per-match card logs into daily, weekly and per-fixture reports, as Markdown
and as interactive, dependency-free HTML pages, and it scores its own predictions afterwards.

This public version runs on a **synthetic sample world**: made-up leagues, clubs and players whose
matches run up to today. Every command works end to end with no network access and no accounts.
To use real data, swap [`src/overdue/source.py`](src/overdue/source.py) for an adapter that
returns the same frames (see [Plugging in real data](#plugging-in-real-data)).

> Personal project, shared for its code. It is not betting advice.

## Quick start

```bash
pip install -e ".[dev]"
overdue backfill            # build the appearance store from the sample source
overdue refresh-positions   # cache positions (enables goalkeeper exclusion)
overdue report              # today's overdue players → reports/<date>.md + .html
pytest -q                   # ~150 tests, no network
```

Open `reports/<date>.html` in a browser for the interactive view.

## How "overdue" is decided

For each player, over a rolling baseline window (the last 2 seasons by default):

| term | meaning |
|------|---------|
| **appearance** | a match the player featured in |
| **card rate** `r` | `cards / appearances`, e.g. `0.2` = a card every 5 games |
| **expected interval** | `1 / r`, the average number of games between cards |
| **streak** `k` | consecutive most-recent appearances with no card |
| **overdue ratio** | `k · r`: about 1 is a normal wait, higher is more overdue |

A player is only scored if they clear a **sample gate** (≥ 12 appearances and ≥ 3 cards in the
window) and is flagged when the overdue ratio is **≥ 1.5**. On top of that, the report keeps only
regulars likely to play: a minimum share of recent starts, no long run of missed games (a proxy for
injury or suspension), and no goalkeepers. All thresholds live in
[`src/overdue/config.py`](src/overdue/config.py).

Appearances and cards are pooled across all competitions (league, Europe, cups), but only players
whose club plays a tracked domestic league are scored, so a midweek European booking correctly
resets a streak.

## Commands

| command | what it does |
|---------|--------------|
| `report` | today's overdue players, with fixture, referee, head-to-head and pick score |
| `report-week` | one table per remaining day of the week |
| `report-team <club>` | every player in one game's two squads, overdue or not |
| `report-late [club]` | today's benches ranked by cameo booking rate (late-card targets) |
| `assess [--date]` | scores a past day's flags: hit rate, base rate and lift |
| `explain <player>` | a player's metrics, recent games and why they are or aren't flagged |
| `refresh-odds --book Bet365` | paste `"<Player> 11/5"` lines to fill the Best Price / Edge columns |
| `check-teams` | finds club-name mismatches between the schedule and the store |
| `backfill`, `refresh-events`, `refresh-positions` | build or top up the local store |

Report commands catch up on newly played matches first (`--no-ingest` skips that).

### The HTML reports

Each report also writes a self-contained HTML page: sortable and filterable tables with header
tooltips, a fixtures panel, view presets, a 0–100 **Pick** score, a **Your price → Edge** input
against the model's fair odds, a starred shortlist with CSV export, and a click-to-expand panel per
player with their season-by-competition breakdown and the team's last 20 games.

### Optional local inputs

Two small JSON files in `data/` sharpen the reports when present:

- `data/lineups.json`: today's line-ups, `{"date": "YYYY-MM-DD", "fixtures": [{"home", "away",
  "confirmed", "starters", "bench", "out"}]}`. Injured or suspended players drop out of every
  report, and `report-late` uses the confirmed benches.
- `data/referee_appointments.json`: `[{"home", "away", "referee", "cards_per_game"}]`, used for the
  referee-adjusted odds column before the schedule names the official.

## Project layout

```
src/overdue/
  source.py        match data source (synthetic sample world; swap for a real adapter)
  ingest.py        backfill + incremental catch-up into the store
  store.py         parquet appearance / event stores + run metadata
  model.py         rate, streak, overdue ratio, availability gates (pure, unit-tested)
  report.py        ranked view model shared by every output
  targets.py       sub-cameo (late-card) targets, team squads, game logs
  assessment.py    scores a past day's predictions, incl. the "super sub" rule
  referees.py      referee strictness from the schedule
  h2h.py           head-to-head card averages
  lineups.py       line-up status store (starter / bench / out)
  appointments.py  referee appointments store
  odds_book.py     pasted-price parsing, odds cache, name matching to report rows
  teamnames.py     derby-safe club-name matching across spellings
  render_text.py   Markdown reports + terminal summaries
  webview.py       interactive HTML reports
  cli.py           the `overdue` command
tests/             ~150 unit tests on synthetic frames (no network)
```

## Testing

```bash
pytest -q
```

The tests cover the model maths and gates, the store, report assembly, rendering, name matching
(accents, mononyms, aliases, derby pairs such as Manchester United and City), the assessment and
super-sub logic, and the sample source itself, which is checked for internal consistency (eleven
starters a side, minutes that add up, events that match appearances). They run on every push and
pull request via the [`tests`](.github/workflows/tests.yml) workflow.

## Plugging in real data

`source.py` is the only module that knows where matches come from. A real adapter needs to provide:

- `read_schedule(seasons, leagues=None)`: one row per fixture with `league, season, game, game_id,
  date, start_time, home_team, away_team, referee, notes, played`;
- `read_match_appearances(game_ids, seasons, leagues=None)`: one row per player per match in the
  `store.COLUMNS` schema (without `date`);
- `read_match_events(game_ids, seasons, leagues=None)`: card and substitution events with minutes;
- `read_player_positions(seasons=None, report=None)`: `player -> GK/DF/MF/FW`.

Respect the terms of use of wherever that data comes from.
