"""Assess how well the model predicted cards on a past day.

Given the store *as it is now* (with a past day's results already ingested), this
reconstructs who the model **would have flagged** going into that day — using only
appearances strictly before it — then checks what actually happened: of the
flagged players, who played and who got carded. Bookmakers usually also settle a
"to be carded" bet as a win when the player is substituted off and the player who
replaced him is carded (the "super sub" rule), so that is tracked alongside the
plain hit rate using the substitution events. The per-day record is appended to
``data/assessment_log.jsonl`` so the model can be tuned against real outcomes
later, and compared against the base rate of carding among everyone who played.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from . import config, source, model
from .config import DOMESTIC_LEAGUE_IDS, MODEL, ModelConfig
from .report import build_report

ASSESSMENT_LOG = config.DATA_DIR / "assessment_log.jsonl"


@dataclass
class FlaggedOutcome:
    player: str
    team: str
    opponent: str
    overdue_ratio: float
    rate: float  # the player's normal card rate (cards per appearance)
    start_rate: float
    start_share: float
    streak: int  # cardless run going into the day
    played: bool  # featured (>= 1 minute) on the day
    minutes: int  # minutes played on the day
    role: str  # "Start" | "Sub" | "DNP" on the day
    carded: bool  # yellow or red on the day
    replaced_by: str | None = None  # who came on for him, if subbed off
    sub_carded: bool = False  # the replacement was carded (bookmaker "super sub" win)
    subbed_off: bool = False  # started and went off before 90'
    sub_candidates: list[str] = field(default_factory=list)  # possible replacements if unclear
    sub_maybe_carded: bool = False  # one of several possible replacements was carded
    sub_by_position: bool = False  # same-minute tie settled by position (like for like)
    game_id: str | None = None
    bookie_odds: float | None = None  # logged 'to be carded' price for the day, if any
    bookie_source: str = ""

    @property
    def bet_won(self) -> bool:
        return self.carded or self.sub_carded


@dataclass
class Assessment:
    date: str
    n_flagged: int
    n_played: int
    n_carded: int
    hit_rate: float  # carded / played among flagged
    base_rate: float  # carded / played among all domestic outfielders that day
    lift: float  # hit_rate / base_rate (>1 means the model beat random)
    n_sub_carded: int = 0  # extra wins via the super-sub rule (player himself not carded)
    hit_rate_with_subs: float = 0.0  # (carded + sub carded) / played
    flagged: list[FlaggedOutcome] = field(default_factory=list)


def _day_rows(appearances: pd.DataFrame, target_day: date) -> pd.DataFrame:
    dates = pd.to_datetime(appearances["date"], errors="coerce")
    return appearances[dates.dt.date == target_day]


def _is_carded(r: pd.Series) -> bool:
    return bool((r["yellow"] > 0) or (r["red"] > 0))


def _norm(name) -> str:
    return " ".join(str(name).split()).casefold() if name is not None and not pd.isna(name) else ""


def _names_match(a, b) -> bool:
    """Equal names, tolerating stray text around one of them (e.g. the data's
    event text picking up a "for …" suffix)."""
    na, nb = _norm(a), _norm(b)
    if not (na and nb):
        return False
    short, long_ = sorted((na, nb), key=len)
    return re.search(rf"(?<!\w){re.escape(short)}(?!\w)", long_) is not None


def _replacement_from_events(events: pd.DataFrame | None, player: str, game_id,
                             target_day: date, started: bool) -> str | None:
    """Who came on for ``player``, from the substitution events of his match."""
    if events is None or events.empty:
        return None
    subs = events[events["kind"] == "sub"]
    if game_id is not None and not pd.isna(game_id) and (subs["game_id"] == str(game_id)).any():
        subs = subs[subs["game_id"] == str(game_id)]
    else:
        subs = subs[pd.to_datetime(subs["date"], errors="coerce").dt.date == target_day]
    for _, e in subs.iterrows():
        if _names_match(e["player_off"], player) and _norm(e["player"]):
            return str(e["player"]).strip()
    if started:
        # A starter can only have gone off, so if the event names him as "on",
        # The data listed the pair the other way round: the other name came on.
        for _, e in subs.iterrows():
            if (_names_match(e["player"], player) and _norm(e["player_off"])
                    and not _names_match(e["player_off"], player)):
                return str(e["player_off"]).strip()
    return None


def _match_teammates(day: pd.DataFrame, pr: pd.Series) -> pd.DataFrame:
    """The other rows for ``pr``'s team in the same match on the day."""
    same = day[(day["team"] == pr["team"]) & (day["player"] != pr["player"])]
    if "game_id" in day.columns and pd.notna(pr.get("game_id")):
        same = same[same["game_id"] == pr["game_id"]]
    return same


def _pos_group(positions: dict[str, str], player) -> str:
    """Primary position (DF / MF / FW / GK) for ``player``, or ""."""
    raw = positions.get(str(player), "") if positions else ""
    return raw.replace("-", ",").split(",")[0].strip().upper()


def _bench_candidates(day: pd.DataFrame, pr: pd.Series, tolerance: int = 3,
                      positions: dict[str, str] | None = None) -> tuple[list[str], bool]:
    """Fallback when there are no sub events: work out who replaced ``pr`` from
    minutes alone, pairing *every* substitution of his team in that match.

    Minutes played give each change's timing: a starter who went off at 78' has 77
    minutes and his replacement 90 - 77 = 13 (one either way with rounding). So
    each player who left early is matched, earliest first, to the unclaimed bench
    player whose entry minute (90 - his minutes) is closest to the exit — a later
    exit claims its own replacement instead of both looking plausible. Returns
    one name when the pairing is clear, several when it isn't (e.g. a double
    change at the same minute), or none.

    When a same-minute change is still a tie, positions break it like for like:
    if ``pr`` is the only player of his position group (DF/MF/FW) going off in it
    and exactly one tied replacement shares that group, he's paired with that
    one. The second value says the answer came from that position tie-break."""
    team = day[(day["team"] == pr["team"])]
    if "game_id" in day.columns and pd.notna(pr.get("game_id")):
        team = team[team["game_id"] == pr["game_id"]]
    team = team.drop_duplicates("player")
    started = (pd.to_numeric(team["started"], errors="coerce").fillna(-1)
               if "started" in team.columns else pd.Series(-1, index=team.index))
    # Unknown start flag: treat long stints as starts, short ones as off the bench.
    is_start = (started == 1) | ((started < 0) & (team["minutes"] >= 60))
    is_start |= team["player"] == pr["player"]
    sent_off = team["red"] > 0 if "red" in team.columns else False
    offs = team[is_start & (team["minutes"] < 90) & ~sent_off]
    ons = team[~is_start & (team["minutes"] >= 1)]
    pool = {str(r["player"]): 90 - int(r["minutes"]) for _, r in ons.iterrows()}

    for exit_min, group in offs.groupby("minutes", sort=True):
        k = len(group)
        gaps = sorted((abs(entry - exit_min), name) for name, entry in pool.items())
        gaps = [(g, n) for g, n in gaps if g <= tolerance]
        if not gaps:
            chosen: list[str] = []
        else:
            cutoff = gaps[min(k, len(gaps)) - 1][0]
            chosen = [n for g, n in gaps if g <= cutoff]
        for n in chosen:
            pool.pop(n, None)
        if pr["player"] in set(group["player"].astype(str)):
            if len(chosen) > 1 and positions:
                mine = _pos_group(positions, pr["player"])
                same_off = [p for p in group["player"].astype(str)
                            if _pos_group(positions, p) == mine]
                same_on = [n for n in chosen if _pos_group(positions, n) == mine]
                if mine and len(same_off) == 1 and len(same_on) == 1:
                    return same_on, True
            return chosen, False
    return [], False


def assess_day(
    appearances: pd.DataFrame,
    schedule: pd.DataFrame | None,
    target_day: date,
    positions: dict[str, str] | None = None,
    cfg: ModelConfig | None = None,
    events: pd.DataFrame | None = None,
    odds_index: dict[str, dict] | None = None,
    report_odds: dict[str, dict] | None = None,
) -> Assessment:
    """Reconstruct the flagged set for ``target_day`` and score it against reality.

    ``events`` (the card/substitution event store) lets a flagged player who was
    subbed off also be credited when his replacement was carded. ``odds_index``
    (the logged odds for the day) adds each flagged player's card price.
    """
    cfg = cfg or MODEL
    positions = positions or {}
    ts = pd.Timestamp(target_day)
    dates = pd.to_datetime(appearances["date"], errors="coerce")

    before = appearances[dates < ts]  # knowledge available going into the day
    day = _day_rows(appearances, target_day)

    if schedule is not None and not getattr(schedule, "empty", True):
        day_fx = source.fixtures_from_schedule(schedule, target_day)
    else:
        day_fx = appearances.iloc[0:0]  # empty; build_report tolerates it

    report = build_report(before, day_fx, target_day, cfg,
                          schedule=schedule, positions=positions)
    if odds_index:
        from .odds_book import attach_odds
        attach_odds(report.rows, odds_index)

    # Actual outcome per player on the day (first appearance row wins).
    day_by_player: dict[str, pd.Series] = {}
    for _, r in day.iterrows():
        day_by_player.setdefault(str(r["player"]), r)

    outcomes: list[FlaggedOutcome] = []
    for row in report.rows:
        pr = day_by_player.get(row.player)
        minutes = int(pr["minutes"]) if pr is not None else 0
        started_val = int(pr["started"]) if (pr is not None and "started" in pr) else -1
        played = minutes >= 1
        if not played:
            role = "DNP"
        elif model.is_started_value(started_val, minutes, cfg):
            role = "Start"
        else:
            role = "Sub"
        carded = played and _is_carded(pr)
        replaced_by = None
        sub_carded = False
        candidates: list[str] = []
        sub_maybe_carded = False
        sub_by_position = False
        is_start = role == "Start"
        subbed_off = is_start and minutes < 90
        if played:
            replaced_by = _replacement_from_events(events, row.player, pr.get("game_id"),
                                                   target_day, is_start)
            if replaced_by is None and subbed_off:
                candidates, by_position = _bench_candidates(day, pr, positions=positions)
                if len(candidates) == 1:
                    replaced_by = candidates[0]
                    sub_by_position = by_position
                elif candidates:
                    maybe = [day_by_player.get(c) for c in candidates]
                    sub_maybe_carded = any(m is not None and _is_carded(m) for m in maybe)
            if replaced_by is not None:
                rep = day_by_player.get(replaced_by)
                if rep is None:
                    rep = next((r for n, r in day_by_player.items()
                                if _names_match(n, replaced_by)), None)
                if rep is not None:
                    sub_carded = _is_carded(rep)
                elif events is not None:
                    rep_ev = events[events["player"].map(lambda n: _names_match(n, replaced_by))
                                    & events["kind"].isin(["yellow", "red", "second_yellow"])
                                    & (pd.to_datetime(events["date"], errors="coerce").dt.date
                                       == target_day)]
                    sub_carded = not rep_ev.empty
        outcomes.append(FlaggedOutcome(
            player=row.player, team=row.team, opponent=row.opponent,
            overdue_ratio=round(row.overdue_ratio, 3), rate=round(row.rate, 3),
            start_rate=round(row.start_rate, 3), start_share=round(row.start_share, 3),
            streak=row.streak, played=played, minutes=minutes, role=role, carded=carded,
            replaced_by=replaced_by, sub_carded=sub_carded and not carded,
            subbed_off=subbed_off,
            sub_candidates=candidates if replaced_by is None else [],
            sub_maybe_carded=sub_maybe_carded and not carded,
            sub_by_position=sub_by_position,
            game_id=None if pr is None or pd.isna(pr.get("game_id")) else str(pr.get("game_id")),
            bookie_odds=(getattr(row, "bookie_odds", None)
                         or (report_odds or {}).get(row.player, {}).get("price")),
            bookie_source=(getattr(row, "bookie_source", "")
                           or (report_odds or {}).get(row.player, {}).get("book", "") or ""),
        ))

    n_flagged = len(outcomes)
    n_played = sum(o.played for o in outcomes)
    n_carded = sum(o.carded for o in outcomes)
    hit_rate = (n_carded / n_played) if n_played else 0.0
    n_sub_carded = sum(o.sub_carded for o in outcomes)
    hit_rate_with_subs = ((n_carded + n_sub_carded) / n_played) if n_played else 0.0

    # Base rate: fraction of domestic outfielders who played that day and were carded.
    dom = day[day["league"].isin(DOMESTIC_LEAGUE_IDS) & (day["minutes"] >= 1)]
    if positions:
        # dtype=bool: on a day with no domestic rows, map() returns an empty object
        # Series, which pandas treats as a column selector and drops every column.
        is_gk = dom["player"].map(
            lambda p: positions.get(str(p), "").upper().startswith("GK")).astype(bool)
        dom = dom[~is_gk]
    total = dom["player"].nunique()
    carded_players = dom[(dom["yellow"] > 0) | (dom["red"] > 0)]["player"].nunique()
    base_rate = (carded_players / total) if total else 0.0
    lift = (hit_rate / base_rate) if base_rate else 0.0

    return Assessment(
        date=target_day.isoformat(), n_flagged=n_flagged, n_played=n_played,
        n_carded=n_carded, hit_rate=round(hit_rate, 3), base_rate=round(base_rate, 3),
        lift=round(lift, 2), n_sub_carded=n_sub_carded,
        hit_rate_with_subs=round(hit_rate_with_subs, 3), flagged=outcomes,
    )


def explain_subs(appearances: pd.DataFrame, events: pd.DataFrame | None,
                 result: Assessment) -> str:
    """Why each subbed-off flagged player's replacement was or wasn't found: the
    sub events stored for his match and his team's other players that day."""
    target_day = date.fromisoformat(result.date)
    day = _day_rows(appearances, target_day)
    out = ["", "Substitution check (players who went off before 90'):"]
    for o in result.flagged:
        if not o.subbed_off:
            continue
        out.append(f"  {o.player} ({o.team}, off after {o.minutes}') → "
                   f"{o.replaced_by or 'replacement not identified'}")
        if events is None or events.empty:
            out.append("    events: event store is empty")
        else:
            game_ev = events[events["game_id"] == str(o.game_id)] if o.game_id else events.iloc[0:0]
            subs = game_ev[game_ev["kind"] == "sub"]
            out.append(f"    events for this match (game_id {o.game_id}): {len(game_ev)} total, "
                       f"{len(subs)} subs")
            for _, e in subs.iterrows():
                out.append(f"      {e['minute']}' {e['team']}: on {e['player']!r}, "
                           f"off {e['player_off']!r}")
        pr = day[day["player"] == o.player]
        if pr.empty:
            continue
        mates = _match_teammates(day, pr.iloc[0])
        rest = 90 - o.minutes
        out.append(f"    team-mates in this match (looking for ~{rest} min off the bench):")
        for _, m in mates.sort_values("minutes").iterrows():
            started = m["started"] if "started" in m else "?"
            card = " carded" if _is_carded(m) else ""
            out.append(f"      {m['player']:<24} min {int(m['minutes']):>3}  started {started}{card}")
    return "\n".join(out)


def append_log(result: Assessment, path: Path | None = None) -> Path:
    """Append the assessment as one JSON line for later model tuning."""
    path = path or ASSESSMENT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    return path


def summary_text(result: Assessment) -> str:
    r = result
    lines = [
        f"Model assessment — {r.date}",
        f"  flagged: {r.n_flagged} · played: {r.n_played} · carded: {r.n_carded}",
        f"  hit rate (carded/played): {r.hit_rate:.0%}",
        f"  base rate (all who played): {r.base_rate:.0%}   →   lift: {r.lift:.2f}×",
    ]
    if r.n_played:
        lines.insert(3, f"  incl. super sub (replacement carded): {r.hit_rate_with_subs:.0%}"
                        f"   (+{r.n_sub_carded} bet{'s' if r.n_sub_carded != 1 else ''} won via sub)")
    if not r.flagged:
        return "\n".join(lines)

    def result_label(o: FlaggedOutcome) -> str:
        if o.carded:
            return "✓ carded"
        if o.sub_carded:
            how = ", same position" if o.sub_by_position else ""
            return f"✓ sub carded ({o.replaced_by}{how})"
        if o.played and o.subbed_off and not o.replaced_by:
            if o.sub_maybe_carded:
                return f"? sub maybe carded ({' or '.join(o.sub_candidates)})"
            if not o.sub_candidates:
                return "· no card (sub unknown)"
        return "· no card" if o.played else "– did not play"

    headers = ["Player", "Team", "Opp", "Min", "Overdue", "Streak", "Rate", "Odds", "Result"]
    right = [False, False, False, True, True, True, True, True, False]

    def cells(o: FlaggedOutcome) -> list[str]:
        odds = f"{o.bookie_odds:.2f}" if o.bookie_odds else "—"
        return [o.player, o.team, o.opponent, str(o.minutes),
                f"{o.overdue_ratio:.2f}", str(o.streak), f"{o.rate:.2f}", odds, result_label(o)]

    order = sorted(r.flagged, key=lambda x: x.overdue_ratio, reverse=True)  # most overdue first
    starters = [cells(o) for o in order if o.role != "Sub"]
    subs = [cells(o) for o in order if o.role == "Sub"]
    # One shared set of widths so the two tables line up.
    widths = [len(h) for h in headers]
    for row_cells in starters + subs:
        for j, c in enumerate(row_cells):
            widths[j] = max(widths[j], len(c))

    def fmt(row_cells: list[str]) -> str:
        return "  " + "  ".join(
            c.rjust(widths[j]) if right[j] else c.ljust(widths[j])
            for j, c in enumerate(row_cells)
        ).rstrip()

    def table(title: str, rows: list[list[str]]) -> list[str]:
        if not rows:
            return []
        return ["", f"  {title}", fmt(headers), fmt(["-" * w for w in widths])] + [
            fmt(c) for c in rows]

    lines += table(f"Started / did not play ({len(starters)})", starters)
    lines += table(f"Came off the bench ({len(subs)})", subs)
    return "\n".join(lines)
