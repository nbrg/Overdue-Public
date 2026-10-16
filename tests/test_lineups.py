"""Line-up status store: starter / bench / out keys and date scoping."""

from __future__ import annotations


def test_player_key_matches_across_spellings():
    from overdue.lineups import _player_key
    # A short "J. Mateta" and the full "Jean-Philippe Mateta" must collide.
    assert _player_key("J. Mateta") == _player_key("Jean-Philippe Mateta") == "j|mateta"
    # Accents are stripped.
    assert _player_key("A. Öztunalı") == _player_key("Aaron Oztunali") == "a|oztunali"
    assert _player_key("") == ""


def test_status_index_confirmed_vs_predicted():
    from overdue.lineups import status_index, status_for
    base = {"home": "Crystal Palace", "away": "Lech Poznań", "url": "u",
            "starters": ["A. Wharton", "J.P. Mateta"],
            "bench": ["E. Nketiah"], "out": ["D. Henderson"]}

    # Confirmed XI → starters/bench/out all used.
    idx = status_index({"date": "2026-09-17", "fixtures": [dict(base, confirmed=True)]})
    assert status_for(idx, "Adam Wharton") == "starter"
    assert status_for(idx, "Jean-Philippe Mateta") == "starter"
    assert status_for(idx, "Eddie Nketiah") == "bench"
    assert status_for(idx, "Dean Henderson") == "out"

    # Predicted (unconfirmed) → ONLY the injured/suspended "out" list is used;
    # the guessed starters/bench are ignored.
    idx2 = status_index({"date": "2026-09-17", "fixtures": [dict(base, confirmed=False)]})
    assert status_for(idx2, "Adam Wharton") == "unknown"
    assert status_for(idx2, "Eddie Nketiah") == "unknown"
    assert status_for(idx2, "Dean Henderson") == "out"
    assert status_for(idx, "Some Unlisted Player") == "unknown"


def test_load_lineups_for_is_date_scoped(tmp_path):
    from overdue.lineups import save_lineups, load_lineups_for
    from datetime import date
    path = tmp_path / "lu.json"
    save_lineups({"date": "2026-09-17", "fixtures": [{"home": "X"}]}, path=path)
    # Same day → loaded; other day → ignored (stale XIs must not filter).
    assert load_lineups_for(date(2026, 9, 17), path=path)["fixtures"] == [{"home": "X"}]
    assert load_lineups_for(date(2026, 9, 18), path=path) == {}


def test_sub_card_targets_drops_starters_and_out():
    import pandas as pd
    from overdue.lineups import status_index
    from overdue.targets import sub_card_targets

    # Two identical cameo-booking profiles; only the lineup status differs.
    def rows_for(player, team):
        recs = []
        for i in range(12):
            recs.append({
                "player": player, "team": team, "league": "ENG-Premier League",
                "season": "2526", "game_id": f"{team}{i}", "date": f"2026-01-{i+1:02d}",
                "minutes": 20, "yellow": 1 if i < 4 else 0, "red": 0, "started": 0,
                "fouls": 0, "goals": 0, "game": f"g{i}",
            })
        # A couple of recent starts so recency gates pass.
        for i in range(3):
            recs.append({
                "player": player, "team": team, "league": "ENG-Premier League",
                "season": "2526", "game_id": f"{team}s{i}", "date": f"2026-02-{i+1:02d}",
                "minutes": 90, "yellow": 0, "red": 0, "started": 1,
                "fouls": 0, "goals": 0, "game": f"gs{i}",
            })
        return recs

    df = pd.DataFrame(
        rows_for("Starter Guy", "Team A")
        + rows_for("Bench Guy", "Team B")
        + rows_for("Injured Guy", "Team C")
    )
    lineups = {"date": "2026-09-17", "fixtures": [{
        "home": "Team A", "away": "Team B", "url": "u", "confirmed": True,
        "starters": ["Starter Guy"], "bench": ["Bench Guy"], "out": ["Injured Guy"],
    }]}
    idx = status_index(lineups)

    kept = {r.player for r in sub_card_targets(df, min_sub_apps=4, min_sub_cards=2,
                                               lineup_status=idx)}
    assert "Bench Guy" in kept
    assert "Starter Guy" not in kept  # confirmed to start → can't be a sub
    assert "Injured Guy" not in kept  # out of squad → excluded

    # Without the lineup filter, all three qualify.
    all_kept = {r.player for r in sub_card_targets(df, min_sub_apps=4, min_sub_cards=2)}
    assert {"Starter Guy", "Bench Guy", "Injured Guy"} <= all_kept

