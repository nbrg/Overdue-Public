"""The synthetic match source: a consistent, deterministic sample world."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from overdue import source

SEASONS = ["2526", "2627"]
TODAY = date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(source, "_today", lambda: TODAY)


def _played_ids(sched: pd.DataFrame) -> list[str]:
    return list(sched.loc[sched["played"], "game_id"])


def test_schedule_is_deterministic_and_has_a_match_day_today():
    a = source.read_schedule(SEASONS)
    b = source.read_schedule(SEASONS)
    pd.testing.assert_frame_equal(a, b)
    assert a["game_id"].is_unique
    today = source.fixtures_from_schedule(a, TODAY)
    assert not today.empty and not today["played"].any()
    # Played games are exactly those before today; the future stops two weeks out.
    assert (a.loc[a["played"], "date"].dt.date < TODAY).all()
    assert a["date"].dt.date.max() <= date(2026, 10, 30)


def test_no_club_plays_twice_on_one_day():
    sched = source.read_schedule(SEASONS)
    sides = pd.concat([sched[["date", "home_team"]].rename(columns={"home_team": "team"}),
                       sched[["date", "away_team"]].rename(columns={"away_team": "team"})])
    assert not sides.duplicated().any()


def test_each_side_fields_eleven_starters_and_three_subs():
    sched = source.read_schedule(SEASONS)
    apps = source.read_match_appearances(_played_ids(sched)[:20], SEASONS)
    per_side = apps.groupby(["game_id", "team"])
    assert (per_side["started"].sum() == 11).all()
    assert (per_side.size() == 14).all()
    # A sub's minutes plus the starter he replaced make up the full 90.
    assert (per_side["minutes"].sum() == 11 * 90).all()


def test_events_line_up_with_appearances():
    sched = source.read_schedule(SEASONS)
    ids = _played_ids(sched)[:40]
    apps = source.read_match_appearances(ids, SEASONS)
    events = source.read_match_events(ids, SEASONS)
    cards = events[events["kind"].isin(["yellow", "red"])]
    assert len(cards) == int((apps["yellow"] | apps["red"]).sum())
    subs = events[events["kind"] == "sub"]
    assert len(subs) == 3 * apps.groupby(["game_id", "team"]).ngroups
    on = apps[apps["started"] == 0].set_index(["game_id", "player"])["minutes"]
    for _, ev in subs.iterrows():
        assert on[(ev["game_id"], ev["player"])] == 90 - ev["minute"]


def test_league_filter_and_empty_requests():
    sched = source.read_schedule(SEASONS, leagues=["ESP-La Liga"])
    assert set(sched["league"]) == {"ESP-La Liga"}
    assert source.read_match_appearances([], SEASONS).empty
    assert source.read_match_events([], SEASONS).empty


def test_positions_cover_every_player():
    report: dict = {}
    pos = source.read_player_positions(SEASONS, report=report)
    sched = source.read_schedule(SEASONS)
    apps = source.read_match_appearances(_played_ids(sched), SEASONS)
    assert set(apps["player"]) <= set(pos)
    assert set(pos.values()) == {"GK", "DF", "MF", "FW"}
    assert set(report) == {"ENG-Premier League", "ESP-La Liga"}


def test_backfill_then_report_flags_overdue_players(monkeypatch, tmp_path):
    from overdue import config, ingest, store
    from overdue.report import build_report

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APPEARANCES_PATH", tmp_path / "appearances.parquet")
    monkeypatch.setattr(config, "CARD_EVENTS_PATH", tmp_path / "card_events.parquet")
    monkeypatch.setattr(config, "META_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(config, "baseline_seasons", lambda today=None, n=None: SEASONS)

    ingest.backfill()
    apps = store.load_appearances()
    sched = source.read_schedule(SEASONS)
    assert apps["game_id"].nunique() == int(sched["played"].sum())

    fixtures = source.fixtures_from_schedule(sched, TODAY)
    report = build_report(apps, fixtures, TODAY, schedule=sched,
                          positions=source.read_player_positions(SEASONS))
    assert report.rows, "the sample world should always have someone overdue today"
    playing = set(fixtures["home_team"]) | set(fixtures["away_team"])
    assert all(r.team in playing for r in report.rows)
