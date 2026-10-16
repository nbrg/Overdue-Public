"""Static configuration: leagues, seasons, thresholds and paths.

Values here are intentionally simple constants plus a couple of small helpers so
the model's behaviour is easy to eyeball and tune. Anything a user is likely to
want to change (thresholds, the baseline window) lives at the top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Paths -----------------------------------------------------------------

# Repo root is two levels up from this file (src/overdue/config.py).
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
REPORTS_DIR = ROOT / "reports"
TEMPLATES_DIR = ROOT / "templates"

# Compact appearance store (one row per player per match) and run metadata.
APPEARANCES_PATH = DATA_DIR / "appearances.parquet"
CARD_EVENTS_PATH = DATA_DIR / "card_events.parquet"  # per-event cards + subs (with minutes)
META_PATH = DATA_DIR / "meta.json"
POSITIONS_PATH = DATA_DIR / "positions.json"  # player -> season position (GK/DF/MF/FW)


def schedule_cache_path(day_iso: str):
    """Path of the day-stamped schedule cache (reports reuse it within the day)."""
    return DATA_DIR / f"schedule-{day_iso}.parquet"

# --- Leagues ---------------------------------------------------------------

UK_TZ = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class League:
    """A competition we track."""

    league_id: str  # stable key used in the store, e.g. "ENG-Premier League"
    name: str  # human label for UI
    short: str  # compact badge label


# The leagues whose players we actually score. A player is only ever flagged if
# they're a regular here; Europe and the cups just add fixtures and bookings.
DOMESTIC_LEAGUES: tuple[League, ...] = (
    League("ENG-Premier League", "Premier League", "EPL"),
    League("ESP-La Liga", "La Liga", "LaLiga"),
    League("FRA-Ligue 1", "Ligue 1", "L1"),
    League("ITA-Serie A", "Serie A", "SerieA"),
    League("ENG-Championship", "Championship", "EFL Champ"),
)

# UEFA club competitions. Bookings here count toward a player's rate and streak,
# and a European fixture today makes an overdue domestic player show up.
EUROPEAN_LEAGUES: tuple[League, ...] = (
    League("INT-Champions League", "Champions League", "UCL"),
    League("INT-Europa League", "Europa League", "UEL"),
    League("INT-Conference League", "Conference League", "UECL"),
)

# English domestic cups (Prem teams also play these).
ENGLISH_CUP_LEAGUES: tuple[League, ...] = (
    League("ENG-FA Cup", "FA Cup", "FA Cup"),
    League("ENG-EFL Cup", "EFL Cup", "EFL Cup"),
)

# Everything we keep card logs and fixtures for.
LEAGUES: tuple[League, ...] = DOMESTIC_LEAGUES + EUROPEAN_LEAGUES + ENGLISH_CUP_LEAGUES

LEAGUE_BY_ID = {lg.league_id: lg for lg in LEAGUES}
DOMESTIC_LEAGUE_IDS = frozenset(lg.league_id for lg in DOMESTIC_LEAGUES)


# --- Model thresholds (tunable) --------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    # Every appearance counts toward the card rate, regardless of minutes. This
    # threshold only splits "starts" (>= start_minutes) from sub cameos (<) for
    # the separate start-rate and sub-card breakdown.
    start_minutes: int = 30

    # Sample gate: a player must clear both to be scored at all.
    # A "card" is any card — yellow or red — since the market is "to be carded".
    min_appearances: int = 12
    min_cards: int = 3

    # Overdue ratio (= streak / expected-games-between-cards) at/above which a
    # player is flagged as overdue.
    overdue_threshold: float = 1.5

    # How many seasons of history form the baseline rate (including current).
    baseline_seasons: int = 2

    # A "start" uses the data's real starting-XI flag when we have it; where that
    # flag is missing (old data / unparsed lineup) we fall back to minutes played.
    start_minutes_fallback: int = 44

    # "Start share" = share of the team's last N competitive matches (all
    # competitions) the player featured in; a proxy for how likely they start.
    start_share_window: int = 10
    # Only surface players at/above this start share (filters fringe/rotation).
    min_start_share: float = 0.35
    # Drop players who started none of the team's last N games (out of the XI /
    # not in the squad — the 5-DNP ghosts). Applied to every report.
    recent_form_window: int = 5
    min_recent_starts: int = 1
    # Drop players who did not feature in the team's last N consecutive games —
    # a proxy for injury/suspension (the match data has no injury feed). One missed game is
    # allowed (rest/rotation); two or more consecutive absences excludes them.
    max_recent_dnp: int = 2

    # Drop players whose most recent appearance is older than this many days
    # (retired, transferred away, or out all season — they won't feature today).
    max_days_since_last_appearance: int = 40

    # Max players to include in the Telegram summary.
    telegram_top_n: int = 15


MODEL = ModelConfig()


# --- Season helpers --------------------------------------------------------


def current_season_start_year(today: date | None = None) -> int:
    """Return the calendar year in which the current European season began.

    European seasons run roughly Aug–May. We treat July as the rollover point:
    on/after 1 July the new season (that starts this calendar year) is current.
    """
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def season_code(start_year: int) -> str:
    """Season code for a season starting in ``start_year``.

    The two-digit start+end form, e.g. 2026/27 -> "2627".
    """
    end_two = (start_year + 1) % 100
    return f"{start_year % 100:02d}{end_two:02d}"


def baseline_seasons(today: date | None = None, n: int | None = None) -> list[str]:
    """Return the ``n`` most recent season codes, oldest first.

    Oldest-first ordering matters: appearance timelines are built by
    concatenating seasons in chronological order so streaks span boundaries.
    """
    n = n or MODEL.baseline_seasons
    start = current_season_start_year(today)
    years = range(start - (n - 1), start + 1)
    return [season_code(y) for y in years]
