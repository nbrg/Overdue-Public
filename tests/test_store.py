"""Store round-trip and merge/meta behaviour."""

from __future__ import annotations

import pandas as pd

from overdue import store


def _rows():
    return pd.DataFrame(
        [
            dict(league="ENG-Premier League", season="2627", game_id="g1", game="g1",
                 date="2026-08-01", team="Arsenal", player="Rice", minutes=90, yellow=1, red=0),
            dict(league="ENG-Premier League", season="2627", game_id="g2", game="g2",
                 date="2026-08-08", team="Arsenal", player="Rice", minutes=90, yellow=0, red=0),
        ]
    )


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "appearances.parquet"
    store.save_appearances(_rows(), path)
    loaded = store.load_appearances(path)
    assert len(loaded) == 2
    assert set(loaded.columns) == set(store.COLUMNS)
    assert loaded["yellow"].dtype.kind == "i"
    assert pd.api.types.is_datetime64_any_dtype(loaded["date"])


def test_merge_dedups_on_player_game(tmp_path):
    existing = _rows()
    # Same player+game with a corrected minutes value should replace, not duplicate.
    update = pd.DataFrame(
        [dict(league="ENG-Premier League", season="2627", game_id="g1", game="g1",
              date="2026-08-01", team="Arsenal", player="Rice", minutes=75, yellow=1, red=0)]
    )
    merged = store.merge_appearances(existing, update)
    assert len(merged) == 2
    g1 = merged[merged["game_id"] == "g1"].iloc[0]
    assert g1["minutes"] == 75


def test_meta_roundtrip(tmp_path):
    path = tmp_path / "meta.json"
    meta = store.Meta(ingested_game_ids={"g1", "g2"})
    meta.save(path)
    loaded = store.Meta.load(path)
    assert loaded.ingested_game_ids == {"g1", "g2"}
    assert loaded.last_run_utc is not None


def test_load_missing_returns_empty(tmp_path):
    df = store.load_appearances(tmp_path / "nope.parquet")
    assert df.empty
    assert list(df.columns) == list(store.COLUMNS)
