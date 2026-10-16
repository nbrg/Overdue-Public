"""Command-line entry points.

    overdue backfill              # one-off: build the baseline history store
    overdue run-daily            # daily: incremental update + render + notify
    overdue report               # catch up, then rebuild today's report
    overdue report-week          # overdue tables for each remaining day of the week
    overdue report-late          # today's benches, ranked by cameo booking rate
    overdue report-team <team>   # full card watch for one game's two squads (every player)
    overdue refresh-odds --paste # paste bookmaker card prices → Best Price / Edge columns
    overdue assess               # score the model's card predictions for a past day

``run-daily`` can self-guard on the Europe/London hour (``--require-hour``) for a
scheduler that fires more than once.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta

from . import (
    config,
    source,
    fixtures as fixtures_mod,
    ingest,
    notify,
    render_html,
    render_text,
    store,
)
from .report import build_report


class _Timer:
    """Lightweight stage timer so a report shows where the time goes.

    ``mark`` prints a timestamped line with the time since the previous mark and
    the running total, so the slow phase (schedule / ingest / line-ups / odds /
    render) is obvious when tuning.
    """

    def __init__(self) -> None:
        self.t0 = time.time()
        self.last = self.t0

    def mark(self, label: str) -> None:
        now = time.time()
        print(f"[{datetime.now():%H:%M:%S}] {label} "
              f"(+{now - self.last:.1f}s · {now - self.t0:.1f}s total)")
        self.last = now


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _tracked_domestic_teams() -> set[str]:
    """Clubs that play in a tracked domestic league (EPL/La Liga/Ligue 1).

    These are the only clubs whose players we score, so a fixture with neither
    club here (e.g. an FA Cup non-league tie) has nothing for us.
    """
    try:
        apps = store.load_appearances()
    except Exception:  # noqa: BLE001
        return set()
    if apps.empty:
        return set()
    return set(apps.loc[apps["league"].isin(config.DOMESTIC_LEAGUE_IDS), "team"]
               .dropna().astype(str))


def _drop_finished_fixtures(fixtures, grace_hours: int = 2):
    """Drop fixtures that kicked off more than ``grace_hours`` ago (already over).

    Returns ``(kept, n_dropped)``. A match-day report is a pre-match tool — a game
    that has effectively finished can't be bet on and shouldn't be flagged. A
    fixture with an unknown kick-off is kept (can't tell).
    """
    import pandas as pd

    if fixtures is None or getattr(fixtures, "empty", True):
        return fixtures, 0
    now = fixtures_mod.uk_now().replace(tzinfo=None)
    st = (pd.to_datetime(fixtures["start_time"], errors="coerce")
          if "start_time" in fixtures.columns else pd.Series(pd.NaT, index=fixtures.index))
    finished = st.notna() & (st < now - timedelta(hours=grace_hours))
    return fixtures[~finished].reset_index(drop=True), int(finished.sum())


def _only_tracked_fixtures(today_fixtures, teams: set[str]):
    """Drop fixtures where neither side is a tracked club; return (kept, n_skipped).

    Matches with the whole-word :func:`teamnames.same_club` (not exact equality),
    so a schedule short name ("Tottenham") still matches a store club
    ("Tottenham Hotspur") — otherwise those fixtures would be wrongly skipped.
    """
    if not teams or today_fixtures is None or today_fixtures.empty:
        return today_fixtures, 0
    from .teamnames import same_club

    names = list(teams)

    def _hit(name: str) -> bool:
        return name in teams or any(same_club(name, t) for t in names)

    mask = today_fixtures.apply(
        lambda r: _hit(str(r["home_team"])) or _hit(str(r["away_team"])), axis=1)
    return today_fixtures[mask].reset_index(drop=True), int((~mask).sum())


def _cmd_backfill(args: argparse.Namespace) -> int:
    leagues = _split_csv(getattr(args, "leagues", None))
    seasons = _split_csv(getattr(args, "seasons", None))
    ingest.backfill(seasons=seasons, leagues=leagues,
                    batch_size=getattr(args, "batch_size", None),
                    force=getattr(args, "force", False))
    print(f"Backfill complete. Store: {config.APPEARANCES_PATH}")
    return 0


def _print_referee_coverage(fixtures, appointments: list[dict], label: str = "today") -> None:
    """Say how many of ``label``'s games have a referee, listing found and missing."""
    from . import appointments as appt_mod

    found, missing = appt_mod.coverage(fixtures, appointments)
    total = len(found) + len(missing)
    if not total:
        return
    print(f"Referees: found {len(found)} of {total} game(s) {label}.")
    for home, away, ref in found:
        print(f"  ✓ {home} v {away}: {ref}")
    for home, away in missing:
        print(f"  ✗ {home} v {away}: no referee appointment found")


def _cmd_refresh_odds(args: argparse.Namespace) -> int:
    """Paste 'to be carded' prices ('<Player> 11/5' lines) into today's odds cache."""
    from . import odds_book

    book = getattr(args, "book", None) or "Bet365"
    print(f"Paste card prices as '<Player> <odds>' lines (odds = 11/5 or 3.2), "
          f"book = {book}.\nEnd with Ctrl-Z then Enter (Windows) / Ctrl-D (Mac/Linux):\n")
    text = sys.stdin.read()
    parsed = odds_book.parse_pasted_odds(text, book=book)
    merged = odds_book.merge_pasted_odds(parsed, fixtures_mod.uk_today())
    print(f"\nParsed {len(parsed)} price(s); {len(merged.get('players', {}))} cached "
          f"for today → {odds_book.ODDS_PATH}. Re-run a report to see Edge fill in.")
    return 0


def _attach_todays_odds(rows: list) -> int:
    """Attach today's cached bookie odds to report/target rows (for the Edge column)."""
    from . import odds_book

    idx = odds_book.odds_index_for(fixtures_mod.uk_today())
    return odds_book.attach_odds(rows, idx)


def _print_missing_positions(rows: list) -> None:
    """Flag report players with no cached position (fix: refresh-positions)."""
    missing = [r for r in rows if not getattr(r, "position", "")]
    if not missing:
        return
    by_comp: dict[str, int] = {}
    for r in missing:
        comp = getattr(r, "league_short", "") or "?"
        by_comp[comp] = by_comp.get(comp, 0) + 1
    print(f"{len(missing)} of {len(rows)} listed player(s) have no position ("
          + ", ".join(f"{c}: {n}" for c, n in sorted(by_comp.items()))
          + "). Run refresh-positions to fetch them.")


def _print_unpriced_hints(rows: list) -> None:
    """Flag shortlisted players whose game was priced but whose name didn't match."""
    from . import odds_book

    idx = odds_book.odds_index_for(fixtures_mod.uk_today())
    hints = odds_book.unpriced_hints(rows, idx)
    if not hints:
        return
    print("No odds matched for these players (name differs at the bookmaker?). If one "
          "of the unmatched names is them, add it to data/player_aliases.json, e.g. "
          '{"Alexsandro Ribeiro": "Victor"}:')
    for line in hints:
        print("  " + line)


def _todays_lineup_status():
    """Load today's confirmed-XI status map (empty if none stored for today)."""
    from . import lineups as lu

    lineups = lu.load_lineups_for(fixtures_mod.uk_today())
    return lu.status_index(lineups)


def _load_positions_warned() -> dict[str, str]:
    """Load the cached positions, warning (consistently, in every report) if the
    cache is empty — without it goalkeepers can't be excluded."""
    positions = store.load_positions()
    if not positions:
        print("Note: positions/goalkeeper exclusion not available yet — "
              "run `overdue refresh-positions` once to enable it.")
    return positions


def _cmd_refresh_events(args: argparse.Namespace) -> int:
    """Ingest card + substitution events (with minutes) for stored matches."""
    league_ids = _resolve_league_ids(getattr(args, "leagues", None))
    leagues = sorted(league_ids) if league_ids is not None else None
    seasons = None
    spec = getattr(args, "seasons", None)
    if spec:
        seasons = [s.strip() for s in spec.split(",") if s.strip()]
    if leagues is not None or seasons is not None:
        print("Scoping event ingest to "
              + (", ".join(config.LEAGUE_BY_ID[i].short for i in leagues) if leagues else "all leagues")
              + (f" · seasons {', '.join(seasons)}" if seasons else "")
              + " — far fewer matches to fetch.")
    events = ingest.ingest_events(seasons=seasons, leagues=leagues,
                                  force=getattr(args, "force", False),
                                  retry_missing=getattr(args, "retry_missing", False))
    cards = events[events["kind"].isin(["yellow", "red", "second_yellow"])] if not events.empty else events
    n_cards = len(cards) if not events.empty else 0
    with_min = int((cards["minute"] > 0).sum()) if n_cards else 0
    print(f"Event store: {len(events)} events across {events['game_id'].nunique() if not events.empty else 0} "
          f"matches ({n_cards} cards, {with_min} with a minute) → {config.CARD_EVENTS_PATH}")
    return 0


def _cmd_refresh_positions(args: argparse.Namespace) -> int:
    """Fetch each player's position from the source and cache it."""
    per_league: dict = {}
    positions = source.read_player_positions(report=per_league)
    for lg_id, res in per_league.items():
        print(f"  {config.LEAGUE_BY_ID[lg_id].short}: "
              + (res if isinstance(res, str) else f"{res} player-season row(s)"))
    if not positions:
        print("No positions fetched; keeping the existing cache.")
        return 1
    # Merge over the cache so a league that failed this time keeps its old values.
    merged = {**store.load_positions(), **positions}
    store.save_positions(merged)
    gks = sum(1 for p in merged.values() if p.upper().startswith("GK"))
    print(f"Saved {len(merged)} player positions ({gks} goalkeepers) to "
          f"{config.POSITIONS_PATH}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    """Show why a given player is / isn't flagged today."""
    from .model import compute_player_metrics

    appearances = store.load_appearances()
    as_of = getattr(args, "as_of", None)
    if as_of:
        import pandas as pd
        cutoff = pd.Timestamp(date.fromisoformat(as_of))
        appearances = appearances[pd.to_datetime(appearances["date"], errors="coerce") < cutoff]
        print(f"(computing as of {as_of} — using only appearances before that date)\n")
    query = args.player.lower()

    raw = appearances[appearances["player"].str.lower().str.contains(query, na=False)]
    if raw.empty:
        print(f"No player matching '{args.player}' is in the store. Their league may not be "
              f"backfilled yet — run `backfill --leagues \"<their league id>\"`.")
        return 0

    metrics = [m for m in compute_player_metrics(appearances) if query in m.player.lower()]

    # Best-effort: does their team have a fixture today?
    playing: set[str] = set()
    try:
        seasons = config.baseline_seasons()
        today = fixtures_mod.uk_today()
        playing = fixtures_mod.teams_playing(source.fixtures_on(today, seasons))
    except Exception as exc:  # noqa: BLE001
        print(f"(couldn't fetch today's fixtures: {exc})\n")

    if not metrics:
        n = raw["player"].nunique()
        print(f"Found {len(raw)} appearance row(s) for {n} matching player(s) in the store, "
              f"but none cleared the sample gate (≥{config.MODEL.min_appearances} apps, "
              f"≥{config.MODEL.min_cards} cards).")
        return 0

    import pandas as pd
    from .model import is_started_value

    for m in metrics:
        last_app = m.last_appearance_date
        plays = m.team in playing if playing else "unknown (no fixtures fetched)"
        print(
            f"\n{m.player}  ({m.team}, {m.league})\n"
            f"  appearances={m.appearances}  cards={m.cards}  "
            f"rate={m.rate:.2f} (1 per {m.expected_interval:.1f})\n"
            f"  streak={m.streak}  overdue_ratio={m.overdue_ratio:.2f}  "
            f"(threshold {config.MODEL.overdue_threshold})\n"
            f"  last_appearance={last_app}  start_share={m.start_share:.0%}  "
            f"recent_starts={m.recent_starts}  recent_dnp={m.recent_dnp}\n"
            f"  qualifies={m.qualifies}  overdue={m.overdue}  plays_today={plays}"
        )
        # Recent appearances so you can see exactly when they were last booked.
        pg = appearances[appearances["player"] == m.player].copy()
        pg["date"] = pd.to_datetime(pg["date"], errors="coerce")
        pg = pg[pg["minutes"] >= 1].sort_values("date").tail(8)
        if not pg.empty:
            print("  recent:")
            for _, r in pg.iterrows():
                started = int(r["started"]) if "started" in r and pd.notna(r["started"]) else -1
                role = "Start" if is_started_value(started, int(r["minutes"]), config.MODEL) else "Sub"
                card = ("Y+R" if r["yellow"] and r["red"] else "R" if r["red"]
                        else "Y" if r["yellow"] else "—")
                d = r["date"].date() if pd.notna(r["date"]) else "?"
                # Show the fixture (the "game" string, e.g. "2026-05-24 Chelsea-Arsenal").
                game = str(r.get("game") or "").strip()
                game = game.split(" ", 1)[1] if " " in game else game
                print(f"    {d}  {str(r['league'])[:16]:16} {role:5} {int(r['minutes']):>3}'  "
                      f"{card:3}  {game}")
    return 0


def _resolve_league_ids(spec: str | None) -> set[str] | None:
    """Parse a ``--leagues`` spec into league ids, or None for all.

    Accepts short labels, names or ids, comma-separated and case-insensitive:
    ``"EPL"``, ``"Premier League"``, ``"premier,championship"``,
    ``"ENG-Premier League"`` all resolve.
    """
    if not spec:
        return None
    tokens = [t.strip().lower() for t in spec.split(",") if t.strip()]
    ids: set[str] = set()
    for lg in config.LEAGUES:
        keys = {lg.league_id.lower(), lg.name.lower(), lg.short.lower()}
        if any(tok in keys or tok in lg.name.lower() or tok in lg.league_id.lower()
               for tok in tokens):
            ids.add(lg.league_id)
    if not ids:
        print(f"Warning: no tracked league matched '{spec}'. Known: "
              + ", ".join(lg.short for lg in config.LEAGUES) + ". Showing all.")
        return None
    return ids


def _teams_in_leagues(appearances, league_ids: set[str]) -> set[str]:
    """Store team names that appear in any of ``league_ids``."""
    if appearances is None or appearances.empty or "league" not in appearances.columns:
        return set()
    return set(appearances.loc[appearances["league"].isin(league_ids), "team"]
               .dropna().astype(str))


def _build_and_deliver(args: argparse.Namespace, appearances, timer: "_Timer | None" = None) -> int:
    timer = timer or _Timer()
    today = fixtures_mod.uk_today()
    # --tomorrow builds the next day's report (no confirmed XIs exist yet then).
    is_today = not getattr(args, "tomorrow", False)
    target_day = today if is_today else today + timedelta(days=1)
    if not is_today:
        print(f"Building tomorrow's report ({target_day.isoformat()}); line-ups skipped.")
    schedule = _schedule_best_effort(config.baseline_seasons())
    timer.mark("Schedule fetched")
    try:
        today_fixtures = source.fixtures_from_schedule(schedule, target_day) \
            if schedule is not None else _empty_fixtures()
    except Exception as exc:  # noqa: BLE001 - fixtures fetch is best-effort
        logging.getLogger(__name__).warning("Could not derive fixtures: %s", exc)
        today_fixtures = _empty_fixtures()

    # A pre-match report disregards games that already finished today (kicked off
    # more than 2 hours ago) — you can't bet a game that's over.
    finished = 0
    if is_today:
        today_fixtures, finished = _drop_finished_fixtures(today_fixtures)
    if finished:
        print(f"Disregarding {finished} of today's game(s) that kicked off more than "
              "2 hours ago (already finished).")

    # --leagues scopes the whole report to specific competitions' games today.
    league_ids = _resolve_league_ids(getattr(args, "leagues", None))
    if league_ids is not None and "league" in today_fixtures.columns:
        today_fixtures = today_fixtures[today_fixtures["league"].isin(league_ids)] \
            .reset_index(drop=True)
        print("Scoping to league(s): "
              + ", ".join(sorted(config.LEAGUE_BY_ID[i].short for i in league_ids)))

    positions = _load_positions_warned()
    from . import appointments as appt_mod
    from .model import compute_player_metrics
    appointments = appt_mod.load_appointments()
    _print_referee_coverage(today_fixtures, appointments,
                            "today" if is_today else "tomorrow")
    # The per-player metrics are the same for every build off this store — compute
    # once and share across the pre-build, the rebuild and the team explorer.
    metrics = compute_player_metrics(appearances, config.MODEL)
    timer.mark("Player metrics computed")

    def _build(status):
        return build_report(appearances, today_fixtures, target_day, schedule=schedule,
                            positions=positions, appointments=appointments,
                            lineup_status=status, metrics=metrics)

    # Tomorrow has no confirmed XIs to filter by; today uses any stored line-ups.
    report = _build(_todays_lineup_status() if is_today else None)
    timer.mark(f"Overdue candidates computed ({len(report.rows)})")

    _attach_todays_odds(report.rows)
    _print_unpriced_hints(report.rows)
    _print_missing_positions(report.rows)
    timer.mark("Odds priced")

    md_path = render_text.render_markdown(report, target_day)
    print(render_text.terminal_summary(report))
    print(f"\nReport written to {md_path}")

    from . import webview, targets as targets_mod
    next_by_team = targets_mod.team_next_fixtures(schedule, target_day)
    logs = targets_mod.game_logs_for(appearances, report.rows, next_by_team=next_by_team)
    team_players = targets_mod.team_player_rows(
        appearances, positions=positions, next_by_team=next_by_team, metrics=metrics)
    if league_ids is not None:  # scope the explorer to the chosen leagues too
        allowed = _teams_in_leagues(appearances, league_ids)
        team_players = {t: rws for t, rws in team_players.items() if t in allowed}
    _attach_todays_odds([r for rws in team_players.values() for r in rws])
    team_logs = targets_mod.game_logs_for(
        appearances, [r for rws in team_players.values() for r in rws],
        next_by_team=next_by_team)
    html_path = webview.render_report_html(report, logs, target_day,
                                           team_players=team_players, team_logs=team_logs)
    print(f"Interactive HTML written to {html_path}")
    timer.mark("HTML rendered")

    # Optional extras (off by default).
    if getattr(args, "html", False):
        legacy = render_html.render(report)
        print(f"Static site page written to {legacy}")
    if getattr(args, "telegram", False):
        notify.send(report, site_url=args.site_url, dry_run=args.dry_run)
    return 0


def _empty_fixtures():
    import pandas as pd

    return pd.DataFrame(
        columns=["league", "season", "game", "game_id", "date", "home_team", "away_team",
                 "notes", "played"]
    )


def _hour_guard(require_hour: int | None, force: bool) -> bool:
    """Return True if we should proceed given the Europe/London hour guard."""
    if force or require_hour is None:
        return True
    now_uk = datetime.now(config.UK_TZ)
    if now_uk.hour != require_hour:
        print(f"Skipping: Europe/London hour is {now_uk.hour}, waiting for {require_hour}.")
        return False
    return True


def _cmd_run_daily(args: argparse.Namespace) -> int:
    if not _hour_guard(args.require_hour, args.force):
        return 0

    if args.no_ingest:
        appearances = store.load_appearances()
    else:
        appearances = ingest.incremental_update()
    return _build_and_deliver(args, appearances)


def _warn_if_stale(appearances, target_day: date, max_age_days: int = 4) -> None:
    """Warn when the store hasn't been updated recently (``report`` doesn't ingest).

    ``report`` only re-renders the committed store — it never fetches new matches.
    If the newest appearance is well in the past, cards from intervening games are
    missing from every streak/rate, so nudge the user to run an ingest.
    """
    import pandas as pd

    if appearances.empty:
        print("Note: the appearance store is empty — run `backfill` first.")
        return
    latest = pd.to_datetime(appearances["date"], errors="coerce").max()
    if pd.isna(latest):
        return
    age = (pd.Timestamp(target_day) - latest.normalize()).days
    if age > max_age_days:
        print(
            f"Note: newest match in the store is {latest.date()} ({age} days ago). "
            f"`report` does not ingest — run `run-daily --force` (or `backfill`) to "
            f"pull the matches played since then before trusting these streaks.\n"
        )


def _note_if_events_behind(appearances) -> None:
    """Nudge a one-off ``refresh-events`` when the card-minute store is far behind.

    Card minutes in the game log come from the event store, which only
    ``refresh-events`` (and the daily catch-up, for new matches) populates. If it
    covers far fewer matches than the appearance store, the log falls back to the
    Y/R letter — so point the user at the one command that backfills it.
    """
    try:
        events = store.load_events()
    except Exception:  # noqa: BLE001
        return
    if appearances.empty:
        return
    # Measure coverage within the SAME scope refresh-events ingests (the baseline
    # seasons) — not the whole store. The store can span more seasons than that
    # (an extra backfilled season, cups), whose matches events never target, so
    # counting them made the note nag forever at ~50% even when fully caught up.
    baseline = set(config.baseline_seasons())
    if "season" in appearances.columns:
        app_ids = set(appearances.loc[appearances["season"].astype(str).isin(baseline),
                                      "game_id"].dropna().astype(str))
    else:
        app_ids = set(appearances["game_id"].dropna().astype(str))
    ev_ids = set(events["game_id"].dropna().astype(str)) if not events.empty else set()
    # Don't count matches already tried that returned nothing (gaps in the source)
    # — refresh-events can't fix those, so they shouldn't nag forever.
    tried_empty = store.Meta.load().events_attempted_game_ids - ev_ids
    app_ids -= tried_empty
    n_app = len(app_ids)
    n_ev = len(app_ids & ev_ids)
    if n_app and n_ev < 0.5 * n_app:
        print(f"Note: exact booking minutes cover {n_ev}/{n_app} recent-season matches. "
              f"Run `overdue refresh-events` once to show the card minute in the game log.")


def _appearances_caught_up(args: argparse.Namespace):
    """Load appearances, first ingesting any matches played since the last run.

    The daily report commands (``report``/``report-week``) top up the store
    automatically so a streak is never computed on stale data. ``--no-ingest``
    skips the network step; a failed fetch degrades gracefully to the stored data
    with a warning.
    """
    if getattr(args, "no_ingest", False):
        appearances = store.load_appearances()
        _warn_if_stale(appearances, fixtures_mod.uk_today())
        _note_if_events_behind(appearances)
        return appearances
    try:
        print("Catching up on matches played since the last run…")
        # Reuse today's cached schedule (same one the report renders from) so the
        # catch-up doesn't re-parse the whole multi-league schedule (~2 min) on a
        # repeat run. The first run of the day still reads it fresh and caches it.
        schedule = _schedule_best_effort(config.baseline_seasons())
        appearances = ingest.incremental_update(schedule=schedule)
        _note_if_events_behind(appearances)
        return appearances
    except Exception as exc:  # noqa: BLE001 - ingest is best-effort here
        logging.getLogger(__name__).warning("Catch-up ingest failed: %s", exc)
        print("Note: couldn't fetch new matches (network?) — using the stored data as-is.")
        appearances = store.load_appearances()
        _warn_if_stale(appearances, fixtures_mod.uk_today())
        _note_if_events_behind(appearances)
        return appearances


def _appearances_stored_only():
    """Load the stored appearances WITHOUT any catch-up ingest (report-late /
    report-team, which are research/pre-match views over the committed store)."""
    appearances = store.load_appearances()
    _warn_if_stale(appearances, fixtures_mod.uk_today())
    _note_if_events_behind(appearances)
    return appearances


def _cmd_report(args: argparse.Namespace) -> int:
    timer = _Timer()
    appearances = _appearances_caught_up(args)
    timer.mark("Store loaded / caught up")
    return _build_and_deliver(args, appearances, timer)


def _week_days(start: date, days: int | None) -> list[date]:
    """Days from ``start`` through the upcoming Sunday, or ``days`` days total."""
    if days is not None and days > 0:
        span = days
    else:
        span = (6 - start.weekday()) + 1  # Monday=0 … Sunday=6, inclusive of Sunday
    return [start + timedelta(days=i) for i in range(span)]


def _cmd_report_week(args: argparse.Namespace) -> int:
    """Build overdue tables for each remaining day of the week, in day order."""
    timer = _Timer()
    appearances = _appearances_caught_up(args)
    timer.mark("Store loaded / caught up")
    schedule = _schedule_best_effort(config.baseline_seasons())
    timer.mark("Schedule fetched")

    positions = _load_positions_warned()
    from . import appointments as appt_mod
    from .model import compute_player_metrics
    appointments = appt_mod.load_appointments()
    metrics = compute_player_metrics(appearances, config.MODEL)  # once for every day
    timer.mark("Player metrics computed")

    start = fixtures_mod.uk_today()
    days = _week_days(start, getattr(args, "days", None))
    day_fx = {day: (source.fixtures_from_schedule(schedule, day)
                    if schedule is not None else _empty_fixtures()) for day in days}
    # Today's slice must drop games that already finished (you can't bet a game
    # that's over). Later days are all in the future, so nothing to drop there.
    day_fx[start], finished = _drop_finished_fixtures(day_fx[start])
    if finished:
        print(f"Disregarding {finished} of today's game(s) that already finished.")
    _print_referee_coverage(day_fx[start], appointments)

    def _build(day, status):
        return build_report(appearances, day_fx[day], day, schedule=schedule,
                            positions=positions, appointments=appointments,
                            lineup_status=status, metrics=metrics)

    # Only today's table is line-up-filtered (confirmed XIs don't exist yet for
    # later days).
    day_reports = [(day, _build(day, _todays_lineup_status() if day == start else None))
                   for day in days]
    timer.mark("Overdue candidates computed")

    for _, rep in day_reports:
        _attach_todays_odds(rep.rows)
    _print_unpriced_hints([r for _, rep in day_reports for r in rep.rows])
    timer.mark("Odds priced")

    md_path = render_text.render_week_markdown(day_reports, start)
    print(render_text.week_terminal_summary(day_reports))
    print(f"\nWeek report written to {md_path}")

    from . import webview, targets as targets_mod
    all_rows = [row for _, rep in day_reports for row in rep.rows]
    next_by_team = targets_mod.team_next_fixtures(schedule, start)
    logs = targets_mod.game_logs_for(appearances, all_rows, next_by_team=next_by_team)
    team_players = targets_mod.team_player_rows(
        appearances, positions=positions, next_by_team=next_by_team, metrics=metrics)
    _attach_todays_odds([r for rws in team_players.values() for r in rws])
    team_logs = targets_mod.game_logs_for(
        appearances, [r for rws in team_players.values() for r in rws],
        next_by_team=next_by_team)
    html_path = webview.render_week_html(day_reports, start, logs,
                                         team_players=team_players, team_logs=team_logs)
    print(f"Interactive HTML written to {html_path}")
    timer.mark("HTML rendered")
    return 0


def _cmd_assess(args: argparse.Namespace) -> int:
    """Score the model's card predictions for a past day (default: yesterday)."""
    from . import assessment, odds_book

    if getattr(args, "date", None):
        target = date.fromisoformat(args.date)
    else:
        target = fixtures_mod.uk_today() - timedelta(days=1)

    appearances = _appearances_caught_up(args)  # pull last night's results first
    if _day_rows_empty(appearances, target):
        print(f"No matches for {target} are in the store — nothing played that day, or the "
              f"catch-up ingest couldn't reach the source (try again, or pass a --date with games).")
        return 0

    schedule = _schedule_best_effort(config.baseline_seasons())
    positions = store.load_positions()
    try:
        events = store.load_events()
    except Exception:  # noqa: BLE001 — super-sub credit is best-effort
        events = None
    if not getattr(args, "no_ingest", False):
        events = _top_up_day_events(appearances, events, target, schedule)
    try:
        odds_index = odds_book.odds_index_for(target, history=True)
    except Exception:  # noqa: BLE001 — odds are optional
        odds_index = {}
    try:
        # Fallback: the prices shown on that day's saved HTML report, for days
        # whose odds.json was since replaced by a later day's prices.
        report_odds = odds_book.odds_from_report_html(target)
    except Exception:  # noqa: BLE001 — odds are optional
        report_odds = {}
    result = assessment.assess_day(appearances, schedule, target, positions, events=events,
                                   odds_index=odds_index, report_odds=report_odds)
    print(assessment.summary_text(result))
    if getattr(args, "explain_subs", False):
        print(assessment.explain_subs(appearances, events, result))
    log_path = assessment.append_log(result)
    print(f"\nLogged to {log_path} for later tuning.")
    return 0


def _top_up_day_events(appearances, events, target, schedule):
    """Fetch the day's card/sub events for any of its matches the event store
    lacks, so ``assess`` knows exactly who replaced whom (minutes alone can't
    split two changes made a minute apart). Best-effort: returns what it has."""
    import pandas as pd

    dates = pd.to_datetime(appearances["date"], errors="coerce")
    day_ids = set(appearances.loc[dates.dt.date == target, "game_id"].dropna().astype(str))
    have = set() if events is None or events.empty else set(
        events["game_id"].dropna().astype(str))
    missing = sorted(day_ids - have)
    if not missing:
        return events
    print(f"Fetching substitution events for {len(missing)} match(es) on {target}…")
    try:
        events = ingest.ingest_events(only_games=missing, schedule=schedule)
        got = set(events["game_id"].dropna().astype(str)) & set(missing)
        print(f"  got events for {len(got)} of {len(missing)}"
              + ("" if len(got) == len(missing) else
                 " (the source may not have them yet; the rest fall back to minutes)"))
    except Exception as exc:  # noqa: BLE001
        print(f"  couldn't fetch events ({exc}); falling back to minutes.")
    return events


def _day_rows_empty(appearances, target) -> bool:
    import pandas as pd

    if appearances.empty:
        return True
    dates = pd.to_datetime(appearances["date"], errors="coerce")
    return not (dates.dt.date == target).any()


def _team_next_fixture_row(schedule, team: str, today):
    """Return ``(home, away, league_id, days_until)`` for ``team``'s next fixture.

    Picks the earliest unplayed, not-called-off, not-yet-finished game on/after
    ``today`` — so it naturally spans today, tomorrow, or a few days out — and
    reports how many days away it is so the caller can honour a "next few days"
    window. A game that already kicked off more than two hours ago is treated as
    over, so a team whose match finished today rolls to its genuine next fixture.
    ``None`` if the team has no upcoming fixture in the schedule.
    """
    import pandas as pd

    if schedule is None or getattr(schedule, "empty", True):
        return None
    from .teamnames import same_team

    df = schedule[schedule["date"].notna()]
    up = df[df["date"].dt.date >= today]
    if "played" in up.columns:
        up = up[~up["played"].fillna(False)]
    if "notes" in up.columns:
        from .targets import _NOT_HAPPENING
        up = up[~up["notes"].fillna("").str.contains(
            _NOT_HAPPENING, case=False, regex=True)]
    if up.empty:
        return None
    # The schedule spells clubs shorter than the store ("Tottenham" vs "Tottenham
    # Hotspur", "Manchester Utd" vs "Manchester United"), so match by canonical
    # club identity, not string equality — otherwise the fixture is invisible.
    # Build a plain bool mask (avoids arrow-dtype `|` pitfalls on empty columns).
    keep = [same_team(str(h), team) or same_team(str(a), team)
            for h, a in zip(up["home_team"], up["away_team"])]
    up = up[pd.Series(keep, index=up.index)].sort_values("date")
    if up.empty:
        return None
    up, _ = _drop_finished_fixtures(up)  # skip a finished today game
    if up.empty:
        return None
    r = up.iloc[0]
    days = (pd.Timestamp(r["date"]).date() - today).days
    return str(r["home_team"]), str(r["away_team"]), str(r["league"]), days


def _resolve_team_key(name: str, teams) -> str:
    """Best match for a schedule team name among the store's team keys (or "").

    Schedule and appearance spellings can differ ("Brighton" vs "Brighton & Hove
    Albion"), so fall back to an accent/punctuation-insensitive exact then
    substring match — otherwise the opponent's squad silently goes missing.
    """
    if not name:
        return ""
    if name in teams:
        return name
    from .teamnames import resolve

    return resolve(name, teams)


def _fixture_entry_matches(fx: dict, home: str, away: str) -> bool:
    """Does a stored line-up entry describe this fixture (either orientation)?"""
    from .teamnames import same_club

    fh, fa = str(fx.get("home", "")), str(fx.get("away", ""))
    return ((same_club(fh, home) and same_club(fa, away))
            or (same_club(fh, away) and same_club(fa, home)))


def _confirmed_fixture_status(home: str, away: str) -> tuple[dict, bool]:
    """Confirmed-XI status for one fixture from today's stored line-ups.

    Returns ``(status_index, confirmed)``; ``({}, False)`` when today's line-up
    file has no confirmed XI for this game.
    """
    from . import lineups as lu

    today = fixtures_mod.uk_today()
    cached = lu.load_lineups_for(today)
    for fx in cached.get("fixtures", []):
        if _fixture_entry_matches(fx, home, away) and fx.get("confirmed"):
            return lu.status_index({"date": today.isoformat(), "fixtures": [fx]}), True
    return {}, False


def _fixture_lineup_status(home: str, away: str) -> dict[str, str]:
    """Injury/line-up status for a single fixture from today's stored line-ups."""
    from . import lineups as lu

    today = fixtures_mod.uk_today()
    fixtures = [fx for fx in lu.load_lineups_for(today).get("fixtures", [])
                if _fixture_entry_matches(fx, home, away)]
    return lu.status_index({"date": today.isoformat(), "fixtures": fixtures})


def _fixture_notes(schedule, appearances, home: str, away: str) -> list[str]:
    """Text lines for above the report-team table: the fixture's head-to-head card
    average and the appointed referee's cards/game (both best-effort)."""
    from . import h2h as h2h_mod, appointments as appt_mod

    notes: list[str] = []
    try:
        meetings, avg = h2h_mod.h2h_card_average(schedule, appearances, home, away)
    except Exception:  # noqa: BLE001
        meetings, avg = 0, None
    if avg is not None:
        notes.append(f"H2H: {avg:.1f} cards/game over {meetings} past meeting"
                     f"{'s' if meetings != 1 else ''}")
    else:
        notes.append("H2H: no prior meetings in the store")
    try:
        ref, ref_cards = appt_mod.referee_for(appt_mod.load_appointments(), home, away)
    except Exception:  # noqa: BLE001
        ref, ref_cards = "", None
    if ref:
        notes.append(f"Referee: {ref}"
                     + (f" — {ref_cards:.1f} cards/game" if ref_cards is not None else ""))
    else:
        notes.append("Referee: not yet appointed")
    return notes


def _cmd_report_team(args: argparse.Namespace) -> int:
    """Full card watch for one game's two squads (every player, priced) — even if
    nobody is overdue. e.g. ``overdue report-team Arsenal``."""
    from . import targets as targets_mod, webview
    from .lineups import status_for

    timer = _Timer()
    appearances = _appearances_stored_only()  # no catch-up ingest for report-team
    timer.mark("Store loaded")
    positions = _load_positions_warned()
    target_day = fixtures_mod.uk_today()
    schedule = _schedule_best_effort(config.baseline_seasons())
    timer.mark("Schedule fetched")
    next_by_team = targets_mod.team_next_fixtures(schedule, target_day)
    all_teams = targets_mod.team_player_rows(
        appearances, positions=positions, next_by_team=next_by_team)

    q = args.team.strip().lower()
    matches = [t for t in all_teams if q in t.lower()]
    if not matches:
        print(f"No tracked team matches '{args.team}'. Try one of: "
              + ", ".join(sorted(all_teams)[:12]) + " …")
        return 1
    team = min(matches, key=len)

    row = _team_next_fixture_row(schedule, team, target_day)
    opponent = ""
    status: dict[str, str] = {}
    notes: list[str] | None = None
    max_days = getattr(args, "days_ahead", 2)
    if row is None:
        print(f"No upcoming fixture found for {team}; showing its squad only.")
    else:
        home, away, _league_id, days = row
        from .teamnames import same_team
        opponent = away if same_team(home, team) else home
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        if days > max_days:
            # The next game is further out than the window — don't attach line-ups
            # or a referee to it, just show the squad. Widen with --days-ahead.
            print(f"{team}'s next game ({home} v {away}) is {when} — beyond the "
                  f"{max_days}-day window. Showing squad only; "
                  f"add --days-ahead {days} to price it.")
            opponent = ""  # squad reference only; don't imply a priced opponent
        else:
            print(f"{team}'s next game: {home} v {away} ({when}).")
            notes = _fixture_notes(schedule, appearances, home, away)
            # Only this one game's line-up/injuries — not the whole day's slate.
            status = _fixture_lineup_status(home, away)

    # Resolve both squads (opponent name may be spelled differently than the store).
    keys = [k for k in (team, _resolve_team_key(opponent, all_teams)) if k in all_teams]
    picked = {k: all_teams[k] for k in dict.fromkeys(keys)}
    # Drop players this fixture's data flags as out (injured/suspended).
    if status:
        picked = {t: [r for r in rws if status_for(status, r.player) != "out"]
                  for t, rws in picked.items()}
    flat = [r for rws in picked.values() for r in rws]
    _attach_todays_odds(flat)
    team_logs = targets_mod.game_logs_for(appearances, flat, next_by_team=next_by_team)
    title = f"{team}" + (f" v {opponent}" if opponent else "") + " — Card Watch"
    slug = re.sub(r"[^a-z0-9]+", "-", team.lower()).strip("-")
    html_path = webview.render_team_html(
        picked, target_day, title, team_logs=team_logs, notes=notes,
        out_name=f"team-{slug}-{target_day.isoformat()}.html")
    print(f"Interactive HTML written to {html_path}")
    timer.mark("HTML rendered")
    return 0


def _cmd_check_teams(args: argparse.Namespace) -> int:
    """Diagnose club-name mismatches between the schedule and the appearance store.

    Sources spell clubs differently (a schedule's "Tottenham" vs the store's
    "Tottenham Hotspur"), and a mismatch silently hides a fixture, squad or price.
    This lists every club in the upcoming domestic fixtures and whether it
    resolves to a store team — so a gap shows up here instead of as a broken
    report. Fix a MISS by adding the abbreviation to ``teamnames._WORD_ALIASES`` /
    ``_SHORT_FORMS`` (or backfilling the club if it's genuinely absent).
    """
    import pandas as pd

    from .teamnames import canonical, resolve

    appearances = store.load_appearances()
    col = appearances["team"] if "team" in appearances.columns else pd.Series(dtype=str)
    store_teams = sorted({str(t) for t in col.dropna().unique()})
    schedule = _schedule_best_effort(config.baseline_seasons())
    if schedule is None or getattr(schedule, "empty", True):
        print("No schedule available (run a report first to populate the day cache).")
        return 1

    today = fixtures_mod.uk_today()
    days = getattr(args, "days", 7) or 7
    horizon = today + timedelta(days=days)
    df = schedule[schedule["date"].notna()]
    up = df[(df["date"].dt.date >= today) & (df["date"].dt.date <= horizon)]
    if "played" in up.columns:
        up = up[~up["played"].fillna(False)]
    dom = up[up["league"].isin(config.DOMESTIC_LEAGUE_IDS)]  # domestic = player scope
    sched_teams = sorted({str(t) for c in ("home_team", "away_team")
                          for t in dom[c].dropna().unique()})

    print(f"Checking {len(sched_teams)} club(s) in the next {days} day(s) of domestic "
          f"fixtures against {len(store_teams)} store team(s):\n")
    misses: list[str] = []
    for name in sched_teams:
        key = name if name in store_teams else resolve(name, store_teams)
        if key:
            print(f"  OK   {name}" + ("" if key == name else f"  ->  {key}"))
        else:
            misses.append(name)
            print(f"  MISS {name}   (canonical={canonical(name)!r}; no store team matches)")
    if misses:
        print(f"\n{len(misses)} unmatched: " + ", ".join(misses))
        print("Add each short form to teamnames._WORD_ALIASES / _SHORT_FORMS, or "
              "backfill the club if it's genuinely missing from the store.")
        return 2
    print("\nAll upcoming clubs resolve to a store team.")
    return 0


def _schedule_best_effort(seasons: list[str]):
    """Fetch the multi-season schedule, cached for the calendar day.

    Reading the multi-league schedule can be slow with a real feed, but it barely
    changes intraday, so the first report of the day fetches and caches it to a
    date-stamped parquet and every later run that day loads it instantly. A failed
    fetch falls back to the most recent cache. ``run-daily``/ingest still call
    ``source.read_schedule`` directly, so they always see fresh played flags.
    Returns ``None`` only if there's no fetch and no cache to fall back to.
    """
    import pandas as pd

    today = fixtures_mod.uk_today().isoformat()
    cache = config.schedule_cache_path(today)
    if cache.exists():
        try:
            df = pd.read_parquet(cache)
            if "start_time" not in df.columns:
                # A cache written before kick-off times were added — refetch so the
                # match-day filters (drop finished games) have kick-off times.
                print(f"Cached schedule ({cache.name}) predates kick-off times — "
                      "refetching so finished games can be dropped.")
            else:
                print(f"Using today's cached schedule ({cache.name}); "
                      "delete it or run-daily to force a refresh.")
                return df
        except Exception as exc:  # noqa: BLE001 - fall through to a fresh fetch
            logging.getLogger(__name__).warning("Cached schedule unreadable: %s", exc)

    try:
        df = source.read_schedule(seasons)
    except Exception as exc:  # noqa: BLE001 - fixtures fetch is best-effort
        logging.getLogger(__name__).warning("Could not fetch schedule: %s", exc)
        prior = sorted(config.DATA_DIR.glob("schedule-*.parquet"))
        if prior:
            try:
                print(f"Schedule fetch failed; using {prior[-1].name} instead.")
                return pd.read_parquet(prior[-1])
            except Exception:  # noqa: BLE001
                pass
        return None

    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache, index=False)
        # Prune older day-stamped schedule caches so they don't accumulate.
        for old in config.DATA_DIR.glob("schedule-*.parquet"):
            if old != cache:
                try:
                    old.unlink()
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        logging.getLogger(__name__).warning("Could not cache schedule: %s", exc)
    return df


def _match_named_players(names: list[str], team_rows: list):
    """Resolve typed player names to this team's rows (surname / token match).

    Handles single-name entries ("Bergvall" → "Lucas Bergvall") by matching on
    the surname token, then on a full token-subset. Returns (rows, unmatched).
    """
    from .odds_book import _name_tokens

    matched, unmatched = [], []
    for name in names:
        toks = _name_tokens(name)
        if not toks:
            continue
        surname = toks[-1]
        cands = [r for r in team_rows
                 if _name_tokens(r.player) and _name_tokens(r.player)[-1] == surname]
        if len(cands) != 1:  # ambiguous or none → try a full token-subset match
            tset = set(toks)
            sub = [r for r in team_rows if tset <= set(_name_tokens(r.player))]
            cands = sub or cands
        if len(cands) >= 1:
            matched.append(cands[0])
        else:
            unmatched.append(name)
    # de-dupe preserving order
    seen, out = set(), []
    for r in matched:
        if r.player not in seen:
            seen.add(r.player)
            out.append(r)
    return out, unmatched


def _report_late_for_team(args: argparse.Namespace) -> int:
    """Late-card targets for ONE live/today fixture: the BENCH only.

    Two ways to say who's on the bench:
    * ``--bench "A, B, C"`` — you name them (works live, once the unused bench
      is no longer listed anywhere);
    * otherwise take the bench from today's stored confirmed XI for the fixture.
    Either way, ranks the named players by their cameo booking rate.
    """
    from . import targets as targets_mod, webview
    from .lineups import status_for
    from .model import compute_player_metrics

    timer = _Timer()
    appearances = _appearances_stored_only()
    timer.mark("Store loaded")
    positions = _load_positions_warned()
    target_day = fixtures_mod.uk_today()
    schedule = _schedule_best_effort(config.baseline_seasons())
    timer.mark("Schedule fetched")

    metrics = compute_player_metrics(appearances, config.MODEL)
    next_by_team = targets_mod.team_next_fixtures(schedule, target_day)
    all_teams = targets_mod.team_player_rows(
        appearances, positions=positions, next_by_team=next_by_team, metrics=metrics)

    q = args.team.strip().lower()
    matches = [t for t in all_teams if q in t.lower()]
    if not matches:
        print(f"No tracked team matches '{args.team}'. Try one of: "
              + ", ".join(sorted(all_teams)[:12]) + " …")
        return 1
    team = min(matches, key=len)
    team_rows = all_teams.get(team, [])
    row = _team_next_fixture_row(schedule, team, target_day)
    home, away = (row[0], row[1]) if row else (team, "")
    fixture_txt = f"{home} v {away}" if away else team

    bench_arg = getattr(args, "bench", None)
    if bench_arg:
        names = [n.strip() for n in re.split(r"[,\n]", bench_arg) if n.strip()]
        rows, missing = _match_named_players(names, team_rows)
        if missing:
            print(f"Couldn't match {len(missing)} name(s) to a {team} player: "
                  + ", ".join(missing) + " (check spelling / try the surname).")
        heading = f"{team} bench (you named {len(rows)})"
        subtitle = f"{fixture_txt} · {len(rows)} named bench player(s)"
    else:
        print(f"{team}: {fixture_txt}")
        status, confirmed = _confirmed_fixture_status(home, away)
        timer.mark("Line-up checked")
        # Bench = the squad MINUS the confirmed XI MINUS the unavailable — derived
        # from reliable signals (starters carry a position code; injured come from
        # the Team News panel). team_rows already excludes recently-absent players.
        if confirmed:
            rows = [r for r in team_rows
                    if status_for(status, r.player) not in ("starter", "out")]
            n_xi = sum(1 for v in status.values() if v == "starter")
            print(f"Confirmed XI ({n_xi} starters) — showing {team} players NOT in the XI "
                  f"and not injured/suspended.")
            heading = f"{team} — available subs (not in XI)"
            subtitle = f"{fixture_txt} · {len(rows)} not in the XI (excl. injured/suspended)"
        else:
            print("No confirmed XI for this game in today's line-ups file "
                  f"(data/lineups.json). Add it, or name the bench "
                  f'with:  report-late {args.team} --bench "Player One, Player Two, …"')
            rows = []
            heading = f"{team} bench"
            subtitle = f"{fixture_txt} · XI not confirmed"

    rows.sort(key=lambda r: (r.adj_sub_rate, r.rate), reverse=True)
    print(f"{len(rows)} {team} bench player(s), ranked by cameo booking rate.")

    logs = targets_mod.game_logs_for(appearances, rows, next_by_team=next_by_team)
    slug = re.sub(r"[^a-z0-9]+", "-", team.lower()).strip("-")
    html_path = webview.render_targets_html(
        rows, logs, target_day, title=f"{team} bench — Late Card Targets",
        subtitle=subtitle, heading=heading,
        out_name=f"late-{slug}-{target_day.isoformat()}.html")
    print(f"Interactive HTML written to {html_path}")
    timer.mark("HTML rendered")
    return 0


# A fixture entry's starter list must match at least this many of a team's squad
# rows for us to trust that we have THAT team's XI. A fixture entry may carry only
# one side's XI (11 names), so the other team matches ~0 — without this gate its
# whole squad would leak into the bench as "not in the XI".
_MIN_XI_MATCH = 6


def _confirmed_bench_rows(entry: dict, team_rows: list, status: dict) -> tuple[list, bool, bool]:
    """One team's late-card bench from a confirmed fixture line-up entry.

    Prefers the exact listed bench (matched to this squad's rows). Otherwise —
    when the entry lists no bench — returns everyone in ``team_rows`` who is NOT
    in the confirmed XI and NOT unavailable. Returns ``(rows, exact, have_xi)``.

    ``have_xi`` is False when this team's XI isn't in the entry (it may carry only
    one side's 11 starters), so the caller can skip the team and note it rather
    than listing the whole squad as "not in the XI".

    The XI/out exclusion matches names to squad rows by surname/token (via
    :func:`_match_named_players`), not the initial+surname key: a line-up may list
    starters by SURNAME only ("Rice"), whose key ("d|rice") wouldn't match the
    store's "Declan Rice" and would leak the whole XI back into the bench. The
    status index is kept as a second, belt-and-braces pass.
    """
    from .lineups import status_for

    named_bench = entry.get("bench") or []
    if named_bench:
        matched = _match_named_players(named_bench, team_rows)[0]
        if matched:
            return matched, True, True
    xi_rows = _match_named_players(entry.get("starters") or [], team_rows)[0]
    if len(xi_rows) < _MIN_XI_MATCH:
        # Too few of this fixture's starters are this team's players — its XI
        # wasn't captured. Don't leak the whole squad; signal "no XI".
        return [], False, False
    xi = {r.player for r in xi_rows}
    gone = {r.player for r in _match_named_players(entry.get("out") or [], team_rows)[0]}
    excluded = xi | gone
    bench = [r for r in team_rows
             if r.player not in excluded
             and status_for(status, r.player) not in ("starter", "out")]
    return bench, False, True


def _todays_late_fixtures(schedule, target_day, league_ids):
    """Today's in-scope fixtures still worth a late-card look, newest kick-off first.

    Keeps games scheduled for ``target_day`` in the chosen leagues (all tracked
    competitions when ``league_ids`` is None), dropping postponed/cancelled ties
    and any game whose kick-off was more than two hours ago (it's effectively
    over). Returns ``(fixtures_df, n_started, n_unknown_time)``.
    """
    import pandas as pd

    from .targets import _NOT_HAPPENING

    if schedule is None or getattr(schedule, "empty", True):
        return _empty_fixtures(), 0, 0
    df = schedule[schedule["date"].notna()]
    day = df[df["date"].dt.date == target_day]
    if "played" in day.columns:
        day = day[~day["played"].fillna(False)]
    if "notes" in day.columns:
        day = day[~day["notes"].fillna("").str.contains(
            _NOT_HAPPENING, case=False, regex=True)]
    if league_ids is not None and "league" in day.columns:
        day = day[day["league"].isin(league_ids)]
    day = day.reset_index(drop=True)
    if day.empty:
        return day, 0, 0

    has_time = "start_time" in day.columns
    unknown = (int(pd.to_datetime(day["start_time"], errors="coerce").isna().sum())
               if has_time else len(day))
    day, started = _drop_finished_fixtures(day)
    sort_col = "start_time" if has_time else "date"
    return day.sort_values(sort_col).reset_index(drop=True), started, unknown


def _cmd_report_late(args: argparse.Namespace) -> int:
    """Show the confirmed-XI bench for every one of today's in-play fixtures.

    A match-day view: for each of today's tracked games with a confirmed XI in
    today's line-ups file (and which kicked off no more than two hours ago), list both squads'
    players who are NOT in the XI and NOT injured/suspended — the players who can
    still come off the bench and pick up a late card. Ranked by cameo booking rate.
    """
    if getattr(args, "team", None):
        return _report_late_for_team(args)

    from . import lineups as lu, targets as targets_mod, webview
    from .model import compute_player_metrics

    timer = _Timer()
    appearances = _appearances_stored_only()  # no catch-up ingest for report-late
    timer.mark("Store loaded")
    positions = _load_positions_warned()
    target_day = fixtures_mod.uk_today()

    schedule = _schedule_best_effort(config.baseline_seasons())
    timer.mark("Schedule fetched")
    league_ids = _resolve_league_ids(getattr(args, "leagues", None))
    if league_ids is not None:
        print("Scoping to league(s): "
              + ", ".join(sorted(config.LEAGUE_BY_ID[i].short for i in league_ids)))

    fixtures, n_started, n_unknown = _todays_late_fixtures(schedule, target_day, league_ids)
    if n_started:
        print(f"Skipping {n_started} game(s) that kicked off more than 2 hours ago.")
    if n_unknown and "start_time" not in getattr(fixtures, "columns", []):
        print("Note: this schedule cache has no kick-off times — can't drop finished "
              "games. Delete data/schedule-*.parquet to refresh with times.")
    if fixtures.empty:
        print(f"No live/upcoming tracked fixtures today ({target_day}). Nothing to show.")
        return 0
    next_by_team = targets_mod.team_next_fixtures(schedule, target_day)
    metrics = compute_player_metrics(appearances, config.MODEL)
    all_teams = targets_mod.team_player_rows(
        appearances, positions=positions, next_by_team=next_by_team, metrics=metrics)
    timer.mark("Player metrics computed")

    cached = lu.load_lineups_for(target_day)
    rows: list = []
    shown_teams: list[str] = []
    n_confirmed = 0
    exact_teams = 0
    unconfirmed: list[str] = []
    missing_xi: list[str] = []
    for _, fx in fixtures.iterrows():
        home, away = str(fx["home_team"]), str(fx["away_team"])
        entry = next((f for f in cached.get("fixtures", [])
                      if _fixture_entry_matches(f, home, away) and f.get("confirmed")), None)
        if entry is None:
            unconfirmed.append(f"{home} v {away}")
            continue
        n_confirmed += 1
        status = lu.status_index({"date": target_day.isoformat(), "fixtures": [entry]})
        for side in (home, away):
            key = _resolve_team_key(side, all_teams)
            if not key or key in shown_teams:
                continue
            side_bench, exact, have_xi = _confirmed_bench_rows(
                entry, all_teams.get(key, []), status)
            if not have_xi:
                # The entry only carries the other side's XI for this fixture, so
                # we can't tell who's benched here — skip rather than list everyone.
                missing_xi.append(key)
                continue
            if side_bench:
                shown_teams.append(key)
                exact_teams += int(exact)
                rows.extend(side_bench)

    if unconfirmed:
        print(f"No confirmed XI yet for {len(unconfirmed)} game(s) in today's line-ups "
              "file — not shown until confirmed: "
              + ", ".join(unconfirmed) + ".")
    if missing_xi:
        print(f"No XI for {len(missing_xi)} team(s) — the line-up entry only has "
              "the other side's XI, so their bench can't be derived: "
              + ", ".join(sorted(set(missing_xi))) + ".")
    fallback_teams = len(shown_teams) - exact_teams
    print(f"{n_confirmed} confirmed fixture(s) → {len(rows)} bench player(s) across "
          f"{len(shown_teams)} team(s) ({exact_teams} exact bench, {fallback_teams} "
          "all-not-in-XI where the bench wasn't listed).")

    rows.sort(key=lambda r: (r.adj_sub_rate, r.rate), reverse=True)
    md_path = render_text.render_targets_markdown(rows, target_day)
    print(render_text.targets_terminal_summary(rows))
    print(f"\nLate-target report written to {md_path}")

    logs = targets_mod.game_logs_for(appearances, rows, next_by_team=next_by_team)
    # Explorer: only today's confirmed-fixture teams, so it matches the slate.
    team_players = {t: all_teams[t] for t in shown_teams if t in all_teams}
    team_logs = targets_mod.game_logs_for(
        appearances, [r for rws in team_players.values() for r in rws],
        next_by_team=next_by_team)
    html_path = webview.render_targets_html(
        rows, logs, target_day, team_players=team_players, team_logs=team_logs,
        title="Late Card Targets — today's confirmed benches",
        subtitle=f"{target_day.isoformat()} · {len(rows)} bench player(s) across "
                 f"{len(shown_teams)} team(s) with a confirmed XI")
    print(f"Interactive HTML written to {html_path}")
    timer.mark("HTML rendered")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="overdue", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser("backfill", help="One-off full history ingest.")
    p_backfill.add_argument(
        "--leagues", default=None,
        help="Comma-separated league ids to limit the ingest, e.g. "
             "\"ENG-Premier League,INT-Champions League\". Default: all tracked competitions.")
    p_backfill.add_argument(
        "--seasons", default=None,
        help="Comma-separated season codes to limit history, e.g. \"2526,2627\". "
             "Default: the configured baseline seasons.")
    p_backfill.add_argument(
        "--force", action="store_true",
        help="Re-read ONLY matches already in the store — the way to apply new "
             "columns. To add new matches, run backfill WITHOUT --force.")
    p_backfill.add_argument(
        "--batch-size", type=int, default=None, dest="batch_size",
        help="Matches per save-batch. Default: 20, or ALL at once with --force.")
    p_backfill.set_defaults(func=_cmd_backfill)

    p_explain = sub.add_parser(
        "explain", help="Show a player's metrics and why they are/aren't flagged today.")
    p_explain.add_argument("player", help="Player name or part of it.")
    p_explain.add_argument("--as-of", default=None, dest="as_of",
                           help="Compute metrics as of this date (YYYY-MM-DD), using only "
                                "earlier games — e.g. to see a player's pre-match state.")
    p_explain.set_defaults(func=_cmd_explain)

    p_odds = sub.add_parser(
        "refresh-odds",
        help="Paste 'to be carded' prices ('<Player> 11/5' lines) to fill the Edge column.")
    p_odds.add_argument("--book", default=None,
                        help="Which bookmaker the pasted prices are from (default Bet365). "
                             "Paste Bet365 then Sky Bet to keep the higher of the two.")
    p_odds.set_defaults(func=_cmd_refresh_odds)

    p_refresh_ev = sub.add_parser(
        "refresh-events",
        help="Ingest card + substitution events with minutes (for late-card stats).")
    p_refresh_ev.add_argument("--force", action="store_true",
                              help="Re-read every match's events.")
    p_refresh_ev.add_argument("--retry-missing", action="store_true", dest="retry_missing",
                              help="Retry matches a previous run tried but got no events for.")
    p_refresh_ev.add_argument("--leagues", default=None,
                              help="Only ingest events for these competitions, e.g. "
                                   "--leagues EPL or --leagues \"Premier League,La Liga\".")
    p_refresh_ev.add_argument("--seasons", default=None,
                              help="Only ingest events for these season codes, e.g. "
                                   "--seasons 2627 (the current season only). "
                                   "Default: the configured baseline seasons.")
    p_refresh_ev.set_defaults(func=_cmd_refresh_events)

    p_refresh_pos = sub.add_parser(
        "refresh-positions",
        help="Fetch & cache player positions (enables GK exclusion + position column).")
    p_refresh_pos.set_defaults(func=_cmd_refresh_positions)

    p_week = sub.add_parser(
        "report-week",
        help="Overdue players for each remaining day of the week (a table per day).")
    p_week.add_argument("--days", type=int, default=None,
                        help="Number of days from today to cover (default: through Sunday).")
    p_week.add_argument("--no-ingest", action="store_true",
                        help="Skip the automatic catch-up of newly-played matches.")
    p_week.set_defaults(func=_cmd_report_week)

    p_late = sub.add_parser(
        "report-late",
        help="Today's confirmed benches, ranked by cameo booking rate (late-card targets). "
             "Give a TEAM to scope to one game's bench only.")
    p_late.add_argument("team", nargs="?", default=None,
                        help="Optional club — show ONLY its benched players, "
                             "e.g. \"report-late Northbridge\".")
    p_late.add_argument("--bench", default=None,
                        help="Comma-separated bench player names to rank (works live), "
                             "e.g. --bench \"Abara, Castro, Holm\". Without it, the bench "
                             "comes from today's confirmed XI in data/lineups.json.")
    p_late.add_argument("--top", type=int, default=40, help="How many players to list.")
    p_late.add_argument("--min-sub-apps", type=int, default=6, dest="min_sub_apps",
                        help="Minimum cameo appearances to be ranked (default 6).")
    p_late.add_argument("--min-sub-cards", type=int, default=2, dest="min_sub_cards",
                        help="Minimum cameo bookings to be ranked.")
    p_late.add_argument("--leagues", default=None,
                        help="Only rank players from these competitions, e.g. "
                             "--leagues EPL or --leagues \"Premier League,Championship\". "
                             "(Ignored in the single-team mode.)")
    p_late.add_argument("--no-ingest", action="store_true",
                        help="Skip the automatic catch-up of newly-played matches.")
    p_late.set_defaults(func=_cmd_report_late)

    p_team = sub.add_parser(
        "report-team",
        help="Full card watch for one game's two squads (every player) — "
             "e.g. `report-team Northbridge`.")
    p_team.add_argument("team", help="Club name or part of it.")
    p_team.add_argument("--days-ahead", type=int, default=2, dest="days_ahead",
                        help="How many days out the team's next game may be to still "
                             "show it (default 2: today, tomorrow, or the next day).")
    p_team.add_argument("--no-ingest", action="store_true",
                        help="Skip the automatic catch-up of newly-played matches.")
    p_team.set_defaults(func=_cmd_report_team)

    p_assess = sub.add_parser(
        "assess",
        help="Score the model's card predictions for a past day (default: yesterday).")
    p_assess.add_argument("--date", default=None,
                          help="Day to assess, YYYY-MM-DD (default: yesterday, UK).")
    p_assess.add_argument("--no-ingest", action="store_true",
                          help="Skip the automatic catch-up of newly-played matches.")
    p_assess.add_argument("--explain-subs", action="store_true",
                          help="Show the substitution data behind each super-sub result.")
    p_assess.set_defaults(func=_cmd_assess)

    p_check = sub.add_parser(
        "check-teams",
        help="Diagnose club-name mismatches between the schedule and the store.")
    p_check.add_argument("--days", type=int, default=7,
                         help="Days ahead of fixtures to check (default 7).")
    p_check.set_defaults(func=_cmd_check_teams)

    for name, func, help_text in (
        ("run-daily", _cmd_run_daily, "Incremental update, write the dated report, print summary."),
        ("report", _cmd_report, "Catch up on new matches, then rebuild the dated report."),
    ):
        p = sub.add_parser(name, help=help_text)
        # Default output is the Markdown report + terminal summary. Extras opt-in:
        p.add_argument("--html", action="store_true", help="Also render the static HTML page.")
        p.add_argument("--telegram", action="store_true", help="Also send the Telegram alert.")
        p.add_argument("--dry-run", action="store_true",
                       help="With --telegram, print the alert instead of sending.")
        p.add_argument("--site-url", default=None, help="Public URL to link in the Telegram alert.")
        p.add_argument("--no-ingest", action="store_true",
                       help="Skip the automatic catch-up of newly-played matches.")
        p.add_argument("--leagues", default=None,
                       help="Only include these competitions' games today, e.g. "
                            "--leagues \"Premier League\" or --leagues EPL,LaLiga.")
        p.add_argument("--tomorrow", action="store_true",
                       help="Build the report for tomorrow's games instead of today's "
                            "(combine with --leagues, e.g. --tomorrow --leagues UCL).")
        p.set_defaults(func=func)
        if name == "run-daily":
            p.add_argument("--require-hour", type=int, default=None,
                           help="Only proceed at this Europe/London hour (e.g. 10).")
            p.add_argument("--force", action="store_true", help="Ignore the hour guard.")

    # Windows' default console codec (cp1252) can't encode accented player names
    # (e.g. 'ć'), which otherwise crashes a print. Force UTF-8 output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - not all streams support reconfigure
            pass

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
