"""Referee card-rate computation from schedule + appearances."""

from __future__ import annotations

import pandas as pd

from overdue import referees


def _appearances_for_game(game_id, n_yellows, n_players=10):
    rows = []
    for i in range(n_players):
        rows.append(dict(game_id=game_id, player=f"{game_id}-p{i}", team="T",
                         yellow=1 if i < n_yellows else 0))
    return rows


def test_referee_card_rates():
    # Ref A officiates 6 games averaging 5 yellows; Ref B 6 games averaging 2.
    apps = []
    sched = []
    for g in range(6):
        apps += _appearances_for_game(f"a{g}", 5)
        sched.append(dict(game_id=f"a{g}", referee="Mike Dean"))
    for g in range(6):
        apps += _appearances_for_game(f"b{g}", 2)
        sched.append(dict(game_id=f"b{g}", referee="Calm Ref"))
    appearances = pd.DataFrame(apps)
    schedule = pd.DataFrame(sched)

    rates = referees.referee_card_rates(schedule, appearances, min_games=5)
    assert rates.loc["Mike Dean", "per_game"] == 5.0
    assert rates.loc["Calm Ref", "per_game"] == 2.0
    # Most card-happy first.
    assert rates.index[0] == "Mike Dean"


def test_min_games_filters_small_samples():
    apps = _appearances_for_game("x0", 4)
    schedule = pd.DataFrame([dict(game_id="x0", referee="One Game")])
    rates = referees.referee_card_rates(schedule, pd.DataFrame(apps), min_games=5)
    assert rates.empty


def test_todays_referees_maps_teams():
    fx = pd.DataFrame([dict(home_team="Arsenal", away_team="Chelsea", referee="Mike Dean")])
    m = referees.todays_referees(fx)
    assert m["Arsenal"] == "Mike Dean" and m["Chelsea"] == "Mike Dean"


def test_todays_referees_handles_missing():
    fx = pd.DataFrame([dict(home_team="A", away_team="B", referee="")])
    assert referees.todays_referees(fx) == {}
