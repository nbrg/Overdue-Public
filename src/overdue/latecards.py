"""Late-card tendencies from the per-event card store (minutes).

Complements the sub-card report: a card shown late in a game is a distinct
signal (game-management fouls, tactical bookings, tiring legs). Using the card
*minute* captured in the event store, we measure how often a player is booked
late — both as a starter running down the clock and as a sub.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

_CARD_KINDS = ("yellow", "red", "second_yellow")


@dataclass
class LateCardStat:
    cards: int  # card events with a known minute
    late: int  # of those, shown at/after the threshold minute
    late_share: float  # late / cards
    avg_minute: float  # mean minute of their cards


def late_card_stats(events: pd.DataFrame, threshold: int = 70) -> dict[str, LateCardStat]:
    """Per-player late-card stats from the event store.

    Only card events with a recorded minute (> 0) are counted. ``threshold`` is
    the minute at/after which a card counts as "late" (default 70).
    """
    if events is None or events.empty:
        return {}
    cards = events[events["kind"].isin(_CARD_KINDS) & (events["minute"] > 0)]
    if cards.empty:
        return {}

    out: dict[str, LateCardStat] = {}
    for player, grp in cards.groupby("player"):
        n = len(grp)
        late = int((grp["minute"] >= threshold).sum())
        out[str(player)] = LateCardStat(
            cards=n, late=late,
            late_share=(late / n) if n else 0.0,
            avg_minute=float(grp["minute"].mean()),
        )
    return out
