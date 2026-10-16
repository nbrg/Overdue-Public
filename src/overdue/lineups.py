"""Today's line-up status store: who starts, who's on the bench, who's out.

Line-ups are kept in ``data/lineups.json`` as
``{"date": "YYYY-MM-DD", "fixtures": [{home, away, confirmed, starters, bench, out}]}``
and turned into a ``player_key -> status`` index the reports filter on. How the
file gets filled is up to you: hand-written, or produced by your own line-up
feed. A file dated other than today is ignored, so a stale XI never filters
today's report.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from . import config

LINEUPS_PATH = config.DATA_DIR / "lineups.json"


def _norm_team(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# Latin letters that don't decompose to ASCII under NFKD (no combining mark to
# strip), so we fold them by hand — otherwise 'Öztunalı' and 'Oztunali' wouldn't
# key alike. Covers the ones common in our leagues' player names.
_LATIN_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l",
    "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ħ": "h",
})


def _unaccent(s: str) -> str:
    """Strip accents so 'Poznań' / 'Öztunalı' compare as plain ASCII."""
    folded = str(s).translate(_LATIN_FOLD)
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c)
    )
    return stripped.replace("ß", "ss")


def _player_key(name: str) -> str:
    """A loose key so different spellings of one player collide.

    A line-up may show "J. Mateta" where the store has "Jean-Philippe Mateta". Both reduce
    to ``"j|mateta"`` — first initial + last surname token, unaccented and
    lowercased — which is enough to match within a single day's squads.
    """
    n = _unaccent(name).lower().replace("'", "").replace("’", "")  # O'Shea -> oshea
    n = re.sub(r"[^a-z0-9]+", " ", n)
    toks = [t for t in n.split() if t]
    if not toks:
        return ""
    return f"{toks[0][0]}|{toks[-1]}"


def status_index(lineups: dict) -> dict[str, str]:
    """Flatten stored line-ups to ``player_key -> status`` for the whole slate.

    ``status`` is ``"starter"`` / ``"bench"`` / ``"out"``. Players not named on
    any fixture aren't in the map (their status is unknown). On a key collision,
    the more actionable status wins (starter/out over bench) so a confirmed
    starter is never demoted to a keep-able bench slot by a namesake.
    """
    priority = {"out": 3, "starter": 2, "bench": 1}
    index: dict[str, str] = {}
    for fx in lineups.get("fixtures", []):
        # The "out" (injured/suspended) list is trustworthy pre-kick-off, but the
        # starters/bench are only real once the XI is CONFIRMED. For an
        # unconfirmed (predicted) line-up we use out only, so we never drop a
        # target on a guessed XI.
        groups = {"out": "out"}
        if fx.get("confirmed"):
            groups.update({"starters": "starter", "bench": "bench"})
        for list_key, status in groups.items():
            for name in fx.get(list_key, []):
                key = _player_key(name)
                if not key:
                    continue
                if priority.get(status, 0) >= priority.get(index.get(key, ""), 0):
                    index[key] = status
    return index


def status_for(index: dict[str, str], player: str) -> str:
    """Look a player's confirmed line-up status up, or ``"unknown"``."""
    return index.get(_player_key(player), "unknown")


def save_lineups(lineups: dict, path: Path | None = None) -> None:
    path = path or LINEUPS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lineups, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_lineups(path: Path | None = None) -> dict:
    path = path or LINEUPS_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_lineups_for(day, path: Path | None = None) -> dict:
    """Load stored line-ups only if they were captured for ``day``.

    Confirmed XIs are date-specific: yesterday's starters must never filter
    today's report. So a stored file whose ``date`` isn't ``day`` is ignored
    (returns ``{}``), and the reports fall back to no line-up filtering.
    """
    lineups = load_lineups(path)
    want = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return lineups if lineups.get("date") == want else {}


