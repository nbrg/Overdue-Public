"""Persistence for the compact appearance store.

The store is a single parquet file with **one row per player per match** they
featured in, plus a small JSON metadata sidecar tracking which match ids
have already been ingested (so daily runs stay incremental).

Keeping the schema explicit here means the rest of the code can rely on these
column names regardless of the data source's quirks upstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config

# Identity columns (strings) that key each row.
ID_COLUMNS: tuple[str, ...] = (
    "league",  # league id, e.g. "ENG-Premier League"
    "season",  # season code, e.g. "2627"
    "game_id",  # match id (stable key)
    "game",  # human game string "YYYY-MM-DD Home-Away"
    "team",  # team the player played for
    "player",  # player name
)

# Per-player, per-match stat columns (integer counts) from the match summary
# table. ``yellow``/``red`` stay 0/1 flags; the rest are raw counts. The model
# only uses minutes + yellow; everything else is kept for your own analysis.
STAT_COLUMNS: tuple[str, ...] = (
    "minutes",  # minutes played
    "yellow",  # 1 if booked (yellow), else 0
    "red",  # 1 if sent off, else 0
    "goals",
    "assists",
    "shots",
    "shots_on_target",
    "pens_scored",  # penalty kicks made
    "pens_att",  # penalty kicks attempted
    "fouls",  # fouls committed
    "fouled",  # times fouled
    "offsides",
    "crosses",
    "tackles_won",
    "interceptions",
    "own_goals",
    "pens_won",
    "pens_conceded",
)

# Real starting-XI flag from the match line-up (1 started, 0 sub, -1 unknown).
# Kept separate from STAT_COLUMNS because its "missing" default is -1, not 0.
FLAG_COLUMNS: tuple[str, ...] = ("started",)

# Full canonical column order of the store.
COLUMNS: tuple[str, ...] = (
    ID_COLUMNS[:4] + ("date",) + ID_COLUMNS[4:] + STAT_COLUMNS + FLAG_COLUMNS
)


def empty_frame() -> pd.DataFrame:
    """An empty appearance frame with the canonical dtypes."""
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})
    return _coerce_dtypes(df)


def _coerce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Tolerate older stores that predate newer stat columns: add any missing.
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in STAT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    for col in FLAG_COLUMNS:
        # Unknown (e.g. a store predating line-up capture) defaults to -1.
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(-1).astype("int64")
    for col in ID_COLUMNS:
        df[col] = df[col].astype("string")
    return df[list(COLUMNS)]


def load_appearances(path: Path | None = None) -> pd.DataFrame:
    """Load the appearance store, or an empty frame if it doesn't exist."""
    path = path or config.APPEARANCES_PATH
    if not path.exists():
        return empty_frame()
    return _coerce_dtypes(pd.read_parquet(path))


def save_appearances(df: pd.DataFrame, path: Path | None = None) -> None:
    """Persist the appearance store as parquet (dedup on player+game_id)."""
    path = path or config.APPEARANCES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _coerce_dtypes(df)
    df = (
        df.dropna(subset=["game_id", "player"])
        .drop_duplicates(subset=["game_id", "team", "player"], keep="last")
        .sort_values(["date", "game_id", "team", "player"])
        .reset_index(drop=True)
    )
    df.to_parquet(path, index=False)


# --- Card/substitution events (one row per event, with the minute) ---------

EVENT_COLUMNS: tuple[str, ...] = (
    "league", "season", "game", "game_id", "date", "team",
    "player", "player_off", "minute", "minute_plus", "kind",
)


def _coerce_events(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in EVENT_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("minute", "minute_plus"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    for col in ("league", "season", "game", "game_id", "team", "player", "player_off", "kind"):
        df[col] = df[col].astype("string")
    return df[list(EVENT_COLUMNS)]


def load_events(path: Path | None = None) -> pd.DataFrame:
    path = path or config.CARD_EVENTS_PATH
    if not path.exists():
        return _coerce_events(pd.DataFrame(columns=EVENT_COLUMNS))
    return _coerce_events(pd.read_parquet(path))


def save_events(df: pd.DataFrame, path: Path | None = None) -> None:
    path = path or config.CARD_EVENTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _coerce_events(df).dropna(subset=["game_id", "player"])
    # Dedupe on the event's natural key (a player can have two cards at different
    # minutes, so minute is part of the key).
    df = (df.drop_duplicates(subset=["game_id", "team", "player", "minute", "kind"], keep="last")
            .sort_values(["date", "game_id", "minute"]).reset_index(drop=True))
    df.to_parquet(path, index=False)


def merge_events(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([existing, new], ignore_index=True)
    return _coerce_events(combined).drop_duplicates(
        subset=["game_id", "team", "player", "minute", "kind"], keep="last")


def load_positions(path: Path | None = None) -> dict[str, str]:
    """Load the cached player -> position map, or empty if not built yet."""
    path = path or config.POSITIONS_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}


def save_positions(positions: dict[str, str], path: Path | None = None) -> None:
    path = path or config.POSITIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(positions.items())), indent=2) + "\n", encoding="utf-8")


def merge_appearances(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append ``new`` rows to ``existing``, de-duplicating on player+game."""
    combined = pd.concat([existing, new], ignore_index=True)
    return _coerce_dtypes(combined).drop_duplicates(
        subset=["game_id", "team", "player"], keep="last"
    )


# --- Run metadata ----------------------------------------------------------


@dataclass
class Meta:
    """Small bookkeeping record persisted alongside the store."""

    ingested_game_ids: set[str]
    last_run_utc: str | None = None
    # Matches the event ingest has already tried. A match here that has no rows in
    # the event store returned nothing (gaps in the source for some competitions),
    # so it isn't re-fetched every run — `refresh-events --retry-missing` retries it.
    events_attempted_game_ids: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path | None = None) -> "Meta":
        path = path or config.META_PATH
        if not path.exists():
            return cls(ingested_game_ids=set())
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            ingested_game_ids=set(raw.get("ingested_game_ids", [])),
            last_run_utc=raw.get("last_run_utc"),
            events_attempted_game_ids=set(raw.get("events_attempted_game_ids", [])),
        )

    def save(self, path: Path | None = None) -> None:
        path = path or config.META_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ingested_game_ids": sorted(self.ingested_game_ids),
            "events_attempted_game_ids": sorted(self.events_attempted_game_ids),
            "last_run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
