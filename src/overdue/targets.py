"""Find players worth targeting for *late* cards.

The betting angle here is bookings that come late in a match (useful for "card
after X minutes" / in-play markets). The per-match data gives each
player's card count and minutes played, but **not the minute a card was shown**,
so we can't directly measure "carded after 70'". What we *can* measure precisely
is the **sub-cameo card**: a player brought on late (played fewer than
``start_minutes``) who still picks up a booking. Such a card is, by construction,
a late-game card — these are the game-managers and tactical-foul specialists.

:func:`sub_card_targets` ranks players by that tendency across the whole store.
It's a standalone research view (not filtered to today's fixtures), so you can
keep a shortlist of names to watch for whenever they're named among the subs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import LEAGUE_BY_ID, MODEL, ModelConfig
from .model import PlayerMetrics, compute_player_metrics, is_started_value, started_mask
from .lineups import _player_key


# Pseudo-cameos used to shrink a small cameo sample toward the league baseline.
# A player with this many cameos is weighted 50/50 between their own rate and the
# baseline; more cameos than this and their own rate dominates.
SMOOTHING_CAMEOS = 6


@dataclass
class TargetRow:
    player: str
    team: str
    league: str
    position: str
    appearances: int
    cards: int
    rate: float  # cards per appearance (all appearances)
    sub_apps: int  # appearances of < start_minutes (came off the bench)
    sub_cards: int  # cards received in those cameos
    sub_rate: float  # sub_cards / sub_apps (raw cameo rate)
    adj_sub_rate: float  # cameo rate shrunk toward the league baseline (the sort key)
    starts: int
    start_cards: int
    start_rate: float  # cards per start
    fouls_per90: float = 0.0  # fouls committed per 90 (leading indicator of cards)
    fouls_per_card: float = 0.0  # fouls committed per card
    fouls_drawn_per90: float = 0.0  # fouls the player draws (wins) per 90
    # Overdue-model fields (so the team explorer / report-team can show the full
    # picture, not just the cameo columns).
    expected_interval: float = 0.0  # appearances between cards (1/rate)
    streak: int = 0  # appearances since last card
    streak_starts: int = 0  # of those, how many were starts
    overdue_ratio: float = 0.0  # streak / expected_interval
    start_share: float = 0.0  # share of the team's recent games started
    recent_dnp: int = 0  # consecutive most-recent team games not featured in (injury proxy)
    next_label: str = ""  # "Today" / "Sat 20 Sep" / "" if unknown
    plays_today: bool = False
    next_days: int = 9999  # days until next game (0 = today; 9999 = unknown), for sorting
    bookie_odds: float | None = None  # cached 'to be carded' price (best of two books)
    bookie_source: str = ""  # which bookmaker the price is from
    bookie_all: dict = field(default_factory=dict)  # {bookmaker: price} for Bet365 & Sky


def _cameo_baseline(pool: list[PlayerMetrics]) -> float:
    total_sub_apps = sum(m.sub_apps for m in pool)
    total_sub_cards = sum(m.sub_cards for m in pool)
    return (total_sub_cards / total_sub_apps) if total_sub_apps else 0.0


def _to_target_row(m, positions, baseline, smoothing, next_by_team) -> "TargetRow":
    adj = (m.sub_cards + smoothing * baseline) / (m.sub_apps + smoothing)
    nxt = next_by_team.get(m.team)
    return TargetRow(
        player=m.player, team=m.team, league=m.league,
        position=positions.get(m.player, ""),
        appearances=m.appearances, cards=m.cards, rate=m.rate,
        sub_apps=m.sub_apps, sub_cards=m.sub_cards,
        sub_rate=(m.sub_cards / m.sub_apps) if m.sub_apps else 0.0,
        adj_sub_rate=adj, starts=m.starts, start_cards=m.start_cards,
        start_rate=m.start_rate, fouls_per90=m.fouls_per90,
        fouls_per_card=m.fouls_per_card, fouls_drawn_per90=m.fouls_drawn_per90,
        expected_interval=m.expected_interval, streak=m.streak,
        streak_starts=m.streak_starts, overdue_ratio=m.overdue_ratio,
        start_share=m.start_share, recent_dnp=m.recent_dnp,
        next_label=("Today" if nxt.is_today else nxt.date_display) if nxt else "",
        plays_today=bool(nxt and nxt.is_today),
        next_days=nxt.days_until if nxt else 9999,
    )


def team_player_rows(
    appearances: pd.DataFrame,
    cfg: ModelConfig | None = None,
    positions: dict[str, str] | None = None,
    next_by_team: dict[str, "NextFixture"] | None = None,
    smoothing: int = SMOOTHING_CAMEOS,
    metrics: list[PlayerMetrics] | None = None,
) -> dict[str, list["TargetRow"]]:
    """Every in-scope outfielder's card stats, grouped by club (no filters).

    Powers the team explorer: unlike :func:`sub_card_targets` there is no sample
    gate and no recency filter, so you see the whole squad. Each team's players
    are sorted most-carded first (ties by adjusted cameo rate). ``metrics`` may be
    a precomputed :func:`compute_player_metrics` list so a report that already ran
    it doesn't pay for it again.
    """
    cfg = cfg or MODEL
    positions = positions or {}
    next_by_team = next_by_team or {}

    def _is_gk(player: str) -> bool:
        return positions.get(player, "").upper().startswith("GK")

    if metrics is None:
        metrics = compute_player_metrics(appearances, cfg)
    metrics = [m for m in metrics if not _is_gk(m.player)]
    baseline = _cameo_baseline(metrics)
    by_team: dict[str, list[TargetRow]] = {}
    for m in metrics:
        # Drop players who look unavailable — absent from the team's last few
        # consecutive games (an injury/suspension proxy from recent ingests), so a
        # squad view doesn't list players who can't be picked.
        if m.recent_dnp >= cfg.max_recent_dnp:
            continue
        by_team.setdefault(m.team, []).append(
            _to_target_row(m, positions, baseline, smoothing, next_by_team))
    for team in by_team:
        by_team[team].sort(key=lambda r: (r.cards, r.adj_sub_rate), reverse=True)
    return dict(sorted(by_team.items()))


def sub_card_targets(
    appearances: pd.DataFrame,
    cfg: ModelConfig | None = None,
    positions: dict[str, str] | None = None,
    min_sub_apps: int = 6,
    min_sub_cards: int = 2,
    smoothing: int = SMOOTHING_CAMEOS,
    top_n: int | None = 40,
    next_by_team: dict[str, NextFixture] | None = None,
    lineup_status: dict[str, str] | None = None,
    metrics: list[PlayerMetrics] | None = None,
) -> list[TargetRow]:
    """Rank in-scope players by their propensity to be booked as a sub.

    Keeps outfielders with at least ``min_sub_apps`` cameo appearances and
    ``min_sub_cards`` cameo bookings, then ranks by an **adjusted** cameo rate:
    each player's raw rate is shrunk toward the league-average cameo booking rate
    in proportion to how few cameos they have (``smoothing`` pseudo-cameos at the
    baseline). So a proven ``5/12`` outranks a noisy ``2/4`` even though the raw
    rate is lower — small samples can't top the list on luck.

    ``lineup_status`` (``player_key -> "starter"/"bench"/"out"``, from today's
    confirmed XIs) drops players who can't be a sub today: confirmed
    **starters** (they start, so no cameo) and the **out-of-squad** (injured /
    omitted). Benched and un-named players are kept.
    """
    cfg = cfg or MODEL
    positions = positions or {}
    next_by_team = next_by_team or {}
    lineup_status = lineup_status or {}

    def _is_gk(player: str) -> bool:
        return positions.get(player, "").upper().startswith("GK")

    if metrics is None:
        metrics = compute_player_metrics(appearances, cfg)

    # League baseline = cameo bookings per cameo appearance across all in-scope
    # outfielders. Small samples are shrunk toward this so they regress to normal.
    pool = [m for m in metrics if not _is_gk(m.player)]
    baseline = _cameo_baseline(pool)

    from .lineups import status_for

    rows: list[TargetRow] = []
    for m in pool:
        if m.sub_apps < min_sub_apps or m.sub_cards < min_sub_cards:
            continue
        # Drop players today's confirmed XI rules out as a sub: named starters
        # (they start) and the out-of-squad (injured/omitted). Bench and unnamed
        # players are kept — they're the ones who can come on and get booked.
        if status_for(lineup_status, m.player) in ("starter", "out"):
            continue
        # Drop players out of the XI — started none of the team's last few games
        # (the all-DNP ghosts). A genuine target is at least sometimes starting.
        if m.recent_starts < cfg.min_recent_starts:
            continue
        # Drop likely-unavailable players (injured/suspended) — absent from the
        # team's last few consecutive games.
        if m.recent_dnp >= cfg.max_recent_dnp:
            continue
        rows.append(_to_target_row(m, positions, baseline, smoothing, next_by_team))

    # Best targets first: highest sample-adjusted cameo rate, ties by volume.
    rows.sort(key=lambda r: (r.adj_sub_rate, r.sub_cards), reverse=True)
    if top_n is not None:
        rows = rows[:top_n]
    return rows


def game_logs_for(
    appearances: pd.DataFrame,
    rows: list,
    cfg: ModelConfig | None = None,
    window: int = 20,
    next_by_team: dict[str, NextFixture] | None = None,
    events: pd.DataFrame | None = None,
) -> dict[str, "PlayerDetail"]:
    """Build ``player -> PlayerDetail`` for every (player, team) in ``rows``.

    ``rows`` is any list of objects exposing ``.player`` and ``.team`` (ReportRow
    or TargetRow), so all three report formats share one detail source. Each
    detail carries the team's recent game log, the player's full
    season × competition breakdown with card data, and (when ``next_by_team`` is
    supplied) the team's next scheduled fixture.
    """
    cfg = cfg or MODEL
    next_by_team = next_by_team or {}
    if events is None:
        # Card/sub minutes come from the event store; load it once (falls back to
        # an empty frame — and thus the letter/estimate display — if not built).
        from . import store
        events = store.load_events()
    # Build the shared index ONCE (group the store by game/team/player, precompute
    # each game's opponent+score and each event's minute), then every player's log
    # is cheap lookups instead of re-scanning the whole store per player.
    index = _LogIndex(appearances, events, cfg)
    logs: dict[str, PlayerDetail] = {}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r.player, r.team)
        if key in seen:
            continue
        seen.add(key)
        logs[r.player] = PlayerDetail(
            games=index.team_log(r.player, r.team, window),
            start_games=index.role_log(r.player, r.team, "Start", window),
            sub_games=index.role_log(r.player, r.team, "Sub", window),
            breakdown=index.breakdown(r.player),
            next_fixture=next_by_team.get(r.team),
        )
    return logs


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 57 -> '57th', 61 -> '61st'."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@dataclass
class GameLogEntry:
    date: str  # YYYY-MM-DD
    opponent: str
    competition: str  # short league label (EPL/UCL/…)
    minutes: int
    role: str  # "Start" | "Sub" | "DNP"
    card: str  # "" | "Y" | "R" | "Y+R"
    team_score: int = 0  # the player's team's goals
    opp_score: int = 0  # the opponent's goals
    score_known: bool = False  # False when the store lacks goal data
    entry_minute: int | None = None  # for subs: the minute they came on
    card_minute: str = ""  # formatted card minute(s), e.g. "87'" or "90+3'"

    @property
    def min_display(self) -> str:
        """Minutes for a starter (plain number); a sub's entry minute otherwise.

        A substitute shows *when* they came on ("57th min") so it's obvious at a
        glance they didn't start; a starter keeps the raw minutes played.
        """
        if self.role == "Sub" and self.entry_minute is not None:
            return f"{_ordinal(self.entry_minute)} min"
        return str(self.minutes)

    @property
    def card_display(self) -> str:
        """The booking minute (e.g. "87'", "90+3'") when known, else the letter."""
        if self.card_minute:
            return self.card_minute
        return self.card or "—"


@dataclass
class SeasonCompRow:
    season: str  # display form, e.g. "26/27"
    competition: str  # short league label
    apps: int  # matches played (any minutes)
    starts: int
    subs: int
    yellows: int  # matches yellow-carded (any role)
    reds: int  # matches sent off (any role)
    start_yellows: int = 0  # of starts
    start_reds: int = 0
    sub_yellows: int = 0  # of sub cameos
    sub_reds: int = 0


@dataclass
class NextFixture:
    date_display: str  # e.g. "Sat 20 Sep"
    opponent: str
    competition: str  # short league label
    is_today: bool
    days_until: int = 0  # days from today (0 = today), for date sorting


@dataclass
class PlayerDetail:
    games: list[GameLogEntry]  # the team's last N games and the player's role in each
    breakdown: list[SeasonCompRow]  # the player's apps/cards per season × competition
    next_fixture: NextFixture | None = None  # the team's next scheduled game
    start_games: list[GameLogEntry] = field(default_factory=list)  # last N starts
    sub_games: list[GameLogEntry] = field(default_factory=list)  # last N sub cameos


# Schedule notes that mean a fixture won't be played as listed.
_NOT_HAPPENING = "postpone|cancel|abandon|suspend|awarded|walkover"


def team_next_fixtures(schedule, today) -> dict[str, NextFixture]:
    """Map each team to its next unplayed, not-called-off, not-yet-finished fixture.

    A game that kicked off more than two hours ago is treated as over even before
    the schedule marks it played, so a team whose match already finished today rolls to
    its next fixture rather than still counting as "plays today".
    """
    if schedule is None or getattr(schedule, "empty", True):
        return {}
    import pandas as pd

    df = schedule.copy()
    df = df[df["date"].notna()]
    upcoming = df[df["date"].dt.date >= today]
    if "played" in upcoming.columns:
        upcoming = upcoming[~upcoming["played"].fillna(False)]
    if "notes" in upcoming.columns:
        called_off = upcoming["notes"].fillna("").str.contains(
            _NOT_HAPPENING, case=False, regex=True
        )
        upcoming = upcoming[~called_off]
    if "start_time" in upcoming.columns:
        from . import fixtures as _fx

        now = _fx.uk_now().replace(tzinfo=None)
        st = pd.to_datetime(upcoming["start_time"], errors="coerce")
        upcoming = upcoming[~(st.notna() & (st < now - pd.Timedelta(hours=2)))]
    upcoming = upcoming.sort_values("date")

    out: dict[str, NextFixture] = {}
    for _, r in upcoming.iterrows():
        d = pd.Timestamp(r["date"]).date()
        home, away = str(r["home_team"]), str(r["away_team"])
        comp = _short_comp(str(r["league"]))
        disp = pd.Timestamp(r["date"]).strftime("%a %d %b")
        days = (d - today).days
        for team, opp in ((home, away), (away, home)):
            if team not in out:  # first upcoming fixture wins
                out[team] = NextFixture(disp, opp, comp, is_today=(d == today),
                                        days_until=days)
    return out


def _fmt_season(code: str) -> str:
    code = str(code)
    return f"{code[:2]}/{code[2:]}" if len(code) == 4 and code.isdigit() else code


def player_season_breakdown(
    appearances: pd.DataFrame, player: str, cfg: ModelConfig | None = None
) -> list[SeasonCompRow]:
    """The player's matches (with start/sub split and cards) per season × competition."""
    cfg = cfg or MODEL
    if appearances.empty:
        return []
    df = appearances[(appearances["player"] == player) & (appearances["minutes"] >= 1)]
    if df.empty:
        return []

    out: list[SeasonCompRow] = []
    for (season, league), grp in df.groupby(["season", "league"]):
        started = started_mask(grp, cfg)
        # Total cards (sum of per-match counts), so a two-yellow game counts as 2
        # and season totals line up with public season stats.
        out.append(
            SeasonCompRow(
                season=_fmt_season(season),
                competition=_short_comp(str(league)),
                apps=int(len(grp)),
                starts=int(started.sum()),
                subs=int((~started).sum()),
                yellows=int(grp["yellow"].sum()),
                reds=int(grp["red"].sum()),
                start_yellows=int(grp.loc[started, "yellow"].sum()),
                start_reds=int(grp.loc[started, "red"].sum()),
                sub_yellows=int(grp.loc[~started, "yellow"].sum()),
                sub_reds=int(grp.loc[~started, "red"].sum()),
            )
        )
    # Newest season first, then by competition.
    out.sort(key=lambda r: (r.season, r.competition), reverse=True)
    return out


def _short_comp(league_id: str) -> str:
    lg = LEAGUE_BY_ID.get(league_id)
    return lg.short if lg is not None else league_id


def _minute_str(minute: int, minute_plus: int) -> str:
    """Format an event minute: '87'' normally, '90+3'' for stoppage time."""
    if minute_plus > 0:
        return f"{minute - minute_plus}+{minute_plus}'"
    return f"{minute}'"


def _build_breakdowns(df: pd.DataFrame) -> dict[str, list["SeasonCompRow"]]:
    """All players' season × competition breakdowns in one grouped pass.

    ``df`` carries a ``roletag`` column (Start/Sub/DNP). Featured rows are
    aggregated by (player, season, league) with start/sub-split card counts, then
    assembled into ``player -> [SeasonCompRow]`` (newest season first).
    """
    feat = df[df["roletag"] != "DNP"]
    if feat.empty:
        return {}
    st = feat["roletag"].eq("Start")
    feat = feat.assign(
        _st=st.astype("int64"),
        _sy=feat["yellow"].where(st, 0), _sr=feat["red"].where(st, 0),
        _by=feat["yellow"].where(~st, 0), _br=feat["red"].where(~st, 0),
    )
    grp = feat.groupby(["player", "season", "league"], sort=False).agg(
        apps=("game_id", "size"), starts=("_st", "sum"),
        yellows=("yellow", "sum"), reds=("red", "sum"),
        sy=("_sy", "sum"), sr=("_sr", "sum"), by=("_by", "sum"), br=("_br", "sum"))
    out: dict[str, list[SeasonCompRow]] = {}
    for (player, season, league), r in grp.iterrows():
        apps, starts = int(r.apps), int(r.starts)
        out.setdefault(str(player), []).append(SeasonCompRow(
            season=_fmt_season(season), competition=_short_comp(str(league)),
            apps=apps, starts=starts, subs=apps - starts,
            yellows=int(r.yellows), reds=int(r.reds),
            start_yellows=int(r.sy), start_reds=int(r.sr),
            sub_yellows=int(r.by), sub_reds=int(r.br)))
    for rows in out.values():
        rows.sort(key=lambda x: (x.season, x.competition), reverse=True)
    return out


class _LogIndex:
    """Precomputed views of the appearance + event stores so many players' game
    logs build from cheap lookups instead of re-scanning the whole store per
    player. Built once (a few groupbys over the store), then reused for all rows.
    """

    def __init__(self, appearances: pd.DataFrame, events, cfg: ModelConfig):
        self.cfg = cfg
        self.has_goals = False
        self._team_games: dict[str, pd.DataFrame] = {}
        self._player_rows: dict[str, pd.DataFrame] = {}
        self._game_ctx: dict[str, dict] = {}
        self._goals: dict[tuple[str, str], int] = {}
        self._og: dict[tuple[str, str], int] = {}
        self.card_by: dict[tuple[str, str], str] = {}
        self.sub_by: dict[tuple[str, str], int] = {}
        self._pby_cache: dict[tuple[str, str], dict] = {}
        self._breakdowns: dict[str, list] = {}
        if appearances is None or appearances.empty:
            return

        df = appearances.dropna(subset=["game_id", "date"]).copy()
        for col in ("game_id", "team", "player", "league"):
            df[col] = df[col].astype(str)
        self.has_goals = "goals" in df.columns

        # Vectorised role per row (matches model.started_mask / is_started_value):
        # started flag where known, else minutes vs the fallback; <1 min = DNP.
        started = (pd.to_numeric(df["started"], errors="coerce").fillna(-1)
                   if "started" in df.columns else pd.Series(-1.0, index=df.index))
        minutes = pd.to_numeric(df["minutes"], errors="coerce").fillna(0)
        is_start = (started == 1) | ((started < 0) & (minutes >= cfg.start_minutes_fallback))
        # 'roletag' (no leading underscore) so itertuples keeps the attribute name.
        df["roletag"] = np.where(minutes < 1, "DNP", np.where(is_start, "Start", "Sub"))

        # Per-game context: which teams featured, plus goals / own-goals per team.
        for gid, grp in df.groupby("game_id"):
            self._game_ctx[gid] = {"teams": grp["team"].unique().tolist(),
                                   "league": grp["league"].iloc[0]}
        if self.has_goals:
            gb = df.groupby(["game_id", "team"])["goals"].sum()
            self._goals = {(str(g), str(t)): int(v) for (g, t), v in gb.items()}
            if "own_goals" in df.columns:
                ob = df.groupby(["game_id", "team"])["own_goals"].sum()
                self._og = {(str(g), str(t)): int(v) for (g, t), v in ob.items()}

        # Team -> its unique games (game_id/date/league) ordered oldest→newest.
        tg = df[["team", "game_id", "date", "league"]].drop_duplicates(["team", "game_id"])
        self._team_games = {t: g.sort_values("date") for t, g in tg.groupby("team")}
        # Player -> all their appearance rows (across teams/seasons), with roletag.
        self._player_rows = {p: g for p, g in df.groupby("player")}
        # Per-player season × competition breakdown, precomputed in ONE grouped pass
        # (start/sub-split card counts) instead of a groupby per player at render.
        self._breakdowns = _build_breakdowns(df)

        # Event minutes keyed by (game_id, player_key): a booking string and a
        # sub-on minute. One pass over the event store, shared by every player.
        if events is not None and not getattr(events, "empty", True):
            per_card: dict[tuple[str, str], list[tuple[int, str]]] = {}
            for _, e in events.iterrows():
                gid, pkey = str(e["game_id"]), _player_key(str(e["player"]))
                minute, plus, kind = int(e["minute"]), int(e["minute_plus"]), str(e["kind"])
                if kind == "sub":
                    self.sub_by[(gid, pkey)] = minute - plus
                elif kind in ("yellow", "red", "second_yellow"):
                    per_card.setdefault((gid, pkey), []).append((minute, _minute_str(minute, plus)))
            for key, cards in per_card.items():
                self.card_by[key] = ", ".join(s for _, s in sorted(cards))

    def _opp_and_score(self, gid: str, team: str):
        ctx = self._game_ctx.get(gid)
        if ctx is None:
            return "", 0, 0, False
        others = [t for t in ctx["teams"] if t != team]
        opp = others[0] if others else ""
        if not self.has_goals:
            return opp, 0, 0, False
        team_score = self._goals.get((gid, team), 0) + sum(self._og.get((gid, o), 0) for o in others)
        opp_score = sum(self._goals.get((gid, o), 0) for o in others) + self._og.get((gid, team), 0)
        return opp, team_score, opp_score, True

    def _player_by_gid(self, player: str, team: str) -> dict:
        """``game_id -> {minutes, yellow, red, role}`` (plain dicts) for one team,
        built once and cached so the three role views share it and the hot loop
        avoids per-cell pandas access."""
        key = (player, team)
        pby = self._pby_cache.get(key)
        if pby is None:
            prows = self._player_rows.get(player)
            pby = {}
            if prows is not None:
                sub = prows[prows["team"] == team].drop_duplicates("game_id")
                for r in sub.itertuples(index=False):
                    pby[str(r.game_id)] = {"minutes": int(r.minutes), "yellow": int(r.yellow),
                                           "red": int(r.red), "role": str(r.roletag)}
            self._pby_cache[key] = pby
        return pby

    def _entries(self, chosen: list, player: str, team: str, include_dnp: bool):
        """``chosen`` is a newest-first list of (game_id, date, league)."""
        pkey = _player_key(player)
        pby = self._player_by_gid(player, team)
        entries: list[GameLogEntry] = []
        for gid, gdate, league in chosen:
            pr = pby.get(gid)
            if pr is None:
                if not include_dnp:
                    continue
                minutes, role, yellow, red = 0, "DNP", 0, 0
            else:
                minutes, role, yellow, red = pr["minutes"], pr["role"], pr["yellow"], pr["red"]
                if minutes < 1:
                    if not include_dnp:
                        continue
                    role = "DNP"
            opp, ts, os_, known = self._opp_and_score(gid, team)
            card = "Y+R" if (yellow and red) else "R" if red else "Y" if yellow else ""
            entry_minute = None
            if role == "Sub":
                entry_minute = self.sub_by.get((gid, pkey))
                if entry_minute is None:
                    entry_minute = max(1, 91 - minutes)
            card_minute = self.card_by.get((gid, pkey), "") if card else ""
            entries.append(GameLogEntry(
                date=pd.Timestamp(gdate).strftime("%Y-%m-%d"),
                opponent=opp, competition=_short_comp(str(league)),
                minutes=minutes, role=role, card=card,
                team_score=ts, opp_score=os_, score_known=known,
                entry_minute=entry_minute, card_minute=card_minute))
        return entries

    @staticmethod
    def _chosen(sel: pd.DataFrame) -> list:
        """(game_id, date, league) tuples newest-first, via fast column .tolist()."""
        gids = [str(g) for g in sel["game_id"].tolist()]
        dates = sel["date"].tolist()
        lgs = sel["league"].tolist()
        return list(zip(gids, dates, lgs))[::-1]

    def team_log(self, player: str, team: str, window: int) -> list[GameLogEntry]:
        tg = self._team_games.get(team)
        if tg is None or tg.empty:
            return []
        return self._entries(self._chosen(tg.tail(window)), player, team, include_dnp=True)

    def role_log(self, player: str, team: str, role: str, window: int) -> list[GameLogEntry]:
        prows = self._player_rows.get(player)
        if prows is None:
            return []
        mine = prows[(prows["team"] == team) & (prows["roletag"] == role)] \
            .drop_duplicates("game_id").sort_values("date").tail(window)
        if mine.empty:
            return []
        return self._entries(self._chosen(mine), player, team, include_dnp=False)

    def breakdown(self, player: str) -> list["SeasonCompRow"]:
        return self._breakdowns.get(player, [])


def player_game_log(
    appearances: pd.DataFrame,
    player: str,
    team: str,
    cfg: ModelConfig | None = None,
    window: int = 20,
    events: pd.DataFrame | None = None,
) -> list[GameLogEntry]:
    """The player's involvement in their team's last ``window`` games (newest first).

    For each of the team's most recent matches: the player's minutes, whether
    they started / came on / didn't feature, and whether they were carded. Games
    the player sat out show as DNP with 0 minutes, so rotation is visible. Card /
    sub minutes come from ``events`` when present, else the 91−minutes estimate
    and the plain card letter. (Standalone helper — :func:`game_logs_for` builds a
    shared :class:`_LogIndex` once for many players instead.)
    """
    cfg = cfg or MODEL
    if appearances is None or appearances.empty:
        return []
    return _LogIndex(appearances, events, cfg).team_log(player, team, window)


def player_role_log(
    appearances: pd.DataFrame,
    player: str,
    team: str,
    role: str,  # "Start" | "Sub"
    cfg: ModelConfig | None = None,
    window: int = 20,
    events: pd.DataFrame | None = None,
) -> list[GameLogEntry]:
    """The player's last ``window`` appearances **of one role** for ``team``.

    Unlike :func:`player_game_log` (the team's last N games), this spans as far
    back as needed to gather up to ``window`` starts — or sub cameos — so
    filtering the game log to "Subs" shows 20 sub appearances, not just the few
    that happened to fall in the last 20 team games.
    """
    cfg = cfg or MODEL
    if appearances is None or appearances.empty:
        return []
    return _LogIndex(appearances, events, cfg).role_log(player, team, role, window)
