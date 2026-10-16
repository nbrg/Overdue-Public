"""Tests for report assembly, config season helpers, and Telegram message."""

from __future__ import annotations

from datetime import date

import pandas as pd

from overdue import config, notify
from overdue.config import ModelConfig, baseline_seasons, season_code
from overdue.report import build_report


def _app(player, team, league, season, d, minutes, yellow, gid):
    return dict(
        league=league, season=season, game_id=gid, game=f"{d} {team}",
        date=pd.Timestamp(d), team=team, player=player, minutes=minutes,
        yellow=yellow, red=0,
    )


def _overdue_player(player, team, league, gid_prefix, apps=12, cards=3):
    return [
        _app(player, team, league, "2627", f"2026-08-{i + 1:02d}", 90,
             1 if i < cards else 0, f"{gid_prefix}{i}")
        for i in range(apps)
    ]


CFG = ModelConfig(start_minutes=30, min_appearances=10, min_cards=2, overdue_threshold=1.5)


def test_season_code_format():
    assert season_code(2026) == "2627"
    assert season_code(2024) == "2425"
    assert season_code(1999) == "9900"


def test_baseline_seasons_order_and_count():
    seasons = baseline_seasons(today=date(2026, 9, 16), n=2)
    assert seasons == ["2526", "2627"]  # oldest first
    assert baseline_seasons(today=date(2026, 3, 1), n=3) == ["2324", "2425", "2526"]


def test_report_includes_overdue_playing_today_only():
    rows = _overdue_player("Rice", "Arsenal", "ENG-Premier League", "ar")
    rows += _overdue_player("Carvajal", "Real Madrid", "ESP-La Liga", "rm")
    df = pd.DataFrame(rows)
    fixtures = pd.DataFrame(
        [dict(league="ENG-Premier League", season="2627", game="g", game_id="gx",
              date=pd.Timestamp("2026-09-16"), home_team="Arsenal", away_team="Chelsea",
              played=False)]
    )
    report = build_report(df, fixtures, date(2026, 9, 16), CFG)
    names = [r.player for r in report.rows]
    assert names == ["Rice"]  # Carvajal overdue but not playing today
    assert report.rows[0].opponent == "Chelsea"
    assert report.total_scored == 2


def test_confirmed_bench_and_out_players_excluded():
    from overdue.lineups import _player_key

    # One overdue regular per club (so each has a clean start-share), all playing.
    rows = (_overdue_player("Starter Man", "Teama", "ENG-Premier League", "st")
            + _overdue_player("Bench Man", "Teamb", "ENG-Premier League", "bn")
            + _overdue_player("Out Man", "Teamc", "ENG-Premier League", "ou"))
    df = pd.DataFrame(rows)
    fixtures = pd.DataFrame(
        [dict(league="ENG-Premier League", season="2627", game="g", game_id=f"gx{i}",
              date=pd.Timestamp("2026-09-16"), home_team=t, away_team="Foe",
              notes="", played=False)
         for i, t in enumerate(("Teama", "Teamb", "Teamc"))]
    )
    status = {_player_key("Starter Man"): "starter",
              _player_key("Bench Man"): "bench",
              _player_key("Out Man"): "out"}
    report = build_report(df, fixtures, date(2026, 9, 16), CFG, lineup_status=status)
    # Only the confirmed starter survives; the benched and out-of-squad are dropped.
    assert [r.player for r in report.rows] == ["Starter Man"]
    assert report.rows[0].xi_status == "Start"
    # Without a confirmed XI, all three overdue regulars are shown.
    assert len(build_report(df, fixtures, date(2026, 9, 16), CFG).rows) == 3


def _fixture(team, opp="Foe", league="ENG-Premier League"):
    return pd.DataFrame(
        [dict(league=league, season="2627", game="g", game_id="gp",
              date=pd.Timestamp("2026-09-16"), home_team=team, away_team=opp,
              notes="", played=False)]
    )


def test_one_from_ban_is_flagged_not_excluded():
    # 4 league yellows (one below the PL threshold of 5), clean recently -> overdue,
    # kept, but flagged as a warning.
    rows = [
        _app("EdgeGuy", "TeamP", "ENG-Premier League", "2627",
             f"2026-08-{i + 5:02d}", 90, 1 if i < 4 else 0, f"e{i}")
        for i in range(14)
    ]
    report = build_report(pd.DataFrame(rows), _fixture("TeamP"), date(2026, 9, 16), CFG)
    row = [r for r in report.rows if r.player == "EdgeGuy"]
    assert row and row[0].availability == "warning"


def test_cup_yellows_do_not_count_toward_league_warning():
    # 3 PL yellows + 2 FA Cup yellows. Playing a PL game today -> only the 3 PL
    # yellows count, which is below the PL threshold-1 of 4 -> no warning.
    # Cup yellows early, then a long clean run in the league -> overdue.
    rows = [
        _app("Mixed", "TeamP", "ENG-FA Cup", "2627", f"2026-08-0{i + 1}", 90, 1, f"fac{i}")
        for i in range(2)
    ]
    rows += [
        _app("Mixed", "TeamP", "ENG-Premier League", "2627",
             f"2026-08-{i + 5:02d}", 90, 1 if i < 3 else 0, f"pl{i}")
        for i in range(14)
    ]
    report = build_report(pd.DataFrame(rows), _fixture("TeamP"), date(2026, 9, 16), CFG)
    row = [r for r in report.rows if r.player == "Mixed"]
    assert row and row[0].availability == ""  # cup cards excluded from PL count


def test_low_start_share_players_filtered():
    # Fringe: overdue, plays today, but featured in only 3 of the team's 10
    # league games this season (30% < 35%) -> excluded.
    rows = []
    for i in range(10):  # prior season: 3 early yellows, then clean
        rows.append(_app("Fringe", "TeamX", "ENG-Premier League", "2526",
                         f"2026-05-{i + 1:02d}", 90, 1 if i < 3 else 0, f"px{i}"))
    for i in range(3):   # current season: 3 clean apps (game ids g0..g2)
        rows.append(_app("Fringe", "TeamX", "ENG-Premier League", "2627",
                         f"2026-09-{i + 1:02d}", 90, 0, f"g{i}"))
    for i in range(10):  # team-mate defines 10 team games this season (g0..g9)
        rows.append(_app("Anchor", "TeamX", "ENG-Premier League", "2627",
                         f"2026-09-{i + 1:02d}", 90, 0, f"g{i}"))
    df = pd.DataFrame(rows)
    fixtures = pd.DataFrame(
        [dict(league="ENG-Premier League", season="2627", game="g", game_id="gx",
              date=pd.Timestamp("2026-09-16"), home_team="TeamX", away_team="Foe",
              notes="", played=False)]
    )
    report = build_report(df, fixtures, date(2026, 9, 16), CFG)
    assert "Fringe" not in [r.player for r in report.rows]


def test_goalkeepers_excluded_and_position_shown():
    rows = _overdue_player("Alisson", "TeamP", "ENG-Premier League", "gk")
    rows += _overdue_player("Van Dijk", "TeamP", "ENG-Premier League", "vd")
    df = pd.DataFrame(rows)
    positions = {"Alisson": "GK", "Van Dijk": "DF"}
    report = build_report(df, _fixture("TeamP"), date(2026, 9, 16), CFG, positions=positions)
    names = [r.player for r in report.rows]
    assert "Alisson" not in names          # goalkeeper excluded
    assert "Van Dijk" in names
    vd = [r for r in report.rows if r.player == "Van Dijk"][0]
    assert vd.position == "DF"             # position shown


def test_report_empty_when_no_fixtures():
    rows = _overdue_player("Rice", "Arsenal", "ENG-Premier League", "ar")
    df = pd.DataFrame(rows)
    empty_fx = pd.DataFrame(
        columns=["league", "season", "game", "game_id", "date", "home_team", "away_team", "played"]
    )
    report = build_report(df, empty_fx, date(2026, 9, 16), CFG)
    # No fixtures -> nobody is "playing today".
    assert report.rows == []


def test_telegram_message_variants():
    rows = _overdue_player("Rice", "Arsenal", "ENG-Premier League", "ar")
    df = pd.DataFrame(rows)
    fixtures = pd.DataFrame(
        [dict(league="ENG-Premier League", season="2627", game="g", game_id="gx",
              date=pd.Timestamp("2026-09-16"), home_team="Arsenal", away_team="Chelsea",
              played=False)]
    )
    report = build_report(df, fixtures, date(2026, 9, 16), CFG)
    msg = notify.build_message(report, site_url="https://x.example/")
    assert "Rice" in msg and "Arsenal v Chelsea" in msg and "https://x.example/" in msg

    empty = build_report(
        df,
        pd.DataFrame(columns=["league", "season", "game", "game_id", "date",
                              "home_team", "away_team", "played"]),
        date(2026, 9, 16),
        CFG,
    )
    assert "No overdue players" in notify.build_message(empty)


def test_player_fouls_drawn_per90():
    # Each overdue player carries their OWN fouls-won-per-90 (2 fouls / 90 min).
    rows = _overdue_player("Rice", "Arsenal", "ENG-Premier League", "ar")
    for r in rows:
        r["fouled"] = 2
    df = pd.DataFrame(rows)
    fx = pd.DataFrame([dict(league="ENG-Premier League", season="2627", game="g",
                            game_id="gx", date=pd.Timestamp("2026-09-16"),
                            home_team="Arsenal", away_team="Chelsea", played=False)])
    report = build_report(df, fx, date(2026, 9, 16), CFG)
    rice = next(r for r in report.rows if r.player == "Rice")
    assert round(rice.fouls_drawn_per90, 1) == 2.0


def test_send_is_dry_run_safe(monkeypatch):
    # No creds + dry_run -> returns False, never touches the network.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    rows = _overdue_player("Rice", "Arsenal", "ENG-Premier League", "ar")
    df = pd.DataFrame(rows)
    fx = pd.DataFrame(
        [dict(league="ENG-Premier League", season="2627", game="g", game_id="gx",
              date=pd.Timestamp("2026-09-16"), home_team="Arsenal", away_team="Chelsea",
              played=False)]
    )
    report = build_report(df, fx, date(2026, 9, 16), CFG)
    assert notify.send(report, dry_run=True) is False
