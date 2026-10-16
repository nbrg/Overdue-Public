"""Assemble the daily 'overdue players' report from metrics + fixtures.

This is the view-model layer: pure data transformation that both the HTML page
and the Telegram message render from, so the two never drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from . import (
    config,
    fixtures as fixtures_mod,
    h2h as h2h_mod,
    odds,
    referees as referees_mod,
    suspensions as suspensions_mod,
)
from .config import LEAGUE_BY_ID, MODEL, ModelConfig
from .model import PlayerMetrics, compute_player_metrics


@dataclass
class ReportRow:
    player: str
    team: str
    opponent: str
    competition: str  # short label of today's fixture (EPL/UCL/…)
    league_name: str
    league_short: str
    appearances: int
    cards: int
    rate: float
    per90: float
    expected_interval: float
    streak: int
    streak_starts: int  # of the cardless run, how many were starts
    overdue_ratio: float
    last_card: str  # formatted date or "—"
    start_share: float
    position: str = ""  # season position (e.g. DF, MF, FW)
    start_rate: float = 0.0  # cards per start
    fouls_per90: float = 0.0  # fouls committed per 90 (leading indicator of cards)
    fouls_per_card: float = 0.0  # fouls committed per card received
    ref_adj_odds: float | None = None  # fair odds adjusted for the assigned referee
    starts: int = 0
    sub_cards: int = 0  # cards received off the bench (< start_minutes)
    sub_apps: int = 0  # number of sub cameo appearances
    availability: str = ""  # "" or "warning" (suspended are excluded upstream)
    availability_note: str = ""
    referee: str = ""
    bookie_odds: float | None = None  # cached 'to be carded' price (best of two books)
    bookie_source: str = ""  # which bookmaker the price is from
    bookie_all: dict = field(default_factory=dict)  # {bookmaker: price} for Bet365 & Sky
    referee_ypg: float | None = None  # referee's cards per game
    h2h_cards: float | None = None  # avg cards in past meetings of these teams
    h2h_games: int = 0
    vs_opp_cards: int = 0  # games the player was carded in v today's opponent (any club)
    vs_opp_games: int = 0  # games the player featured in v today's opponent (any club)
    vs_opp_clubs: dict = field(default_factory=dict)  # {club: (cards, games)} behind those totals
    fouls_drawn_per90: float = 0.0  # fouls the player draws (wins) per 90
    xi_status: str = ""  # today's confirmed line-up status: "Start" / "Bench" / "—"
    links: dict[str, str] = field(default_factory=dict)


@dataclass
class Report:
    generated_utc: str
    target_day: str
    rows: list[ReportRow]  # overdue players whose team plays on target_day
    fixtures: list[dict]  # today's fixtures for context
    total_scored: int  # players who cleared the sample gate
    threshold: float


def _league_labels(league_id: str) -> tuple[str, str]:
    lg = LEAGUE_BY_ID.get(league_id)
    if lg is None:
        return league_id, league_id
    return lg.name, lg.short


def _fmt_date(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    return pd.Timestamp(ts).strftime("%d %b %Y")


def _suspension_warning(
    appearances: pd.DataFrame, player: str, team: str, comp_id: str, season: str
) -> tuple[str, str]:
    """Compute the one-from-a-ban warning for today's competition."""
    if not comp_id:
        return "", ""
    comp_rows = appearances[
        (appearances["league"] == comp_id) & (appearances["season"] == season)
    ]
    player_yellows = int((comp_rows.loc[comp_rows["player"] == player, "yellow"] > 0).sum())
    team_games = int(comp_rows.loc[comp_rows["team"] == team, "game_id"].nunique())
    return suspensions_mod.warning_for(comp_id, player_yellows, team_games)


def _reconcile_fixture_names(fixtures: pd.DataFrame, store_teams: set[str]) -> pd.DataFrame:
    """Rewrite fixture team names to the appearance store's spelling.

    A schedule may spell some clubs shorter than the player-stats store
    ("Tottenham" vs "Tottenham Hotspur", "Manchester Utd" vs "Manchester United").
    Without this, those clubs' players fail the ``m.team in teams_playing`` check
    and are dropped from the report, and their opponent shows "—". Matching uses
    the whole-word :func:`teamnames.same_club`, so it never collapses two clubs.
    """
    if fixtures is None or fixtures.empty or not store_teams:
        return fixtures
    from .teamnames import same_club

    names = list(store_teams)

    def _sub(name: str) -> str:
        for st in names:
            if same_club(name, st):
                return st
        return name

    out = fixtures.copy()
    for col in ("home_team", "away_team"):
        if col in out.columns:
            out[col] = out[col].astype(str).map(_sub)
    return out


def build_report(
    appearances: pd.DataFrame,
    today_fixtures: pd.DataFrame,
    target_day: date,
    cfg: ModelConfig | None = None,
    schedule: pd.DataFrame | None = None,
    positions: dict[str, str] | None = None,
    appointments: list[dict] | None = None,
    lineup_status: dict[str, str] | None = None,
    metrics: list[PlayerMetrics] | None = None,
) -> Report:
    """Compute metrics, filter to overdue players who play today, and rank them.

    ``schedule`` (the full multi-season schedule) is optional and only used to
    derive referee card tendencies; without it, referee fields stay empty.
    ``positions`` maps player -> season position; goalkeepers are excluded and the
    position is shown in the report.
    ``lineup_status`` (``player_key -> status`` from today's confirmed
    XIs) drops players confirmed **out** of the squad (injured/omitted) AND those
    confirmed on the **bench** — this is a pre-match, starter-focused card watch, so
    a player who won't start isn't a target (benched sub targets are report-late's
    job). A confirmed starter is kept (they're exactly who this report wants).
    """
    cfg = cfg or MODEL
    positions = positions or {}
    lineup_status = lineup_status or {}
    from .lineups import _player_key

    def _is_gk(player: str) -> bool:
        return positions.get(player, "").upper().startswith("GK")

    # ``metrics`` is the expensive per-player computation; callers that build more
    # than one report from the same store (the daily/weekly pre-build + rebuild,
    # and the team explorer) pass it in so it's computed once, not repeatedly.
    if metrics is None:
        metrics = compute_player_metrics(appearances, cfg)
    scored = [m for m in metrics if m.qualifies and not _is_gk(m.player)]

    # Reconcile the schedule's club spellings to the store's, so short-named clubs
    # (Tottenham, Man Utd, …) aren't dropped by the exact `m.team in playing` test.
    store_team_names = (set(appearances["team"].dropna().astype(str))
                        if not appearances.empty and "team" in appearances.columns else set())
    today_fixtures = _reconcile_fixture_names(today_fixtures, store_team_names)

    playing = fixtures_mod.teams_playing(today_fixtures)
    opponents = fixtures_mod.opponent_map(today_fixtures)
    competitions = fixtures_mod.competition_map(today_fixtures)
    today_refs = referees_mod.todays_referees(today_fixtures)
    ref_rates = referees_mod.referee_card_rates(schedule, appearances)
    ref_ypg = ref_rates["per_game"].to_dict() if not ref_rates.empty else {}
    league_cpg = referees_mod.league_card_average(appearances)

    # Referee appointments fill in pre-match referees the schedule lacks.
    appt_for_team: dict[str, tuple[str, float | None]] = {}
    if appointments and today_fixtures is not None and not today_fixtures.empty:
        from . import appointments as appt_mod

        for _, fx in today_fixtures.iterrows():
            ref, cards = appt_mod.referee_for(
                appointments, str(fx["home_team"]), str(fx["away_team"])
            )
            if ref:
                appt_for_team[str(fx["home_team"])] = (ref, cards)
                appt_for_team[str(fx["away_team"])] = (ref, cards)

    # Head-to-head card average per team playing today.
    h2h_for_team: dict[str, tuple[int, float | None]] = {}
    if today_fixtures is not None and not today_fixtures.empty:
        for _, fx in today_fixtures.iterrows():
            home, away = str(fx["home_team"]), str(fx["away_team"])
            result = h2h_mod.h2h_card_average(schedule, appearances, home, away)
            h2h_for_team[home] = result
            h2h_for_team[away] = result

    vs_opp = h2h_mod.records_vs_opponent(h2h_mod.player_vs_opponent(appearances))

    cutoff = pd.Timestamp(target_day) - pd.Timedelta(days=cfg.max_days_since_last_appearance)
    current_season = config.baseline_seasons(target_day)[-1]

    rows: list[ReportRow] = []
    for m in scored:
        if not m.overdue:
            continue
        # Only surface players whose team is known to play today. An empty
        # ``playing`` set (no fixtures, or a failed fixtures fetch) means we
        # flag nobody rather than emit false alerts.
        if m.team not in playing:
            continue
        # Drop players who haven't featured recently — retired, transferred away,
        # or out all season won't be in today's XI.
        if m.last_appearance_date is not None and pd.notna(m.last_appearance_date):
            if pd.Timestamp(m.last_appearance_date) < cutoff:
                continue
        # Drop fringe players unlikely to start (low share of team's league games).
        if m.start_share < cfg.min_start_share:
            continue
        # Drop players out of the XI — started none of the team's last few games.
        if m.recent_starts < cfg.min_recent_starts:
            continue
        # Drop likely-unavailable players (injured/suspended) — absent from the
        # team's last few consecutive games.
        if m.recent_dnp >= cfg.max_recent_dnp:
            continue
        # Drop players today's confirmed XI rules out as a pre-match starter target:
        # the out-of-squad (injured/omitted) AND the benched (won't start). A
        # confirmed starter is kept — they're the ideal candidate here. (Bench/out
        # only apply once the XI is confirmed ~1h before kick-off; before that every
        # overdue regular is shown.)
        if lineup_status.get(_player_key(m.player)) in ("out", "bench"):
            continue
        league_name, league_short = _league_labels(m.league)
        comp_id = competitions.get(m.team, "")
        _, comp_short = _league_labels(comp_id)
        # Prefer an appointment (available pre-match); fall back to the schedule's
        # post-match referee. Card avg: appointment's figure, else our own history.
        appt_ref, appt_cards = appt_for_team.get(m.team, ("", None))
        referee = appt_ref or today_refs.get(m.team, "")
        referee_cards = appt_cards if appt_cards is not None else (
            ref_ypg.get(referee) if referee else None
        )
        # Referee-adjusted odds: scale the player's per-start card probability by
        # how strict the assigned ref is vs the league average, then price it.
        ref_adj_odds = None
        if referee_cards and league_cpg and m.start_rate > 0:
            adj_prob = min(0.95, m.start_rate * (referee_cards / league_cpg))
            if adj_prob > 0:
                ref_adj_odds = 1.0 / adj_prob
        # Suspension warning is for the competition of *today's* fixture, counting
        # only that competition's yellows this season (bans are competition-siloed).
        availability, availability_note = _suspension_warning(
            appearances, m.player, m.team, comp_id, current_season
        )
        # His record v today's opponent with every club he's faced them for.
        clubs = vs_opp.get((m.player, opponents.get(m.team)), {})
        rows.append(
            ReportRow(
                player=m.player,
                team=m.team,
                opponent=opponents.get(m.team, "—"),
                competition=comp_short,
                league_name=league_name,
                league_short=league_short,
                position=positions.get(m.player, ""),
                availability=availability,
                availability_note=availability_note,
                referee=referee,
                referee_ypg=referee_cards,
                h2h_cards=h2h_for_team.get(m.team, (0, None))[1],
                h2h_games=h2h_for_team.get(m.team, (0, None))[0],
                vs_opp_clubs=clubs,
                vs_opp_cards=sum(c for c, _ in clubs.values()),
                vs_opp_games=sum(g for _, g in clubs.values()),
                fouls_drawn_per90=m.fouls_drawn_per90,
                xi_status={"starter": "Start", "bench": "Bench"}.get(
                    lineup_status.get(_player_key(m.player), ""), "—"),
                appearances=m.appearances,
                cards=m.cards,
                rate=m.rate,
                per90=m.per90,
                expected_interval=m.expected_interval,
                streak=m.streak,
                streak_starts=m.streak_starts,
                overdue_ratio=m.overdue_ratio,
                last_card=_fmt_date(m.last_card_date),
                start_share=m.start_share,
                start_rate=m.start_rate,
                fouls_per90=m.fouls_per90,
                fouls_per_card=m.fouls_per_card,
                ref_adj_odds=ref_adj_odds,
                starts=m.starts,
                sub_cards=m.sub_cards,
                sub_apps=m.sub_apps,
                links=odds.lookup_links(m.player, m.team),
            )
        )

    rows.sort(key=lambda r: r.overdue_ratio, reverse=True)

    fixtures_list = []
    if not today_fixtures.empty:
        for _, fx in today_fixtures.sort_values("date").iterrows():
            name, short = _league_labels(str(fx["league"]))
            ref = str(fx.get("referee") or "").strip()
            ref = "" if ref.lower() == "nan" else ref
            home = str(fx["home_team"])
            h2h_games, h2h_avg = h2h_for_team.get(home, (0, None))
            fixtures_list.append(
                {
                    "home": home,
                    "away": str(fx["away_team"]),
                    "league": short,
                    "referee": ref,
                    "referee_ypg": ref_ypg.get(ref) if ref else None,
                    "h2h_cards": h2h_avg,
                    "h2h_games": h2h_games,
                }
            )

    return Report(
        generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        target_day=target_day.strftime("%A %d %B %Y"),
        rows=rows,
        fixtures=fixtures_list,
        total_scored=len(scored),
        threshold=cfg.overdue_threshold,
    )
