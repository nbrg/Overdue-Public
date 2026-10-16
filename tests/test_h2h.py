"""Head-to-head card averages."""

from __future__ import annotations

import pandas as pd

from overdue import h2h


def _apps_for_game(game_id, n_cards, n_players=8):
    return [
        dict(game_id=game_id, player=f"{game_id}-{i}", yellow=1 if i < n_cards else 0, red=0)
        for i in range(n_players)
    ]


def test_h2h_average_over_meetings():
    apps = _apps_for_game("g1", 6) + _apps_for_game("g2", 4) + _apps_for_game("other", 0)
    schedule = pd.DataFrame([
        dict(game_id="g1", home_team="Arsenal", away_team="Spurs"),   # 6 cards
        dict(game_id="g2", home_team="Spurs", away_team="Arsenal"),   # 4 cards (reverse)
        dict(game_id="other", home_team="Arsenal", away_team="Chelsea"),  # different pairing
    ])
    games, avg = h2h.h2h_card_average(schedule, pd.DataFrame(apps), "Arsenal", "Spurs")
    assert games == 2
    assert avg == 5.0  # (6 + 4) / 2


def test_h2h_no_meetings():
    apps = _apps_for_game("g1", 3)
    schedule = pd.DataFrame([dict(game_id="g1", home_team="A", away_team="B")])
    games, avg = h2h.h2h_card_average(schedule, pd.DataFrame(apps), "A", "Z")
    assert games == 0 and avg is None


def _a(gid, team, player, y=0, minutes=90):
    return dict(game_id=gid, team=team, player=player, yellow=y, red=0, minutes=minutes)


def test_player_vs_opponent_counts_cards_in_the_fixture():
    apps = pd.DataFrame([
        _a("g1", "Everton", "Gueye", 1), _a("g1", "Chelsea", "Caicedo"),
        _a("g2", "Chelsea", "Caicedo", 1), _a("g2", "Everton", "Gueye", 1),
        _a("g3", "Everton", "Gueye", 0), _a("g3", "Chelsea", "Caicedo", 1),
        _a("g4", "Everton", "Gueye", 1), _a("g4", "Spurs", "Son"),       # other opponent
        _a("g5", "Everton", "Gueye", 1, minutes=0), _a("g5", "Chelsea", "Caicedo"),  # unused sub
    ])
    got = h2h.player_vs_opponent(apps)
    assert got[("Gueye", "Everton", "Chelsea")] == (2, 3)
    assert got[("Caicedo", "Chelsea", "Everton")] == (2, 3)
    assert got[("Gueye", "Everton", "Spurs")] == (1, 1)


def test_strong_h2h_needs_two_cards_in_half_the_meetings():
    assert h2h.is_strong_h2h(2, 4) and h2h.is_strong_h2h(3, 5)
    assert not h2h.is_strong_h2h(1, 1)   # one meeting is too little
    assert not h2h.is_strong_h2h(2, 5)


def test_record_vs_opponent_spans_clubs_and_flags_multi_club():
    apps = pd.DataFrame([
        _a("g1", "Everton", "Gueye", 1), _a("g1", "Chelsea", "Caicedo"),
        _a("g2", "Everton", "Gueye", 0), _a("g2", "Chelsea", "Caicedo"),
        _a("g3", "Watford", "Gueye", 1), _a("g3", "Chelsea", "Caicedo"),  # former club
    ])
    rec = h2h.records_vs_opponent(h2h.player_vs_opponent(apps))
    clubs = rec[("Gueye", "Chelsea")]
    assert clubs == {"Everton": (1, 2), "Watford": (1, 1)}
    assert h2h.h2h_highlight(clubs) == "multi"            # 2/3, booked for two clubs
    assert h2h.h2h_highlight({"Everton": (2, 3)}) == "strong"
    assert h2h.h2h_highlight({"Everton": (2, 3), "Watford": (0, 2)}) == ""  # 2/5
    assert h2h.h2h_highlight({"Everton": (2, 3), "Watford": (0, 1)}) == "strong"
