"""Head-to-head card history for a fixture.

How many cards a specific pairing tends to produce — derby/rivalry intensity
shows up here. Computed from past meetings of the two teams in the store
(any competition, either home/away orientation).
"""

from __future__ import annotations

import pandas as pd


def _cards_per_game(appearances: pd.DataFrame) -> pd.Series:
    reds = appearances["red"] if "red" in appearances.columns else 0
    carded = ((appearances["yellow"] > 0) | (pd.Series(reds, index=appearances.index) > 0)).astype(int)
    return carded.groupby(appearances["game_id"]).sum()


def h2h_card_average(
    schedule: pd.DataFrame, appearances: pd.DataFrame, team_a: str, team_b: str
) -> tuple[int, float | None]:
    """Return (meetings, average total cards) for past ``team_a`` v ``team_b`` games."""
    if schedule is None or schedule.empty or appearances.empty:
        return 0, None
    cpg = _cards_per_game(appearances)

    s = schedule.dropna(subset=["game_id"])
    mask = (
        (s["home_team"] == team_a) & (s["away_team"] == team_b)
    ) | (
        (s["home_team"] == team_b) & (s["away_team"] == team_a)
    )
    game_ids = s.loc[mask, "game_id"].dropna().unique()
    values = [cpg.get(g) for g in game_ids]
    values = [float(v) for v in values if v is not None and not pd.isna(v)]
    if not values:
        return 0, None
    return len(values), sum(values) / len(values)


def player_vs_opponent(appearances: pd.DataFrame) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Map ``(player, team, opponent)`` to ``(games carded, games played)``.

    Every match in the store where the player featured (any minutes) for
    ``team`` against ``opponent``, in any competition and either venue — so a
    lookup with the player's current club and today's opponent gives his record
    in this exact fixture.
    """
    if appearances is None or appearances.empty:
        return {}
    df = appearances.dropna(subset=["game_id"])
    if "minutes" in df.columns:
        df = df[df["minutes"].fillna(0) > 0]
    if df.empty:
        return {}
    teams = df.groupby("game_id")["team"].unique()
    opp = {}
    for gid, ts in teams.items():
        if len(ts) == 2:
            opp[(gid, ts[0])] = ts[1]
            opp[(gid, ts[1])] = ts[0]
    df = df.assign(opponent=[opp.get((g, t)) for g, t in zip(df["game_id"], df["team"])])
    df = df[df["opponent"].notna()]
    reds = df["red"].fillna(0) if "red" in df.columns else 0
    df = df.assign(carded=((df["yellow"].fillna(0) > 0) | (reds > 0)).astype(int))
    # One row per player per game (guards against duplicate store rows).
    per_game = df.groupby(["player", "team", "opponent", "game_id"])["carded"].max()
    agg = per_game.groupby(level=[0, 1, 2]).agg(["sum", "count"])
    return {k: (int(v["sum"]), int(v["count"])) for k, v in agg.iterrows()}


def records_vs_opponent(
    vs: dict[tuple[str, str, str], tuple[int, int]]
) -> dict[tuple[str, str], dict[str, tuple[int, int]]]:
    """Regroup :func:`player_vs_opponent` as ``(player, opponent) -> {club: (cards, games)}``,
    so a player's record against a side counts every club he faced them with."""
    out: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
    for (player, team, opp), rec in vs.items():
        out.setdefault((player, opp), {})[team] = rec
    return out


def h2h_highlight(clubs: dict[str, tuple[int, int]]) -> str:
    """"" / "strong" / "multi": a strong record (:func:`is_strong_h2h` on the
    totals), and "multi" when he was carded against them for 2+ different clubs."""
    cards = sum(c for c, _ in clubs.values())
    games = sum(g for _, g in clubs.values())
    if not is_strong_h2h(cards, games):
        return ""
    return "multi" if sum(1 for c, _ in clubs.values() if c > 0) >= 2 else "strong"


def is_strong_h2h(cards: int, games: int) -> bool:
    """A notably good card record in this fixture: 2+ cards in at least half the meetings."""
    return games >= 2 and cards >= 2 and cards * 2 >= games
