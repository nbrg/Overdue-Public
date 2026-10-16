"""Render the daily report as a Markdown file and a compact terminal summary.

These are the primary local outputs: a dated file you can open/share, and a short
ranked list printed to stdout. The overdue table is laid out as a fixed-width,
column-aligned block (inside a code fence) so it reads cleanly both raw and in a
Markdown viewer. No network, no external delivery.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from . import config
from .report import Report, ReportRow


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _fair_odds(rate: float) -> str:
    """Break-even decimal odds for a per-start card rate (1 ÷ rate).

    A 0.20 cards-per-start rate implies fair odds of 5.00 — anything shorter is
    poor value. Returns "—" when the rate is 0 (no basis for a price).
    """
    if rate and rate > 0:
        return f"{1.0 / rate:.2f}"
    return "—"


# Plain-English help for every column header, surfaced as HTML tooltips. Card =
# yellow or red throughout (the market is "player to be carded").
COLUMN_HELP: dict[str, str] = {
    "#": "Rank in this table.",
    "Player": "Player name (as spelled in the match data).",
    "Pos": "Position: GK/DF/MF/FW. Goalkeepers are excluded.",
    "Team": "The player's club.",
    "Opp": "Today's opponent.",
    "Comp": "Competition of today's fixture (EPL/UCL/…).",
    "XI": "Today's confirmed line-up status: Start (in the XI), Bench, or — "
          "(XI not confirmed yet). Click to sort starters vs bench. Confirmed-out "
          "players are already removed.",
    "Next": "The team's next scheduled game — 'Today' if they play today.",
    "H2H Cards": "Games this player was carded in / games he played against today's opponent "
                 "(any club, all stored seasons). Yellow when 2+ cards in at least half the "
                 "meetings; orange when that record includes cards for more than one club.",
    "H2H": "Average total cards in past meetings of these two teams (number of meetings).",
    "FlsWon": "Fouls the player draws (wins) per 90 minutes — eyeball it against a fixture to "
              "judge who might draw bookings from opponents.",
    "Apps": "Appearances (any minutes) in the baseline window; in brackets, how many were starts.",
    "Cards": "Appearances in which the player was carded (yellow or red).",
    "Rate": "Cards per appearance across all appearances.",
    "Fouls90": "Fouls committed per 90 minutes — the leading indicator of a booking.",
    "CardsPerStart": "Cards per start (started the match; per-start probability of being carded).",
    "CardsPerSubCameo": "Raw cards per substitute appearance (came off the bench).",
    "AdjCameo": "Cameo card rate shrunk toward the league average by sample size — the sort key, "
                "so a proven 5/12 outranks a noisy 2/4.",
    "RecommendedOdds": "Fair decimal odds = 1 ÷ the rate. Anything shorter isn't value.",
    "RefAdjOdds": "RecommendedOdds scaled for the assigned referee's strictness "
                  "(their cards/game vs the league average).",
    "SubCd": "Cards received off the bench / number of sub cameo appearances.",
    "Carded Every": "Average number of appearances between cards (1 ÷ Rate).",
    "Cardless run": "Consecutive most-recent appearances with no card; in brackets, how many were starts.",
    "Overdue": "Cardless run ÷ Carded Every. ~1 is average; higher = more overdue.",
    "Start%": "Share of the team's last 10 games the player started.",
    "RefCd": "The appointed referee's average total cards per game (— until the ref is appointed).",
    "★": "Star a player to add them to your shortlist (saved in this browser).",
    "Pick": "Blended 0–100 pick score from overdue, fouls, start-share, rate and H2H — higher = stronger.",
    "Best Price": "The best decimal odds available (the higher of Bet365/Sky, editable); Edge is computed from it.",
    "Edge": "Your value: model probability vs the price you typed. Green = positive edge.",
    # Detail (game log + season breakdown) headers.
    "Season": "Season (e.g. 26/27).",
    "Date": "Match date.",
    "Opponent": "The opposing team in that match.",
    "Score": "Final score with the player's team's goals in bold (left of the dash).",
    "Min": "Minutes the player was on the pitch.",
    "Role": "Start (in the XI), Sub (came off the bench), or DNP (did not feature).",
    "Card": "Card received in that match: Y (yellow), R (red), Y+R, or — (none).",
    "Starts": "Matches started (≥ the start-minutes threshold).",
    "Subs": "Matches as a sub cameo (under the start-minutes threshold).",
    "Yel": "Matches in which the player was yellow-carded.",
    "Red": "Matches in which the player was sent off.",
}


# (header, right_aligned, value-getter) for each overdue-table column.
_COLUMNS: list[tuple[str, bool, "callable"]] = [
    # Identity & today's fixture
    ("#", True, None),  # rank filled in separately
    ("Player", False, lambda r: r.player),
    ("Pos", False, lambda r: r.position or "—"),
    ("Team", False, lambda r: r.team),
    ("Opp", False, lambda r: r.opponent),
    ("Comp", False, lambda r: r.competition),
    ("XI", False, lambda r: getattr(r, "xi_status", "—") or "—"),
    # Card frequency
    ("Apps", True, lambda r: str(r.appearances)),
    ("Cards", True, lambda r: str(r.cards)),
    ("Rate", True, lambda r: f"{r.rate:.2f}"),
    # Overdue signal
    ("Carded Every", True, lambda r: f"{r.expected_interval:.1f}"),
    ("Cardless run", True, lambda r: f"{r.streak} ({r.streak_starts} st)"),
    ("Overdue", True, lambda r: f"{r.overdue_ratio:.2f}x"),
    ("Start%", True, lambda r: _pct(r.start_share)),
    # Start vs sub splits
    ("CardsPerStart", True, lambda r: f"{r.start_rate:.2f}"),
    ("SubCd", True, lambda r: f"{r.sub_cards}/{r.sub_apps}"),
    # Foul drivers (leading indicators of a booking)
    ("Fouls90", True, lambda r: f"{r.fouls_per90:.1f}" if r.fouls_per90 else "—"),
    ("FlsWon", True, lambda r: f"{r.fouls_drawn_per90:.1f}"
     if getattr(r, "fouls_drawn_per90", 0) else "—"),
    # Match context (referee & history)
    ("RefCd", True, lambda r: f"{r.referee_ypg:.1f}" if r.referee_ypg is not None else "—"),
    ("H2H", True, lambda r: f"{r.h2h_cards:.1f} ({r.h2h_games})" if r.h2h_cards is not None else "—"),
    ("H2H Cards", True, lambda r: f"{r.vs_opp_cards}/{r.vs_opp_games}"
     if getattr(r, "vs_opp_games", 0) else "—"),
    # Fair odds (Best Price / Edge / Pick are appended by the interactive view)
    ("RecommendedOdds", True, lambda r: _fair_odds(r.start_rate)),
    ("RefAdjOdds", True, lambda r: f"{r.ref_adj_odds:.2f}" if r.ref_adj_odds is not None else "—"),
]


def _align(headers: list[str], right: list[bool], matrix: list[list[str]]) -> str:
    """Fixed-width, column-aligned text table from headers + a cell matrix."""
    widths = [len(h) for h in headers]
    for cells in matrix:
        for j, cell in enumerate(cells):
            widths[j] = max(widths[j], len(cell))

    def fmt_row(cells: list[str]) -> str:
        parts = []
        for j, cell in enumerate(cells):
            parts.append(cell.rjust(widths[j]) if right[j] else cell.ljust(widths[j]))
        return "  ".join(parts).rstrip()

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines += [fmt_row(cells) for cells in matrix]
    return "\n".join(lines)


def _aligned_table(rows: list[ReportRow]) -> str:
    """Build a fixed-width, column-aligned text table of the overdue rows."""
    headers = [c[0] for c in _COLUMNS]
    right = [c[1] for c in _COLUMNS]
    matrix: list[list[str]] = []
    for i, r in enumerate(rows, start=1):
        cells = [str(i) if getter is None else getter(r) for _, _, getter in _COLUMNS]
        matrix.append(cells)
    return _align(headers, right, matrix)


def render_markdown(report: Report, target_day: date, out_dir: Path | None = None) -> Path:
    """Write ``reports/<YYYY-MM-DD>.md`` and return its path."""
    out_dir = out_dir or config.REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# Overdue — Yellow Card Watch")
    lines.append("")
    lines.append(f"**{report.target_day}**  ·  generated {report.generated_utc}")
    lines.append("")
    lines.append(
        f"{len(report.rows)} overdue player(s) playing today · "
        f"{report.total_scored} players scored · overdue threshold {report.threshold:.1f}×"
    )
    lines.append("")

    if report.rows:
        lines.append("## Overdue today")
        lines.append("")
        lines.append("```")
        lines.append(_aligned_table(report.rows))
        lines.append("```")
        lines.append("")
        lines.append(
            "_Rate = cards per appearance (any minutes) · Fouls90 = fouls committed per 90 "
            "(leading indicator) · CardsPerStart = cards per start · "
            "RecommendedOdds = fair decimal odds (1 ÷ CardsPerStart); shorter isn't value · "
            "RefAdjOdds = RecommendedOdds scaled for the assigned referee's strictness · "
            "SubCd = cards/appearances off the bench · "
            "Cardless run = games since last card (of which N were starts) · "
            "Carded Every = avg games between cards · "
            "Overdue = Cardless run ÷ Carded Every · Start% = share of last 10 games started · "
            "RefCd = appointed referee's cards/game (— until the ref is appointed) · "
            "H2H = avg cards in past meetings (count). Card = yellow or red._"
        )
        lines.append("")
        warned = [r for r in report.rows if r.availability == "warning"]
        if warned:
            lines.append("**⚠ One booking from a suspension:** "
                         + ", ".join(f"{r.player} ({r.availability_note})" for r in warned))
            lines.append("")
    else:
        lines.append("_No overdue players featuring today._")
        lines.append("")

    if report.fixtures:
        lines.append("## Today's fixtures")
        lines.append("")
        for fx in report.fixtures:
            ref = fx.get("referee") or ""
            ypg = fx.get("referee_ypg")
            ref_txt = ""
            if ref:
                ref_txt = f" — ref {ref}" + (f" ({ypg:.1f} cd/gm)" if ypg is not None else "")
            h2h_avg = fx.get("h2h_cards")
            h2h_n = fx.get("h2h_games") or 0
            h2h_txt = f" · H2H {h2h_avg:.1f} cds ({h2h_n} mtgs)" if h2h_avg is not None else ""
            lines.append(f"- `{fx['league']}` {fx['home']} v {fx['away']}{ref_txt}{h2h_txt}")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Odds are not live — check Sky Bet / Bet365 for a booking price. Line-ups aren't "
        "known at generation time, so this lists overdue **regulars** whose team plays today; "
        "confirm they start (Start%). Postponements are filtered on a best-effort basis and "
        "may lag reality — cross-check the fixtures above._"
    )
    lines.append("")

    out_path = out_dir / f"{target_day.isoformat()}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _overdue_block(report: Report) -> list[str]:
    """The heading + table (or 'none') for one day's overdue players."""
    if report.rows:
        return ["```", _aligned_table(report.rows), "```"]
    return ["_No overdue players featuring._"]


def render_week_markdown(
    day_reports: list[tuple[date, Report]],
    start_day: date,
    out_dir: Path | None = None,
) -> Path:
    """Write ``reports/week-<start>.md`` with one overdue table per day, in order."""
    out_dir = out_dir or config.REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    total = sum(len(r.rows) for _, r in day_reports)
    lines: list[str] = ["# Overdue — Week Ahead", ""]
    lines.append(
        f"**{start_day.strftime('%a %d %b')} – "
        f"{day_reports[-1][0].strftime('%a %d %b %Y') if day_reports else ''}**"
        f"  ·  {total} overdue player-slot(s) across {len(day_reports)} day(s)"
    )
    lines.append("")
    for day, report in day_reports:
        n = len(report.rows)
        lines.append(f"## {day.strftime('%A %d %B')} — {n} overdue")
        lines.append("")
        lines += _overdue_block(report)
        lines.append("")

    lines.append("---")
    lines.append(
        "_Same model as the daily report; a player can appear on multiple days if "
        "their team plays more than once. Line-ups aren't known ahead of time — "
        "these are overdue **regulars** whose team is scheduled to play. Card = "
        "yellow or red._"
    )
    lines.append("")
    out_path = out_dir / f"week-{start_day.isoformat()}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def week_terminal_summary(day_reports: list[tuple[date, Report]]) -> str:
    """Compact per-day overdue tables for stdout."""
    out: list[str] = []
    for day, report in day_reports:
        out.append(f"— {day.strftime('%A %d %b')} ({len(report.rows)} overdue) —")
        if report.rows:
            out.append(_aligned_table(report.rows))
        else:
            out.append("(none)")
        out.append("")
    return "\n".join(out).rstrip()


# Columns for the sub-cameo / team-squad card report. Carries the full picture
# (overdue streak, Start%, cameo split) so the team explorer and report-team show
# every stored field; report-late layers its odds/Edge columns off separately.
_TARGET_COLUMNS: list[tuple[str, bool, "callable"]] = [
    # Identity & next fixture
    ("#", True, None),
    ("Player", False, lambda r: r.player),
    ("Team", False, lambda r: r.team),
    ("Pos", False, lambda r: r.position or "—"),
    ("Next", False, lambda r: r.next_label or "—"),
    # Card frequency
    ("Apps", True, lambda r: f"{r.appearances} ({r.starts} starts)"),
    ("Cards", True, lambda r: str(r.cards)),
    ("Rate", True, lambda r: f"{r.rate:.2f}"),
    # Overdue signal
    ("Carded Every", True, lambda r: f"{r.expected_interval:.1f}"),
    ("Cardless run", True, lambda r: f"{r.streak} ({r.streak_starts} st)"),
    ("Overdue", True, lambda r: f"{r.overdue_ratio:.2f}x"),
    ("Start%", True, lambda r: _pct(r.start_share)),
    # Start vs sub splits
    ("CardsPerStart", True, lambda r: f"{r.start_rate:.2f}"),
    ("CardsPerSubCameo", True, lambda r: f"{r.sub_rate:.2f}"),
    ("AdjCameo", True, lambda r: f"{r.adj_sub_rate:.2f}"),
    ("SubCd", True, lambda r: f"{r.sub_cards}/{r.sub_apps}"),
    # Foul drivers
    ("Fouls90", True, lambda r: f"{r.fouls_per90:.1f}" if r.fouls_per90 else "—"),
    ("FlsWon", True, lambda r: f"{r.fouls_drawn_per90:.1f}"
     if getattr(r, "fouls_drawn_per90", 0) else "—"),
    # Fair odds
    ("RecommendedOdds", True, lambda r: _fair_odds(r.rate)),
]

# The late / sub-cameo report drops the fair-odds column: these are in-play bench
# targets (the players aren't on the pitch), not pre-match bets, so odds aren't
# relevant. report-team keeps the full set (it IS a pre-match view).
# The late report drops fair odds (in-play, not a pre-match bet) and "Next"
# (every row is today's game — it always reads "Today").
_LATE_COLUMNS = [c for c in _TARGET_COLUMNS if c[0] not in ("RecommendedOdds", "Next")]


def _targets_table(rows: list) -> str:
    headers = [c[0] for c in _LATE_COLUMNS]
    right = [c[1] for c in _LATE_COLUMNS]
    matrix: list[list[str]] = []
    for i, r in enumerate(rows, start=1):
        matrix.append([str(i) if g is None else g(r) for _, _, g in _LATE_COLUMNS])
    return _align(headers, right, matrix)


def render_targets_markdown(
    rows: list, target_day: date, out_dir: Path | None = None
) -> Path:
    """Write ``reports/late-targets-<date>.md`` ranking sub-cameo card targets."""
    out_dir = out_dir or config.REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = ["# Late / Sub-Cameo Card Targets", ""]
    lines.append(f"_generated {target_day.isoformat()}_")
    lines.append("")
    lines.append(
        "Players ranked by how often they're **booked after coming on as a sub** "
        "(an appearance under the start-minutes threshold). A card in a cameo is, "
        "by construction, a late-game booking — these are the game-managers and "
        "tactical-foul specialists worth watching whenever they're named on the bench."
    )
    lines.append("")
    if rows:
        lines.append("```")
        lines.append(_targets_table(rows))
        lines.append("```")
        lines.append("")
        lines.append(
            "_Ranked by AdjCameo, highest first. Apps = appearances (starts) · "
            "Rate = cards per appearance · Fouls90 = fouls committed per 90 (leading "
            "indicator) · RecommendedOdds = fair decimal odds (1 ÷ Rate) · "
            "CardsPerStart = cards per start · CardsPerSubCameo = raw cards per sub cameo · "
            "AdjCameo = cameo rate shrunk toward the league average by sample size (the sort "
            "key, so a proven 5/12 outranks a noisy 2/4) · SubCd = cameo cards / cameo "
            "appearances. Card = yellow or red._"
        )
    else:
        lines.append("_No players cleared the sample gate for cameo bookings yet._")
    lines.append("")
    lines.append("---")
    lines.append(
        "_Note: the match data records each player's card count and minutes, but "
        "not the minute a card was shown, so true 'carded after 70'' can't be measured "
        "directly — the sub-cameo signal is the reliable late-card proxy we have._"
    )
    lines.append("")
    out_path = out_dir / f"late-targets-{target_day.isoformat()}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def targets_terminal_summary(rows: list) -> str:
    if not rows:
        return "No players cleared the sample gate for cameo bookings yet."
    return ("Late / sub-cameo card targets  (ranked by AdjCameo — sample-adjusted "
            "cameo card rate, highest first)\n\n" + _targets_table(rows))


def terminal_summary(report: Report) -> str:
    """A compact ranked list for stdout."""
    if not report.rows:
        return (
            f"Overdue — {report.target_day}\n"
            f"No overdue players featuring today "
            f"({report.total_scored} scored, threshold {report.threshold:.1f}×)."
        )

    out = [
        f"Overdue — {report.target_day}  "
        f"({len(report.rows)} playing today of {report.total_scored} scored)",
        "",
        _aligned_table(report.rows),
    ]
    return "\n".join(out)
