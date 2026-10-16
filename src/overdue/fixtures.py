"""Work out which teams play on a given day (Europe/London)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from . import config


def uk_today(now: datetime | None = None) -> date:
    """Today's date in the Europe/London timezone."""
    now = now or datetime.now(config.UK_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=config.UK_TZ)
    return now.astimezone(config.UK_TZ).date()


def uk_now(now: datetime | None = None) -> datetime:
    """Current wall-clock time in the Europe/London timezone (tz-aware)."""
    now = now or datetime.now(config.UK_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=config.UK_TZ)
    return now.astimezone(config.UK_TZ)


def teams_playing(fixtures: pd.DataFrame) -> set[str]:
    """Set of team names involved in the given fixtures frame."""
    if fixtures.empty:
        return set()
    home = set(fixtures["home_team"].dropna().astype(str))
    away = set(fixtures["away_team"].dropna().astype(str))
    return home | away


def opponent_map(fixtures: pd.DataFrame) -> dict[str, str]:
    """Map each team to its opponent for the day (last fixture wins if duplicated)."""
    mapping: dict[str, str] = {}
    for _, row in fixtures.iterrows():
        home, away = str(row["home_team"]), str(row["away_team"])
        mapping[home] = away
        mapping[away] = home
    return mapping


def competition_map(fixtures: pd.DataFrame) -> dict[str, str]:
    """Map each team to the league id of its fixture today."""
    mapping: dict[str, str] = {}
    for _, row in fixtures.iterrows():
        league = str(row["league"])
        mapping[str(row["home_team"])] = league
        mapping[str(row["away_team"])] = league
    return mapping
