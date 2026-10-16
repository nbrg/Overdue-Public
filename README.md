# Overdue

[![tests](https://github.com/nbrg/Overdue-Public/actions/workflows/tests.yml/badge.svg)](https://github.com/nbrg/Overdue-Public/actions/workflows/tests.yml)

Overdue looks for football players who are due a yellow card. Some players get booked at a
fairly steady rate. When one of them goes a lot longer than usual without a card, they show up
here. The tool builds daily, weekly and per-match reports from a store of match logs, in
Markdown and as interactive HTML pages, and it checks afterwards how its picks did.

## About this repo

This is a cut-down public copy of a private project I use day to day. It's here to show the
code: the model, the reports, the tests and the CI setup. The private version has data
integrations and some extra tooling that I've left out.

To make it runnable on its own, this copy uses a generated sample world instead of real data.
The leagues, clubs and players are made up, and their fixtures run up to the current date, so
every command works offline with no accounts or keys. [`src/overdue/source.py`](src/overdue/source.py)
is the only module that knows where data comes from, so a real source can be swapped in there
(see [Using real data](#using-real-data)).

It's a personal project and nothing in it is betting advice.

## Quick start

```bash
pip install -e ".[dev]"
overdue backfill            # build the appearance store from the sample source
overdue refresh-positions   # cache player positions (used to skip goalkeepers)
overdue report              # today's overdue players → reports/<date>.md and .html
pytest -q
```

Open `reports/<date>.html` in a browser for the interactive version.

## How it decides a player is overdue

Each player is looked at over a rolling window (the last two seasons by default):

| term | meaning |
|------|---------|
| appearance | a match the player played in |
| card rate `r` | cards ÷ appearances, so `0.2` means a card every 5 games |
| expected gap | `1 / r`, the usual number of games between cards |
| streak `k` | how many of their most recent appearances had no card |
| overdue ratio | `k · r`. Around 1 is a normal wait; higher means more overdue |

A player needs at least 12 appearances and 3 cards in the window to be scored, and is flagged
once the overdue ratio reaches 1.5. The report then keeps only regular starters who are likely
to play: they need a minimum share of recent starts, no long run of missed games (a rough sign
of injury or suspension), and goalkeepers are left out. The thresholds are all in
[`src/overdue/config.py`](src/overdue/config.py).

Appearances and cards count across every competition (league, Europe, cups), but only players
whose club is in a tracked domestic league get scored. That way a midweek European booking
still resets a player's streak.

## Commands

| command | what it does |
|---------|--------------|
| `report` | today's overdue players, with fixture, referee, head-to-head and a pick score |
| `report-week` | one table for each remaining day of the week |
| `report-team <club>` | every player in one match's two squads, overdue or not |
| `report-late [club]` | today's substitutes ranked by how often they get booked after coming on |
| `assess [--date]` | scores a past day's picks: hit rate, base rate and lift |
| `explain <player>` | one player's numbers, recent games and why they are or aren't flagged |
| `refresh-odds --book Bet365` | paste `"<Player> 11/5"` lines to fill in the price and edge columns |
| `check-teams` | finds club names that don't match between the schedule and the store |
| `backfill`, `refresh-events`, `refresh-positions` | build or top up the local store |

The report commands first pull in any newly played matches. Pass `--no-ingest` to skip that.

### HTML reports

Each report also writes a single self-contained HTML page. You can sort and filter the tables,
switch between column views, star players into a shortlist and export it to CSV, and type in a
price to see your edge against the model's fair odds. Clicking a player opens their record by
season and competition, plus their club's last 20 games.

### Optional local inputs

Two small JSON files in `data/` are picked up when present:

- `data/lineups.json` has today's line-ups:
  `{"date": "YYYY-MM-DD", "fixtures": [{"home", "away", "confirmed", "starters", "bench", "out"}]}`.
  Players listed as out are dropped from every report, and `report-late` uses the confirmed
  benches.
- `data/referee_appointments.json` has `[{"home", "away", "referee", "cards_per_game"}]`. It
  fills the referee-adjusted odds before the schedule names the referee.

## Project layout

```
src/overdue/
  source.py        where match data comes from (the sample world here)
  ingest.py        backfill and incremental updates into the store
  store.py         parquet stores for appearances and events, plus run metadata
  model.py         card rate, streak, overdue ratio and availability checks
  report.py        the ranked view that every output format uses
  targets.py       late-substitute targets, squads and game logs
  assessment.py    scores a past day's picks
  referees.py      how strict each referee is, from the schedule
  h2h.py           head-to-head card averages
  lineups.py       line-up status (starter / bench / out)
  appointments.py  referee appointments
  odds_book.py     reading pasted prices, the odds cache, matching names to players
  teamnames.py     matching club names across spellings without mixing up rivals
  render_text.py   Markdown reports and terminal output
  webview.py       the interactive HTML reports
  cli.py           the `overdue` command
tests/             unit tests on small hand-built data, no network
```

## Tests

```bash
pytest -q
```

The tests cover the model maths and filters, the store, building and rendering reports, name
matching (accents, single names, aliases, and clubs that share a city like Manchester United
and City), the assessment logic and the sample source itself. The sample source is checked for
consistency: eleven starters a side, minutes that add up, and events that line up with
appearances. They run on every push and pull request through the
[`tests`](.github/workflows/tests.yml) workflow.

## Using real data

`source.py` is the only module that knows where matches come from. A replacement needs to
provide:

- `read_schedule(seasons, leagues=None)`: one row per fixture with `league, season, game,
  game_id, date, start_time, home_team, away_team, referee, notes, played`
- `read_match_appearances(game_ids, seasons, leagues=None)`: one row per player per match, in
  the `store.COLUMNS` schema (without `date`)
- `read_match_events(game_ids, seasons, leagues=None)`: card and substitution events with
  minutes
- `read_player_positions(seasons=None, report=None)`: a dict of `player -> GK/DF/MF/FW`

If you plug in a real source, check its terms of use first.
