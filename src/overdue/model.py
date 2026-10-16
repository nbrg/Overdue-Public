"""The 'overdue' model.

Pure functions over the appearance store (see :mod:`overdue.store`). No network,
no I/O — everything here is unit-tested on synthetic timelines.

Definitions (all over the baseline window held in the store):

* **appearance** — any match the player featured in (any minutes).
* **card** — a yellow OR a red; we don't distinguish (market is "to be carded").
* **card rate** ``r`` — ``carded appearances / appearances``.
* **expected interval** — ``1 / r`` appearances between cards.
* **streak** ``k`` — consecutive most-recent appearances with no card.
* **overdue ratio** — ``k / (1/r) = k * r``. ~1 is average; higher = more overdue.

A player is *scored* only if they clear the sample gate (enough appearances and
enough cards for the rate to mean something), and *flagged overdue* when their
overdue ratio meets the configured threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import DOMESTIC_LEAGUE_IDS, MODEL, ModelConfig


@dataclass
class PlayerMetrics:
    player: str
    team: str
    league: str
    appearances: int  # all matches played (any minutes)
    cards: int  # appearances in which the player was carded (yellow or red)
    rate: float  # cards per appearance (over all appearances)
    per90: float  # per 90 minutes
    fouls: int  # total fouls committed (leading indicator of cards)
    fouls_per90: float  # fouls committed per 90 minutes
    fouls_per_card: float  # fouls committed per card received (0.0 if no cards)
    fouls_drawn_per90: float  # fouls the player draws (wins) per 90 minutes
    expected_interval: float  # appearances between cards (1/rate)
    streak: int  # appearances since last card
    streak_starts: int  # of those streak appearances, how many were starts
    starts: int  # appearances of >= start_minutes
    start_cards: int  # cards received in start appearances
    start_rate: float  # cards per start
    sub_apps: int  # appearances of < start_minutes (came off the bench)
    sub_cards: int  # cards received in those sub cameo appearances
    overdue_ratio: float  # streak / expected_interval
    last_card_date: pd.Timestamp | None
    last_appearance_date: pd.Timestamp | None  # most recent match played
    start_share: float  # share of team's latest-season games featured in
    recent_starts: int  # starts in the team's last `recent_form_window` games
    recent_dnp: int  # consecutive most-recent team games not featured in (injury proxy)
    qualifies: bool  # cleared the sample gate
    overdue: bool  # qualifies AND ratio >= threshold


def started_mask(df: pd.DataFrame, cfg: ModelConfig) -> pd.Series:
    """Boolean 'was a starter' per appearance.

    Uses the data's real starting-XI flag (``started`` 1/0) where we have it, and
    falls back to minutes played only where it's unknown (``started`` -1 / absent).
    """
    minutes_start = df["minutes"] >= cfg.start_minutes_fallback
    if "started" not in df.columns:
        return minutes_start
    started = df["started"]
    return (started == 1) | ((started < 0) & minutes_start)


def is_started_value(started_val, minutes: int, cfg: ModelConfig) -> bool:
    """Scalar version of :func:`started_mask` for a single appearance."""
    if started_val == 1:
        return True
    if started_val is None or started_val < 0:
        return minutes >= cfg.start_minutes_fallback
    return False  # explicit substitute (started_val == 0)


def _trailing_streak(yellow_flags: list[int]) -> int:
    """Count consecutive trailing zeros (no-yellow appearances) from the end."""
    streak = 0
    for flag in reversed(yellow_flags):
        if flag:
            break
        streak += 1
    return streak


def _team_recent_matches(appearances: pd.DataFrame, window: int) -> dict[str, set]:
    """Map team -> set of its ``window`` most recent match ids across all comps.

    Built from every stored appearance (not minutes-filtered) so it reflects the
    team's actual recent fixtures, regardless of which players we're scoring.
    """
    df = (
        appearances.dropna(subset=["game_id", "date"])[["team", "game_id", "date"]]
        .drop_duplicates(["team", "game_id"])
        .sort_values("date")
    )
    return {team: set(grp["game_id"].tail(window)) for team, grp in df.groupby("team")}


def _team_recent_ordered(appearances: pd.DataFrame, window: int) -> dict[str, list]:
    """Map team -> its last ``window`` game ids, newest first."""
    df = (
        appearances.dropna(subset=["game_id", "date"])[["team", "game_id", "date"]]
        .drop_duplicates(["team", "game_id"])
        .sort_values("date")
    )
    return {team: list(grp["game_id"].tail(window))[::-1] for team, grp in df.groupby("team")}


def compute_player_metrics(
    appearances: pd.DataFrame, cfg: ModelConfig | None = None
) -> list[PlayerMetrics]:
    """Compute overdue metrics for every in-scope player.

    Rate and streak are computed over *all* competitions (domestic + European) so
    a booking anywhere resets the streak, but only players with domestic-league
    appearances are scored — their identity (team/league/start-share) comes from
    the domestic side.
    """
    cfg = cfg or MODEL
    if appearances.empty:
        return []

    # Every match played counts (any minutes). Rows with 0 minutes = didn't play.
    played = appearances[appearances["minutes"] >= 1].copy()
    if played.empty:
        return []
    played = played.sort_values(["player", "date", "game_id"])

    team_recent = _team_recent_matches(appearances, cfg.start_share_window)
    team_recent_form = _team_recent_matches(appearances, cfg.recent_form_window)
    team_recent_order = _team_recent_ordered(appearances, cfg.start_share_window)

    # Clubs that actually play in a tracked domestic league (EPL/La Liga/Ligue 1).
    # A player's current club must be one of these, so that a player whose *main*
    # league we don't track (e.g. a Liga Portugal club appearing only via the
    # Champions League, or a same-name collision) is not scored on European games
    # alone.
    domestic_teams = set(
        appearances.loc[appearances["league"].isin(DOMESTIC_LEAGUE_IDS), "team"]
        .dropna().astype(str)
    )

    results: list[PlayerMetrics] = []
    for player, grp in played.groupby("player", sort=False):
        grp = grp.sort_values(["date", "game_id"])

        # Scope: only score players who feature in one of our domestic leagues…
        domestic_grp = grp[grp["league"].isin(DOMESTIC_LEAGUE_IDS)]
        if domestic_grp.empty:
            continue
        # …and whose current club is itself a tracked domestic-league club.
        if str(grp.iloc[-1]["team"]) not in domestic_teams:
            continue

        appearances_n = len(grp)  # across all competitions, any minutes
        # "Card" = a yellow OR a red — the market is "player to be carded", so we
        # don't distinguish. A carded appearance resets the streak and counts
        # toward the rate.
        carded = (grp["yellow"] > 0) | (grp["red"] > 0)
        cards = int(carded.sum())
        card_flags = carded.astype(int).tolist()

        # Split starts (real starting-XI flag, minutes fallback) from bench
        # appearances, for a per-start rate and cards picked up off the bench.
        is_start = started_mask(grp, cfg)
        starts = int(is_start.sum())
        start_cards = int((carded & is_start).sum())
        sub_apps = int((~is_start).sum())
        sub_cards = int((carded & ~is_start).sum())
        start_rate = (start_cards / starts) if starts else 0.0

        # Identity comes from the most recent domestic appearance; team from the
        # most recent appearance overall (same club name across competitions).
        team = str(grp.iloc[-1]["team"])
        league = str(domestic_grp.iloc[-1]["league"])

        rate = cards / appearances_n if appearances_n else 0.0
        total_minutes = int(grp["minutes"].sum())
        per90 = (cards / (total_minutes / 90.0)) if total_minutes else 0.0
        # Fouls committed are the leading indicator of a booking: a high-foul
        # player on a cardless streak is a genuine target; a low-foul one isn't.
        fouls = int(grp["fouls"].sum()) if "fouls" in grp.columns else 0
        fouls_per90 = (fouls / (total_minutes / 90.0)) if total_minutes else 0.0
        fouls_per_card = (fouls / cards) if cards else 0.0
        # Fouls drawn (won) — a player who wins lots of fouls draws bookings from
        # opponents; shown per player so you can eyeball it against a fixture.
        fouls_drawn = int(grp["fouled"].sum()) if "fouled" in grp.columns else 0
        fouls_drawn_per90 = (fouls_drawn / (total_minutes / 90.0)) if total_minutes else 0.0
        expected_interval = (1.0 / rate) if rate > 0 else float("inf")
        streak = _trailing_streak(card_flags)
        # How many of the cardless-run appearances were starts (vs sub cameos)?
        # The streak is the last ``streak`` rows of grp (sorted by date), and
        # ``is_start`` is aligned to grp, so slice its tail.
        streak_starts = int(is_start.iloc[len(grp) - streak:].sum()) if streak else 0
        overdue_ratio = (streak / expected_interval) if expected_interval not in (0, float("inf")) else 0.0

        booked = grp[carded]
        last_card_date = booked["date"].max() if not booked.empty else None
        last_appearance_date = grp["date"].max()

        # Start share = share of the team's last N competitive matches STARTED.
        started_gids = set(grp.loc[is_start, "game_id"])
        recent = team_recent.get(team, set())
        start_share = (len(started_gids & recent) / len(recent)) if recent else 0.0
        # Starts within the team's last few games (out-of-XI ghosts start 0).
        recent_form = team_recent_form.get(team, set())
        recent_starts = len(started_gids & recent_form)
        # Consecutive most-recent team games the player did NOT feature in — a
        # proxy for injury/suspension (an absent player stops appearing).
        played_gids = set(grp["game_id"])
        recent_dnp = 0
        for gid in team_recent_order.get(team, []):
            if gid in played_gids:
                break
            recent_dnp += 1

        qualifies = appearances_n >= cfg.min_appearances and cards >= cfg.min_cards
        overdue = qualifies and overdue_ratio >= cfg.overdue_threshold

        results.append(
            PlayerMetrics(
                player=str(player),
                team=team,
                league=league,
                appearances=appearances_n,
                cards=cards,
                rate=rate,
                per90=per90,
                fouls=fouls,
                fouls_per90=fouls_per90,
                fouls_per_card=fouls_per_card,
                fouls_drawn_per90=fouls_drawn_per90,
                expected_interval=expected_interval,
                streak=streak,
                streak_starts=streak_starts,
                starts=starts,
                start_cards=start_cards,
                start_rate=start_rate,
                sub_apps=sub_apps,
                sub_cards=sub_cards,
                overdue_ratio=overdue_ratio,
                last_card_date=last_card_date,
                last_appearance_date=last_appearance_date,
                start_share=start_share,
                recent_starts=recent_starts,
                recent_dnp=recent_dnp,
                qualifies=qualifies,
                overdue=overdue,
            )
        )

    return results


def metrics_to_frame(metrics: list[PlayerMetrics]) -> pd.DataFrame:
    """Convert a list of :class:`PlayerMetrics` into a DataFrame."""
    if not metrics:
        return pd.DataFrame(
            columns=[f.name for f in PlayerMetrics.__dataclass_fields__.values()]  # type: ignore[attr-defined]
        )
    return pd.DataFrame([m.__dict__ for m in metrics])
