"""Referee card tendencies, derived from the schedule + appearance store.

Referees vary a lot in how many cards they show (some average 5+/game, some 2),
which is one of the strongest signals for card markets. The schedule carries
the referee per match, so we can compute each referee's cards-per-game (yellows
AND reds) from data we already have — no extra source.
"""

from __future__ import annotations

import pandas as pd


def referee_card_rates(
    schedule: pd.DataFrame, appearances: pd.DataFrame, min_games: int = 5
) -> pd.DataFrame:
    """Cards-per-game (yellow + red) by referee.

    Returns a frame indexed by referee with columns ``games``, ``cards`` and
    ``per_game``. Only referees with at least ``min_games`` officiated games in
    the store are included (small samples are unreliable).
    """
    empty = pd.DataFrame(columns=["games", "cards", "per_game"])
    if schedule is None or schedule.empty or appearances.empty:
        return empty
    if "referee" not in schedule.columns:
        return empty

    # Total cards per match = carded appearances (yellow or red) across players.
    reds = appearances["red"] if "red" in appearances.columns else 0
    carded = ((appearances["yellow"] > 0) | (pd.Series(reds, index=appearances.index) > 0)).astype(int)
    cards_per_game = carded.groupby(appearances["game_id"]).sum()

    games = (
        schedule.dropna(subset=["game_id", "referee"])
        .assign(referee=lambda d: d["referee"].astype(str).str.strip())
        .query("referee != ''")
        .drop_duplicates("game_id")[["game_id", "referee"]]
    )
    games["cards"] = games["game_id"].map(cards_per_game)
    games = games.dropna(subset=["cards"])
    if games.empty:
        return empty

    agg = games.groupby("referee").agg(
        games=("game_id", "nunique"), cards=("cards", "sum")
    )
    agg = agg[agg["games"] >= min_games]
    agg["per_game"] = agg["cards"] / agg["games"]
    return agg.sort_values("per_game", ascending=False)


def league_card_average(appearances: pd.DataFrame) -> float:
    """Average total cards (yellow or red) per match across the store.

    This is the baseline a referee's cards/game is measured against: a referee
    on 5.5 in a league averaging 4.0 is ~1.4× strict, which scales a player's
    booking probability for the referee-adjusted odds.
    """
    if appearances is None or appearances.empty or "game_id" not in appearances.columns:
        return 0.0
    reds = appearances["red"] if "red" in appearances.columns else 0
    carded = ((appearances["yellow"] > 0) | (pd.Series(reds, index=appearances.index) > 0)).astype(int)
    per_game = carded.groupby(appearances["game_id"]).sum()
    return float(per_game.mean()) if not per_game.empty else 0.0


def todays_referees(today_fixtures: pd.DataFrame) -> dict[str, str]:
    """Map each team playing today to the referee of its fixture (if known)."""
    mapping: dict[str, str] = {}
    if today_fixtures is None or today_fixtures.empty or "referee" not in today_fixtures.columns:
        return mapping
    for _, row in today_fixtures.iterrows():
        ref = str(row.get("referee") or "").strip()
        if ref and ref.lower() != "nan":
            mapping[str(row["home_team"])] = ref
            mapping[str(row["away_team"])] = ref
    return mapping
