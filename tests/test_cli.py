"""CLI helpers that don't need a network/browser — synthetic data only."""

from __future__ import annotations

from datetime import date

import pandas as pd

from overdue.cli import _resolve_team_key, _team_next_fixture_row


def _sched(rows):
    return pd.DataFrame(rows)


def test_next_fixture_row_picks_earliest_with_days_until():
    sched = _sched([
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Leeds",
         "date": pd.Timestamp("2026-09-15"), "played": True, "notes": ""},   # past
        {"league": "ENG-Premier League", "home_team": "Brighton", "away_team": "Arsenal",
         "date": pd.Timestamp("2026-09-19"), "played": False, "notes": ""},  # next (+1)
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Spurs",
         "date": pd.Timestamp("2026-09-27"), "played": False, "notes": ""},
    ])
    home, away, league, days = _team_next_fixture_row(sched, "Arsenal", date(2026, 9, 18))
    assert (home, away, league) == ("Brighton", "Arsenal", "ENG-Premier League")
    assert days == 1  # tomorrow


def test_next_fixture_row_skips_finished_today_game(monkeypatch):
    from datetime import datetime

    from overdue import cli, fixtures as fixtures_mod

    monkeypatch.setattr(fixtures_mod, "uk_now", lambda: datetime(2026, 9, 19, 20, 0))  # 8pm
    sched = _sched([
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Spurs",
         "date": pd.Timestamp("2026-09-19"), "start_time": pd.Timestamp("2026-09-19 15:00"),
         "played": False, "notes": ""},   # today but finished (kicked off 5h ago)
        {"league": "ENG-Premier League", "home_team": "Chelsea", "away_team": "Arsenal",
         "date": pd.Timestamp("2026-09-27"), "start_time": pd.Timestamp("2026-09-27 15:00"),
         "played": False, "notes": ""},   # the genuine next fixture
    ])
    home, away, _, days = cli._team_next_fixture_row(sched, "Arsenal", date(2026, 9, 19))
    assert (home, away) == ("Chelsea", "Arsenal") and days == 8  # rolled past the finished one


def test_next_fixture_row_skips_called_off_games():
    sched = _sched([
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Spurs",
         "date": pd.Timestamp("2026-09-19"), "played": False, "notes": "Postponed"},
        {"league": "ENG-Premier League", "home_team": "Chelsea", "away_team": "Arsenal",
         "date": pd.Timestamp("2026-09-21"), "played": False, "notes": ""},
    ])
    home, away, league, days = _team_next_fixture_row(sched, "Arsenal", date(2026, 9, 18))
    assert (home, away) == ("Chelsea", "Arsenal") and days == 3


def test_next_fixture_row_none_when_no_upcoming():
    sched = _sched([
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Leeds",
         "date": pd.Timestamp("2026-09-10"), "played": True, "notes": ""},
    ])
    assert _team_next_fixture_row(sched, "Arsenal", date(2026, 9, 18)) is None


def test_schedule_best_effort_caches_for_the_day(monkeypatch, tmp_path):
    from overdue import cli, config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.fixtures_mod, "uk_today", lambda: date(2026, 9, 18))
    sched = _sched([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                     "game_id": "x", "date": pd.Timestamp("2026-09-18"),
                     "start_time": pd.Timestamp("2026-09-18 15:00"),
                     "home_team": "Arsenal", "away_team": "Chelsea",
                     "referee": "", "notes": "", "played": False}])
    calls = {"n": 0}

    def fake_read(seasons, leagues=None):
        calls["n"] += 1
        return sched

    monkeypatch.setattr(cli.source, "read_schedule", fake_read)
    a = cli._schedule_best_effort(["2627"])
    b = cli._schedule_best_effort(["2627"])   # served from the day cache, no refetch
    assert calls["n"] == 1
    assert list(a["home_team"]) == ["Arsenal"] and list(b["home_team"]) == ["Arsenal"]
    assert config.schedule_cache_path("2026-09-18").exists()


def test_resolve_team_key_matches_differently_spelled_opponent():
    teams = ["Arsenal", "Brighton & Hove Albion", "Wolverhampton Wanderers"]
    assert _resolve_team_key("Arsenal", teams) == "Arsenal"          # exact
    assert _resolve_team_key("Brighton", teams) == "Brighton & Hove Albion"  # substring
    # The schedule's short forms now resolve to the store's long form.
    assert _resolve_team_key("Wolves", teams) == "Wolverhampton Wanderers"
    assert _resolve_team_key("Barcelona", teams) == ""              # no plausible match
    assert _resolve_team_key("", teams) == ""


def test_drop_finished_fixtures(monkeypatch):
    from datetime import datetime

    from overdue import cli, fixtures as fixtures_mod

    monkeypatch.setattr(fixtures_mod, "uk_now", lambda: datetime(2026, 9, 19, 20, 0))  # 8pm
    fx = _sched([
        {"home_team": "Burnley", "away_team": "Derby",
         "start_time": pd.Timestamp("2026-09-19 15:00")},   # >2h ago -> dropped
        {"home_team": "Arsenal", "away_team": "Spurs",
         "start_time": pd.Timestamp("2026-09-19 19:30")},   # in play -> kept
        {"home_team": "Chelsea", "away_team": "Leeds",
         "start_time": pd.NaT},                              # unknown time -> kept
    ])
    kept, n = cli._drop_finished_fixtures(fx)
    assert n == 1
    assert {(r["home_team"], r["away_team"]) for _, r in kept.iterrows()} == {
        ("Arsenal", "Spurs"), ("Chelsea", "Leeds")}


def test_todays_late_fixtures_today_scope_and_kickoff(monkeypatch):
    from datetime import datetime

    from overdue import cli, fixtures as fixtures_mod

    today = date(2026, 9, 19)
    # "Now" is 17:00 UK — a 14:00 kick-off is >2h ago (finished); 16:30 and 19:45
    # are still live/upcoming.
    monkeypatch.setattr(fixtures_mod, "uk_now", lambda: datetime(2026, 9, 19, 17, 0))
    sched = _sched([
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Spurs",
         "date": pd.Timestamp("2026-09-19"), "start_time": pd.Timestamp("2026-09-19 14:00"),
         "played": False, "notes": ""},  # kicked off >2h ago -> dropped
        {"league": "ENG-Premier League", "home_team": "Chelsea", "away_team": "Leeds",
         "date": pd.Timestamp("2026-09-19"), "start_time": pd.Timestamp("2026-09-19 16:30"),
         "played": False, "notes": ""},  # in play (<2h) -> kept
        {"league": "ENG-Premier League", "home_team": "Everton", "away_team": "Wolves",
         "date": pd.Timestamp("2026-09-19"), "start_time": pd.Timestamp("2026-09-19 19:45"),
         "played": False, "notes": ""},  # later today -> kept
        {"league": "ESP-La Liga", "home_team": "Real Madrid", "away_team": "Sevilla",
         "date": pd.Timestamp("2026-09-19"), "start_time": pd.Timestamp("2026-09-19 20:00"),
         "played": False, "notes": ""},  # today but wrong league -> dropped by scope
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Leeds",
         "date": pd.Timestamp("2026-09-20"), "start_time": pd.Timestamp("2026-09-20 15:00"),
         "played": False, "notes": ""},  # tomorrow -> dropped
    ])
    fx, n_started, _ = cli._todays_late_fixtures(sched, today, {"ENG-Premier League"})
    kept = {(r["home_team"], r["away_team"]) for _, r in fx.iterrows()}
    assert kept == {("Chelsea", "Leeds"), ("Everton", "Wolves")}
    assert n_started == 1  # the 14:00 game
    # No league scope -> the La Liga game is included (still today, still upcoming).
    fx_all, _, _ = cli._todays_late_fixtures(sched, today, None)
    assert ("Real Madrid", "Sevilla") in {(r["home_team"], r["away_team"]) for _, r in fx_all.iterrows()}


def test_confirmed_bench_rows_excludes_surname_only_xi():
    from overdue.cli import _confirmed_bench_rows

    class _R:
        def __init__(self, p):
            self.player = p

    # A realistic squad: 11 starters + 4 bench players.
    starters_full = ["Declan Rice", "Martin Ødegaard", "Ben White", "William Saliba",
                     "Gabriel Magalhães", "Jurriën Timber", "Bukayo Saka",
                     "Kai Havertz", "Leandro Trossard", "Thomas Partey", "David Raya"]
    bench_full = ["Gabriel Martinelli", "Mikel Merino", "Ethan Nwaneri", "Fábio Vieira"]
    team_rows = [_R(p) for p in starters_full + bench_full]

    # Live match: the line-up lists the XI by SURNAME only, no bench/out. The
    # initial+surname key ("d|rice") wouldn't match "Declan Rice"; surname
    # matching must still exclude all 11 starters, leaving the 4 bench players.
    entry = {"confirmed": True, "bench": [], "out": [],
             "starters": [s.split()[-1] for s in starters_full]}
    bench, exact, have_xi = _confirmed_bench_rows(entry, team_rows, {})
    assert have_xi is True and exact is False
    assert {r.player for r in bench} == set(bench_full)

    # This fixture's XI belongs to the OTHER team — almost none of these starters
    # are this squad's players, so have_xi is False and no bench is derived (the
    # whole squad must NOT leak in).
    other = {"confirmed": True, "bench": [], "out": [],
             "starters": ["Palmer", "Caicedo", "Enzo", "Cucurella", "James"]}
    bench2, exact2, have_xi2 = _confirmed_bench_rows(other, team_rows, {})
    assert have_xi2 is False and bench2 == []

    # Pre-match capture with a real bench list -> exact bench, matched by surname.
    entry3 = {"confirmed": True, "starters": [], "out": [],
              "bench": ["Martinelli", "Merino"]}
    bench3, exact3, have_xi3 = _confirmed_bench_rows(entry3, team_rows, {})
    assert exact3 is True and have_xi3 is True
    assert {r.player for r in bench3} == {"Gabriel Martinelli", "Mikel Merino"}


def test_match_named_players_by_surname_and_accents():
    from overdue.cli import _match_named_players

    class _R:
        def __init__(self, p): self.player = p

    rows = [_R("Lucas Bergvall"), _R("Mathys Tel"), _R("Radu Drăgușin"),
            _R("Dejan Kulusevski")]
    matched, missing = _match_named_players(
        ["Bergvall", "Tel", "Dragusin", "Nobody"], rows)
    assert [r.player for r in matched] == ["Lucas Bergvall", "Mathys Tel", "Radu Drăgușin"]
    assert missing == ["Nobody"]


