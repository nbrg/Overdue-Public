"""Per-competition suspension warning rules."""

from __future__ import annotations

from overdue.suspensions import warning_for


def test_premier_league_five_before_matchweek_19():
    status, note = warning_for("ENG-Premier League", 4, team_comp_games=14)
    assert status == "warning" and "EPL" in note


def test_premier_league_deadline_passed():
    # On 4 yellows but the team is past its 19th game -> the 5-card tier lapsed.
    status, _ = warning_for("ENG-Premier League", 4, team_comp_games=25)
    assert status == ""


def test_premier_league_second_tier():
    status, _ = warning_for("ENG-Premier League", 9, team_comp_games=30)
    assert status == "warning"


def test_la_liga_no_deadline():
    assert warning_for("ESP-La Liga", 4, team_comp_games=30)[0] == "warning"


def test_uefa_four_card_threshold():
    assert warning_for("INT-Champions League", 3, team_comp_games=4)[0] == "warning"
    assert warning_for("INT-Champions League", 2, team_comp_games=4)[0] == ""


def test_cup_two_card_threshold():
    assert warning_for("ENG-FA Cup", 1, team_comp_games=2)[0] == "warning"


def test_below_threshold_no_warning():
    assert warning_for("ENG-Premier League", 3, team_comp_games=5)[0] == ""


def test_unknown_competition_no_rule():
    assert warning_for("INT-Champions League?", 3, team_comp_games=1) == ("", "")
