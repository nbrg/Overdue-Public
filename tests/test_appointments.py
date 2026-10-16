"""Fixture-matching for referee appointments (no network)."""

from __future__ import annotations
from overdue import appointments as appt


def test_referee_for_matches_by_team_name():
    appointments = [
        {"home": "Arsenal FC", "away": "Chelsea FC", "referee": "Michael Oliver",
         "cards_per_game": 4.2},
        {"home": "Liverpool", "away": "Everton", "referee": "Anthony Taylor",
         "cards_per_game": 3.1},
    ]
    ref, cards = appt.referee_for(appointments, "Arsenal", "Chelsea")
    assert ref == "Michael Oliver" and cards == 4.2
    # No meeting for this pairing.
    assert appt.referee_for(appointments, "Arsenal", "Spurs") == ("", None)


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "appts.json"
    data = [{"home": "A", "away": "B", "referee": "Ref X", "cards_per_game": 3.5}]
    appt.save_appointments(data, p)
    assert appt.load_appointments(p) == data


def test_coverage_splits_found_and_missing_games():
    import pandas as pd

    from overdue import appointments as ra

    fx = pd.DataFrame({"home_team": ["Arsenal", "Bristol City", "Burnley"],
                       "away_team": ["Lille", "Blackburn", "Charlton Athletic"],
                       "referee": ["", "Tim Robinson", pd.NA]})
    appts = [{"home": "Arsenal", "away": "Lille", "referee": "Felix Zwayer"}]
    found, missing = ra.coverage(fx, appts)
    assert found == [("Arsenal", "Lille", "Felix Zwayer"),
                     ("Bristol City", "Blackburn", "Tim Robinson")]
    assert missing == [("Burnley", "Charlton Athletic")]
    assert ra.coverage(pd.DataFrame(), appts) == ([], [])
