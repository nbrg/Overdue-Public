"""Upcoming referee appointments (who referees which fixture).

The match data only names the referee *after* a game, so pre-match referee
strictness comes from an appointments file, ``data/referee_appointments.json``:
a list of ``{home, away, referee, cards_per_game}``, matched to fixtures by
team name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

APPOINTMENTS_PATH = config.DATA_DIR / "referee_appointments.json"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def save_appointments(appointments: list[dict], path: Path | None = None) -> None:
    path = path or APPOINTMENTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(appointments, indent=2) + "\n", encoding="utf-8")


def load_appointments(path: Path | None = None) -> list[dict]:
    path = path or APPOINTMENTS_PATH
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _norm_team(name: str) -> str:
    """Loose team-name key for matching appointment names to schedule names."""
    name = _clean(name).lower()
    for junk in (" fc", " cf", " afc", " united", " city", "."):
        name = name.replace(junk, "")
    return re.sub(r"[^a-z0-9]", "", name)


def referee_for(appointments: list[dict], home: str, away: str) -> tuple[str, float | None]:
    """Find the appointed referee (and card avg) for a fixture, by team names."""
    h, a = _norm_team(home), _norm_team(away)
    for appt in appointments:
        ah, aa = _norm_team(appt.get("home", "")), _norm_team(appt.get("away", ""))
        if not ah or not aa:
            continue
        # Match either orientation, allowing one side to be a substring of the other.
        pairs = [(h, ah), (a, aa)]
        if all(x and y and (x in y or y in x) for x, y in pairs):
            return _clean(appt.get("referee")), appt.get("cards_per_game")
    # Full names ("FC Bayern München", "Paris Saint-Germain") can differ from
    # the schedule's ("Bayern Munich", "Paris S-G"); fall back to the
    # club-name matcher used for fixtures.
    from .teamnames import same_team

    for appt in appointments:
        if same_team(home, appt.get("home", "")) and same_team(away, appt.get("away", "")):
            return _clean(appt.get("referee")), appt.get("cards_per_game")
    return "", None


def coverage(fixtures, appointments: list[dict]) -> tuple[list[tuple[str, str, str]],
                                                          list[tuple[str, str]]]:
    """Split ``fixtures`` into games with a known referee and games without one.

    A referee counts as known from an appointment or, failing that, the
    fixture's own ``referee`` column (some officials are named pre-match).
    Returns ``([(home, away, referee)], [(home, away)])``.
    """
    found: list[tuple[str, str, str]] = []
    missing: list[tuple[str, str]] = []
    if fixtures is None or getattr(fixtures, "empty", True):
        return found, missing
    for _, fx in fixtures.iterrows():
        home, away = str(fx["home_team"]), str(fx["away_team"])
        ref, _ = referee_for(appointments or [], home, away)
        if not ref and "referee" in fixtures.columns:
            raw = fx["referee"]
            ref = "" if raw is None or str(raw) in ("", "nan", "<NA>", "None") else str(raw)
        if ref:
            found.append((home, away, ref))
        else:
            missing.append((home, away))
    return found, missing


