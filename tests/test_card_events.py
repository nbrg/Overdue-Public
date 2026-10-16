"""Card/substitution event storage and late-card stats (no network)."""

from __future__ import annotations

import pandas as pd

from overdue import store
from overdue.latecards import late_card_stats


def test_events_store_roundtrip(tmp_path):
    df = pd.DataFrame([
        {"league": "ENG-Premier League", "season": "2627", "game": "g", "game_id": "gid1",
         "date": pd.Timestamp("2026-09-13"), "team": "Chelsea", "player": "Enzo",
         "player_off": None, "minute": 82, "minute_plus": 0, "kind": "yellow"},
    ])
    p = tmp_path / "events.parquet"
    store.save_events(df, p)
    back = store.load_events(p)
    assert len(back) == 1 and back.iloc[0]["minute"] == 82 and back.iloc[0]["kind"] == "yellow"


def test_late_card_stats():
    ev = pd.DataFrame([
        {"player": "Late Guy", "kind": "yellow", "minute": 80},
        {"player": "Late Guy", "kind": "yellow", "minute": 88},
        {"player": "Late Guy", "kind": "yellow", "minute": 30},
        {"player": "Early Guy", "kind": "yellow", "minute": 15},
        {"player": "Early Guy", "kind": "red", "minute": 40},
    ])
    stats = late_card_stats(ev, threshold=70)
    assert stats["Late Guy"].cards == 3
    assert stats["Late Guy"].late == 2                 # 80', 88'
    assert stats["Late Guy"].late_share == 2 / 3
    assert stats["Early Guy"].late == 0


def test_incremental_tops_up_recent_events_even_with_no_new_matches(tmp_path, monkeypatch):
    from datetime import date
    from overdue import ingest
    sched = pd.DataFrame([
        {"game_id": "old", "game": "g0", "date": pd.Timestamp("2026-08-01"), "played": True},
        {"game_id": "have", "game": "g1", "date": pd.Timestamp("2026-10-14"), "played": True},
        {"game_id": "miss", "game": "g2", "date": pd.Timestamp("2026-10-15"), "played": True},
        {"game_id": "future", "game": "g3", "date": pd.Timestamp("2026-10-18"), "played": False},
    ])
    monkeypatch.setattr(ingest.store, "load_events", lambda: pd.DataFrame(
        [{"game_id": "have", "player": "x", "kind": "sub"}]))
    called = {}
    monkeypatch.setattr(ingest, "ingest_events",
                        lambda **kw: called.setdefault("games", kw["only_games"]))
    ingest._top_up_recent_events(sched, ["2627"], date(2026, 10, 16))
    assert called["games"] == ["miss"]          # recent, played, and missing only

