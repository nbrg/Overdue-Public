"""Match data source: schedules, per-match card logs and events.

Everything upstream of the appearance store comes through this module, so the
rest of the codebase only ever sees tidy pandas frames with our own column names
(see ``store.COLUMNS``). The public build ships a **synthetic** source: a small,
deterministic set of made-up leagues, clubs and players whose matches run up to
today and a fortnight beyond. It exercises every report end to end without any
network access. To run against real data, replace these functions with an
adapter for your own feed that returns the same frames.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd

from . import config

SEED = 7

# Which tracked competitions the sample world covers: two domestic leagues
# (scored) plus the Champions League (fixtures and bookings only).
_SAMPLE_DOMESTIC = {
    "ENG-Premier League": [
        "Northbridge Athletic", "Harrowgate United", "Kingsmere Rovers", "Eastholm City",
        "Ashford Vale", "Redcliffe Town", "Marlow Forest", "Sutton Wharf",
    ],
    "ESP-La Liga": [
        "Real Castellar", "Atlético Mirasol", "CD Valdeoro", "Unión Puerto Alto",
        "Deportivo Arenal", "SD Sierra Blanca", "CF Montealegre", "Racing Costa Brava",
    ],
}
_EUROPE = "INT-Champions League"
_EUROPE_CLUBS = 3  # top clubs per league that also play in Europe

_FIRST = ["Adam", "Bruno", "Carlos", "Daniel", "Elias", "Felix", "Gabriel", "Hugo", "Ivan",
          "Jonas", "Kai", "Luca", "Marco", "Nico", "Oscar", "Pablo", "Rafael", "Samuel",
          "Theo", "Victor", "Yann", "Zeki", "Mateo", "Leo", "Iker", "Joel", "Aaron", "Omar"]
_LAST = ["Abara", "Bellamy", "Castro", "Dalton", "Esteve", "Fenwick", "Garrido", "Holm",
         "Ibarra", "Jarvis", "Kemper", "Lozano", "Moreau", "Navarro", "Okafor", "Pereira",
         "Quint", "Rossi", "Salgado", "Thorne", "Ulloa", "Varga", "Whitlock", "Ximenez",
         "Yilmaz", "Zamora", "Aldana", "Brennan", "Cortés", "Doyle", "Echevarría", "Fry",
         "Gallagher", "Hartmann", "Iturbe", "Jensen", "Kovač", "Laine", "Mbeki", "Nyström",
         "Ortiz", "Petrović", "Quaresma", "Rahman", "Sørensen", "Tavares", "Uribe", "Vidal",
         "Wójcik", "Xavier", "Yoon", "Zielinski", "Asante", "Bianchi", "Carvalho", "Diallo"]
_REFEREES = ["M. Calloway", "J. Ortega", "S. Brightwell", "P. Lindqvist", "R. Delgado",
             "T. Haversham"]
_SQUAD = (["GK"] * 2 + ["DF"] * 6 + ["MF"] * 6 + ["FW"] * 4)


@dataclass
class _Player:
    name: str
    team: str
    pos: str
    card_rate: float  # chance of a booking in a full 90
    foul_rate: float  # fouls per 90
    quality: float  # how often they're picked to start


def _gid(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8]


def _season_start(code: str) -> date:
    return date(2000 + int(code[:2]), 8, 9)


def _squads(rng: random.Random) -> dict[str, list[_Player]]:
    teams = [t for clubs in _SAMPLE_DOMESTIC.values() for t in clubs]
    # Every player gets a distinct surname, so names read clearly in the reports.
    surnames = rng.sample(_LAST, k=min(len(_LAST), len(teams) * len(_SQUAD)))
    squads: dict[str, list[_Player]] = {}
    n = 0
    for team in teams:
            squad = []
            for pos in _SQUAD:
                name = f"{rng.choice(_FIRST)} {surnames[n % len(surnames)]}"
                n += 1
                base = {"GK": 0.04, "DF": 0.2, "MF": 0.22, "FW": 0.12}[pos]
                squad.append(_Player(
                    name=name, team=team, pos=pos,
                    card_rate=max(0.02, rng.gauss(base, 0.07)),
                    foul_rate=max(0.2, rng.gauss(1.2 if pos != "GK" else 0.1, 0.5)),
                    quality=rng.random()))
            squads[team] = squad
    return squads


def _round_robin(clubs: list[str]) -> list[list[tuple[str, str]]]:
    """Double round-robin rounds (circle method)."""
    teams = list(clubs)
    n = len(teams)
    rounds = []
    for r in range(n - 1):
        pairs = [(teams[i], teams[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) if r % 2 else (b, a) for a, b in pairs])
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return rounds + [[(b, a) for a, b in rnd] for rnd in rounds]


@lru_cache(maxsize=4)
def _world(seasons: tuple[str, ...], today: date) -> tuple[pd.DataFrame, pd.DataFrame,
                                                             pd.DataFrame, dict]:
    """Build (schedule, appearances, events, positions) for ``seasons``."""
    rng = random.Random(SEED)
    squads = _squads(rng)
    sched, apps, events = [], [], []
    horizon = today + timedelta(days=14)

    for season in seasons:
        start = _season_start(season)
        if start <= today < start + timedelta(days=365):
            # Line the current season's rounds up with today, so there is always
            # a match day to report on.
            start = today - timedelta(weeks=(today - start).days // 7)
        fixtures: list[tuple[str, date, str, str]] = []
        for league, clubs in _SAMPLE_DOMESTIC.items():
            rounds = _round_robin(clubs)
            # Four passes of the double round-robin, two rounds a week.
            for i, rnd in enumerate(rounds * 4):
                day = start + timedelta(days=(i * 7) // 2)
                fixtures += [(league, day, h, a) for h, a in rnd]
        europe = [c for clubs in _SAMPLE_DOMESTIC.values() for c in clubs[:_EUROPE_CLUBS]]
        for i in range(8):  # midweek European nights
            day = start + timedelta(weeks=5 + 3 * i, days=5)  # between domestic rounds
            pool = europe[:]
            rng.shuffle(pool)
            fixtures += [(_EUROPE, day, pool[j], pool[j + 1]) for j in range(0, len(pool) - 1, 2)]

        for league, day, home, away in sorted(fixtures, key=lambda f: f[1]):
            if day > horizon:
                continue
            gid = _gid(season, league, day, home, away)
            game = f"{day.isoformat()} {home}-{away}"
            played = day < today
            sched.append({
                "league": league, "season": season, "game": game, "game_id": gid,
                "date": pd.Timestamp(day),
                "start_time": pd.Timestamp(datetime.combine(day, datetime.min.time())
                                           + timedelta(hours=20 if league == _EUROPE else 15)),
                "home_team": home, "away_team": away,
                "referee": rng.choice(_REFEREES) if played else "",
                "notes": "", "played": played,
            })
            if not played:
                continue
            for team in (home, away):
                _play(rng, squads[team], season, league, gid, game, day, apps, events)

    positions = {p.name: p.pos for squad in squads.values() for p in squad}
    return (pd.DataFrame(sched), pd.DataFrame(apps), pd.DataFrame(events), positions)


def _play(rng, squad, season, league, gid, game, day, apps, events) -> None:
    """Simulate one side's appearances, cards and substitutions in a match."""
    keepers = sorted((p for p in squad if p.pos == "GK"), key=lambda p: -p.quality)
    outfield = sorted((p for p in squad if p.pos != "GK"),
                      key=lambda p: -(p.quality + rng.random() * 0.6))
    starters = [keepers[0]] + outfield[:10]
    bench = outfield[10:13]
    subs = rng.sample(range(1, 11), k=3)
    minutes = {p.name: 90 for p in starters}
    for off_idx, on in zip(subs, bench):
        off = starters[off_idx]
        at = rng.randint(55, 85)
        minutes[off.name] = at
        minutes[on.name] = 90 - at
        events.append({"game_id": gid, "league": league, "season": season, "game": game,
                       "team": off.team, "player": on.name, "player_off": off.name,
                       "minute": at, "minute_plus": 0, "kind": "sub", "date": pd.Timestamp(day)})
    for p in starters + bench:
        mins = minutes[p.name]
        share = mins / 90
        booked = rng.random() < p.card_rate * share
        red = rng.random() < 0.01 * share
        apps.append({
            "league": league, "season": season, "game_id": gid, "game": game,
            "date": pd.Timestamp(day), "team": p.team, "player": p.name,
            "minutes": mins, "yellow": int(booked), "red": int(red),
            "goals": int(p.pos == "FW" and rng.random() < 0.3 * share),
            "assists": int(rng.random() < 0.1 * share), "shots": rng.randint(0, 3),
            "shots_on_target": rng.randint(0, 1), "pens_scored": 0, "pens_att": 0,
            "fouls": sum(rng.random() < p.foul_rate * share / 3 for _ in range(3)),
            "fouled": rng.randint(0, 2), "offsides": 0, "crosses": rng.randint(0, 2),
            "tackles_won": rng.randint(0, 3), "interceptions": rng.randint(0, 2),
            "own_goals": 0, "pens_won": 0, "pens_conceded": 0,
            "started": int(p in starters),
        })
        if booked or red:
            on_at = 90 - mins if p in bench else 0
            events.append({"game_id": gid, "league": league, "season": season, "game": game,
                           "team": p.team, "player": p.name, "player_off": None,
                           "minute": on_at + rng.randint(1, max(mins, 1)), "minute_plus": 0,
                           "kind": "red" if red else "yellow", "date": pd.Timestamp(day)})


def _today() -> date:
    from .fixtures import uk_today

    return uk_today()


def _scope(df: pd.DataFrame, leagues: list[str] | None) -> pd.DataFrame:
    return df if not leagues or df.empty else df[df["league"].isin(leagues)]


# --- The source interface used by ingest / cli ------------------------------


def read_schedule(seasons: list[str], leagues: list[str] | None = None) -> pd.DataFrame:
    """Fixture schedule for ``seasons``: league, season, game, game_id, date,
    start_time, home_team, away_team, referee, notes, played."""
    sched = _world(tuple(seasons), _today())[0]
    return _scope(sched, leagues).reset_index(drop=True)


def read_match_appearances(game_ids: list[str], seasons: list[str],
                           leagues: list[str] | None = None) -> pd.DataFrame:
    """Per-player rows for ``game_ids`` in the store schema, minus ``date``."""
    from .store import empty_frame

    apps = _world(tuple(seasons), _today())[1]
    if not game_ids or apps.empty:
        return empty_frame().drop(columns=["date"])
    out = apps[apps["game_id"].isin(set(map(str, game_ids)))]
    return _scope(out, leagues).drop(columns=["date"]).reset_index(drop=True)


def read_match_events(game_ids: list[str], seasons: list[str],
                      leagues: list[str] | None = None) -> pd.DataFrame:
    """Card and substitution events (with minutes) for ``game_ids``."""
    cols = ["game_id", "league", "season", "game", "team", "player", "player_off",
            "minute", "minute_plus", "kind"]
    ev = _world(tuple(seasons), _today())[2]
    if not game_ids or ev.empty:
        return pd.DataFrame(columns=cols)
    out = ev[ev["game_id"].isin(set(map(str, game_ids)))]
    return _scope(out, leagues)[cols].reset_index(drop=True)


def read_player_positions(seasons: list[str] | None = None,
                          report: dict | None = None) -> dict[str, str]:
    """``player -> position`` (GK/DF/MF/FW); ``report`` gets a per-league count."""
    seasons = seasons or config.baseline_seasons()
    positions = _world(tuple(seasons), _today())[3]
    if report is not None:
        for league in _SAMPLE_DOMESTIC:
            report[league] = len(positions) // len(_SAMPLE_DOMESTIC)
    return dict(positions)


# Schedule notes that mean a game won't be played as listed.
_NOT_HAPPENING = "postpone|cancel|abandon|suspend|awarded|walkover"


def fixtures_from_schedule(schedule: pd.DataFrame, day: date) -> pd.DataFrame:
    """Filter an already-fetched schedule to ``day``, dropping postponed games."""
    on_day = schedule["date"].dt.date == day
    if "notes" in schedule.columns:
        called_off = schedule["notes"].fillna("").str.contains(
            _NOT_HAPPENING, case=False, regex=True
        )
    else:
        called_off = pd.Series(False, index=schedule.index)
    return schedule.loc[on_day & ~called_off].reset_index(drop=True)


def fixtures_on(day: date, seasons: list[str], leagues: list[str] | None = None) -> pd.DataFrame:
    """The fixtures scheduled on ``day``, excluding postponed/cancelled ones."""
    return fixtures_from_schedule(read_schedule(seasons, leagues), day)
