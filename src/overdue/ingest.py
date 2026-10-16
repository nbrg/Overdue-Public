"""Populate and update the appearance store from the match data source.

Two entry points:

* :func:`backfill` — one-off: pulls the full baseline history into the local
  store (with a real feed this is the slow step, so it saves as it goes).
* :func:`incremental_update` — cheap daily top-up: fetches only matches that have
  been played since the last run and appends them.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from . import config, source, store

logger = logging.getLogger(__name__)


def _attach_dates(appearances: pd.DataFrame, schedule: pd.DataFrame) -> pd.DataFrame:
    """Join match dates onto appearance rows via game_id."""
    dates = schedule[["game_id", "date"]].drop_duplicates("game_id")
    merged = appearances.merge(dates, on="game_id", how="left")
    return merged


def _played_game_ids(schedule: pd.DataFrame) -> list[str]:
    played = schedule.loc[schedule["played"], "game_id"].dropna().unique().tolist()
    return [str(g) for g in played]


def backfill(
    seasons: list[str] | None = None,
    leagues: list[str] | None = None,
    batch_size: int | None = None,
    force: bool = False,
) -> pd.DataFrame:
    """Fetch played matches across the baseline seasons into the store.

    ``leagues`` restricts which competitions to fetch.

    Without ``force`` this ingests only matches **not yet** in the store. It runs
    in small batches, **saving after each one**, so an interrupted run leaves a
    partial, resumable store.

    With ``force`` it re-reads **only the matches already in the store** — the
    way to apply new columns (e.g. the starter flag, card counts) to an existing
    store — in a **single batch**, since a source's per-call overhead (reading
    the schedule) usually dwarfs the per-match cost.

    ``batch_size`` overrides the batch size (default: 20, or all matches in one
    go when ``force``).
    """
    seasons = seasons or config.baseline_seasons()
    schedule = source.read_schedule(seasons, leagues)
    meta = store.Meta.load()
    appearances = store.load_appearances()

    played = _played_game_ids(schedule)
    if force:
        # Re-read ONLY already-ingested matches.
        todo = [g for g in played if g in meta.ingested_game_ids]
        # One big batch: fewer calls = far faster. All matches fit in memory.
        batch_size = batch_size or max(len(todo), 1)
        logger.info("Backfill --force: re-reading %d already-ingested matches "
                    "in %d batch(es)", len(todo),
                    (len(todo) + batch_size - 1) // batch_size)
    else:
        todo = [g for g in played if g not in meta.ingested_game_ids]
        batch_size = batch_size or 20
        logger.info("Backfill: %d new matches to ingest (%d already done)",
                    len(todo), len(meta.ingested_game_ids))

    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        logger.info("Ingesting matches %d-%d of %d", start + 1, start + len(batch), len(todo))
        new = source.read_match_appearances(batch, seasons, leagues)
        new = _attach_dates(new, schedule)
        appearances = store.merge_appearances(appearances, new)
        meta.ingested_game_ids.update(batch)
        store.save_appearances(appearances)
        meta.save()
        if not force:
            # Store the same matches' card/sub events as we go, instead of
            # needing a separate refresh-events run.
            try:
                ingest_events(seasons=seasons, leagues=leagues, only_games=batch,
                              schedule=schedule)
            except Exception as exc:  # noqa: BLE001 - events are best-effort
                logger.warning("Event ingest for this batch failed: %s", exc)

    return appearances


def ingest_events(
    seasons: list[str] | None = None,
    leagues: list[str] | None = None,
    batch_size: int = 20,
    force: bool = False,
    only_games: list[str] | None = None,
    schedule: pd.DataFrame | None = None,
    retry_missing: bool = False,
) -> pd.DataFrame:
    """Populate the card/substitution event store (with minutes).

    Only matches not already in the event store are fetched (unless ``force``).

    Matches the full backfill has already tried but that returned no events (a
    source with gaps for some competitions) are remembered and skipped on later runs
    so the same dead backlog isn't re-read every time; ``retry_missing`` (or
    ``force``) tries them again. The daily catch-up (``only_games`` set) always
    retries its matches — a just-played game's events can appear a bit later.

    ``only_games`` restricts the candidate matches; ``schedule`` is reused if the
    caller already read it.
    """
    seasons = seasons or config.baseline_seasons()
    if schedule is None:
        schedule = source.read_schedule(seasons, leagues)
    dates = schedule[["game_id", "game", "date"]].drop_duplicates("game_id")
    date_by_game = {str(r["game"]): (str(r["game_id"]), r["date"]) for _, r in dates.iterrows()}
    date_by_gid = {str(r["game_id"]): r["date"] for _, r in dates.iterrows()}

    events = store.load_events()
    have = set(events["game_id"].dropna().unique()) if not events.empty else set()
    meta = store.Meta.load()
    # Skip matches already tried-and-empty, but only for the full backfill — the
    # daily catch-up (only_games) always retries, and force/retry_missing override.
    skip_tried = (only_games is None and not force and not retry_missing)
    attempted = meta.events_attempted_game_ids if skip_tried else set()
    candidates = only_games if only_games is not None else _played_game_ids(schedule)
    todo = [g for g in candidates if force or (g not in have and g not in attempted)]
    n_skipped_tried = 0 if not skip_tried else len(
        [g for g in candidates if g not in have and g in attempted])
    logger.info("Event ingest: %d matches to read (%d already done%s)", len(todo), len(have),
                f", {n_skipped_tried} previously empty — use --retry-missing"
                if n_skipped_tried else "")

    # A source call usually carries a fixed per-call cost (reading the schedule),
    # so read the whole backlog in ONE batch (mirroring ``backfill --force``); a
    # small incremental top-up keeps its modest batch so a partial save survives
    # an interruption.
    if only_games is None:
        batch_size = max(len(todo), 1)

    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        logger.info("Event ingest: reading matches %d-%d of %d",
                    start + 1, start + len(batch), len(todo))
        new = source.read_match_events(batch, seasons, leagues)
        got: set[str] = set()
        if not new.empty:
            # Prefer the source's own game_id — mapping via the "game" description
            # string silently drops matches whose events' string doesn't exactly
            # match the schedule's. Fall back to that string only when no game_id
            # was provided.
            if "game_id" in new.columns and new["game_id"].notna().any():
                new["game_id"] = new["game_id"].astype("string")
                new["date"] = new["game_id"].map(lambda g: date_by_gid.get(str(g)))
            else:
                new["game_id"] = new["game"].map(
                    lambda g: date_by_game.get(str(g), (None, None))[0])
                new["date"] = new["game"].map(
                    lambda g: date_by_game.get(str(g), (None, None))[1])
            got = set(new["game_id"].dropna().astype(str))
            events = store.merge_events(events, new)
            store.save_events(events)
        empty = [g for g in batch if g not in got]
        if empty:
            logger.info("Event ingest: %d/%d matches returned no events. "
                        "Recorded so they aren't "
                        "re-read; `refresh-events --retry-missing` tries again.",
                        len(empty), len(batch))

    # Remember every match tried in the full backfill, so tried-and-empty ones
    # aren't re-fetched on every future run.
    if only_games is None and todo:
        meta.events_attempted_game_ids |= set(todo)
        meta.save()
    return events


def _cache_schedule(schedule: pd.DataFrame, today: date | None = None) -> None:
    """Write ``schedule`` to today's day-stamped cache (best-effort) and prune old
    ones, so a report run the same day reuses it instead of re-reading the source."""
    from . import fixtures

    day = (today or fixtures.uk_today()).isoformat()
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = config.schedule_cache_path(day)
        schedule.to_parquet(path, index=False)
        for old in config.DATA_DIR.glob("schedule-*.parquet"):
            if old != path:
                try:
                    old.unlink()
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("Could not cache schedule: %s", exc)


def incremental_update(
    seasons: list[str] | None = None, today: date | None = None, schedule=None
) -> pd.DataFrame:
    """Ingest matches played since the last run; return the updated store.

    ``schedule`` lets a caller pass an already-loaded schedule (e.g. today's
    day-cache) so the schedule isn't read again on a repeat run.
    When omitted, a fresh schedule is read and published to today's cache.
    """
    seasons = seasons or config.baseline_seasons(today)
    if schedule is None:
        schedule = source.read_schedule(seasons)
        # Publish this fresh schedule to today's cache so the report that follows
        # reuses it instead of re-parsing (~2 min) the same data again.
        _cache_schedule(schedule, today)
    meta = store.Meta.load()
    appearances = store.load_appearances()

    todo = [g for g in _played_game_ids(schedule) if g not in meta.ingested_game_ids]
    if not todo:
        logger.info("Incremental update: no new played matches.")
        meta.save()
        _top_up_recent_events(schedule, seasons, today)
        return appearances

    logger.info("Incremental update: ingesting %d newly-played matches.", len(todo))
    new = source.read_match_appearances(todo, seasons)
    new = _attach_dates(new, schedule)
    appearances = store.merge_appearances(appearances, new)
    meta.ingested_game_ids.update(todo)
    store.save_appearances(appearances)
    meta.save()

    # Keep the card/substitution event store current for the same matches, so
    # the game log shows the exact booking minute without a
    # separate refresh-events run. Best-effort: never fail the appearance update.
    _top_up_recent_events(schedule, seasons, today, extra=todo)
    return appearances


def _top_up_recent_events(schedule: pd.DataFrame, seasons: list[str], today: date | None,
                          extra: list[str] | None = None, days: int = 14) -> None:
    """Fetch card/sub events for recently played matches the event store lacks.

    Covers the matches just ingested (``extra``) plus any played in the last
    ``days`` whose events are still missing — e.g. a top-up that failed, or a
    read before the source had posted the events — so ``assess`` already has
    who-replaced-whom instead of fetching it on the spot. Best-effort: never
    fails the appearance update.
    """
    try:
        from . import fixtures

        day = today or fixtures.uk_today()
        events = store.load_events()
        have = set(events["game_id"].dropna().astype(str)) if not events.empty else set()
        played = schedule[schedule["played"]].dropna(subset=["game_id"])
        dates = pd.to_datetime(played["date"], errors="coerce").dt.date
        recent = played[dates >= day - timedelta(days=days)]["game_id"].astype(str)
        want = list(dict.fromkeys(list(extra or []) + list(recent)))
        missing = [g for g in want if g not in have]
        if missing:
            logger.info("Event top-up: %d recent match(es) without sub/card events.",
                        len(missing))
            ingest_events(seasons=seasons, only_games=missing, schedule=schedule)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Event top-up failed: %s", exc)
