"""Odds — pluggable interface, deliberately not wired to a live feed in v1.

Neither Bet365 nor Sky Bet exposes a clean, permitted API for player 'to be
carded' markets, and general odds APIs that carry soccer player props are limited
to US bookmakers. So v1 ships *no* live prices: the user looks them up manually.

We still model odds as a provider Protocol so a real feed can be dropped in later
without touching the rest of the pipeline, and we build convenience look-up links
so the daily output points straight at where to check a price.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from urllib.parse import quote_plus


@runtime_checkable
class OddsProvider(Protocol):
    """Anything that can return a booking price for a player, someday."""

    def card_price(self, player: str, team: str, opponent: str | None = None) -> str | None:
        """Return a display price (e.g. '5/2') or ``None`` if unavailable."""
        ...


class NullOddsProvider:
    """The v1 provider: never has a price. Keeps the pipeline odds-agnostic."""

    def card_price(self, player: str, team: str, opponent: str | None = None) -> str | None:
        return None


def lookup_links(player: str, team: str) -> dict[str, str]:
    """Build convenience search links for looking a booking price up manually.

    These are plain search URLs — a nudge to the right page.
    """
    q = quote_plus(f"{player} to be booked odds")
    return {
        "google": f"https://www.google.com/search?q={q}",
        "skybet": "https://m.skybet.com/football",
        "bet365": "https://www.bet365.com/#/AC/B1/C1/D1002/",
    }
