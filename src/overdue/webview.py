"""Self-contained interactive HTML for every report format.

One page style shared by the daily, weekly and late-target reports: a ranked
table per section where **clicking a player row expands their game log** — their
team's last 20 matches with minutes, whether they started / came on / sat out,
and whether they were carded. Everything (data, CSS, a few lines of JS) is inline
so the file opens offline in any browser with no dependencies.

The section tables reuse the same column definitions as the text renderer
(``render_text._COLUMNS`` / ``_TARGET_COLUMNS``), so the HTML and Markdown never
drift.
"""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

from . import config
from . import h2h as h2h_mod
from .config import LEAGUE_BY_ID
from .render_text import COLUMN_HELP
from .targets import PlayerDetail


def _row_league(r) -> str:
    """Short competition label for a row, for the league filter.

    Prefers the competition of the fixture being played (e.g. UCL for Arsenal v
    Lille) over the player's domestic league, so filtering by EPL doesn't keep
    European ties.
    """
    comp = getattr(r, "competition", None)
    if comp:
        return str(comp)
    short = getattr(r, "league_short", None)
    if short:
        return str(short)
    lid = str(getattr(r, "league", "") or "")
    lg = LEAGUE_BY_ID.get(lid)
    return lg.short if lg is not None else lid


def _row_attrs(r) -> str:
    """Filter data-attributes for a table row (league / position / plays-today)."""
    parts = [f'data-player="{html.escape(getattr(r, "player", ""))}"']
    team = getattr(r, "team", "") or ""
    if team:
        parts.append(f'data-team="{html.escape(team)}"')
    league = _row_league(r)
    if league:
        parts.append(f'data-league="{html.escape(league)}"')
    pos = getattr(r, "position", "") or ""
    if pos:
        parts.append(f'data-pos="{html.escape(pos)}"')
    nd = getattr(r, "next_days", None)
    if nd is not None:
        parts.append(f'data-nextday="{nd}"')
        parts.append(f'data-nextlabel="{html.escape(getattr(r, "next_label", "") or "—")}"')
    return " ".join(parts)


def _th(label: str, num: bool = False, sortable: bool = False) -> str:
    """A header cell with a plain-English tooltip from COLUMN_HELP."""
    tip = html.escape(COLUMN_HELP.get(label, ""))
    cls = ("num" if num else "txt") + (" sortable" if sortable else "")
    attrs = f' title="{tip}"' if tip else ""
    body = html.escape(label)
    if sortable:
        # Sortable (main-table) headers are draggable (reorder) + carry a stable
        # data-label and an ✕ to hide the column for this session (Reset restores).
        attrs += (f' data-type="{"num" if num else "txt"}" draggable="true"'
                  f' data-label="{html.escape(label)}"')
        body += '<span class="colx" title="Hide this column (Reset restores it)">×</span>'
    return f'<th class="{cls}"{attrs}>{body}</th>'


_FILTER_BTNS = (
    '<span class="filters">'
    '<button type="button" data-f="all" class="on">All</button>'
    '<button type="button" data-f="Start">Starts</button>'
    '<button type="button" data-f="Sub">Subs</button></span>'
)


def _brk_view(detail: PlayerDetail, view: str) -> str:
    """One breakdown table for a role view: 'all', 'Start' or 'Sub'."""
    body = []
    for b in detail.breakdown:
        if view == "all":
            body.append(
                f"<tr><td>{html.escape(b.season)}</td><td>{html.escape(b.competition)}</td>"
                f'<td class="num">{b.apps}</td><td class="num">{b.starts}</td>'
                f'<td class="num">{b.subs}</td><td class="num">{b.yellows}</td>'
                f'<td class="num">{b.reds}</td></tr>'
            )
        elif view == "Start":
            body.append(
                f"<tr><td>{html.escape(b.season)}</td><td>{html.escape(b.competition)}</td>"
                f'<td class="num">{b.starts}</td><td class="num">{b.start_yellows}</td>'
                f'<td class="num">{b.start_reds}</td></tr>'
            )
        else:  # Sub
            body.append(
                f"<tr><td>{html.escape(b.season)}</td><td>{html.escape(b.competition)}</td>"
                f'<td class="num">{b.subs}</td><td class="num">{b.sub_yellows}</td>'
                f'<td class="num">{b.sub_reds}</td></tr>'
            )
    if view == "all":
        head = (_th("Season") + _th("Comp") + _th("Apps", True) + _th("Starts", True)
                + _th("Subs", True) + _th("Yel", True) + _th("Red", True))
    else:
        count_hdr = "Starts" if view == "Start" else "Subs"
        head = (_th("Season") + _th("Comp") + _th(count_hdr, True)
                + _th("Yel", True) + _th("Red", True))
    return (f'<table class="detail brk-{view}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(body)}</tbody></table>")


def _breakdown_table(detail: PlayerDetail) -> str:
    if not detail.breakdown:
        return ""
    views = _brk_view(detail, "all") + _brk_view(detail, "Start") + _brk_view(detail, "Sub")
    return (
        '<div class="dsect">'
        f'<div class="dhead">By season &amp; competition{_FILTER_BTNS}</div>'
        f'<div class="brk" data-filter="all">{views}</div></div>'
    )


def _games_view(entries: list, view: str) -> str:
    """One game-log table for a role view: 'all', 'Start' or 'Sub'.

    Each view carries its OWN rows: 'all' is the team's last N games (rotation +
    DNP visible), while 'Start'/'Sub' are the player's last N appearances of that
    role — so filtering to Subs shows up to N sub cameos, not just the ones inside
    the team's last N games.
    """
    head = (_th("Date") + _th("Opponent") + _th("Comp") + _th("Score", True)
            + _th("Min", True) + _th("Role") + _th("Card", True))
    if not entries:
        body = f'<tr><td colspan="7" class="empty">No {"" if view == "all" else view.lower()+" "}games in the store.</td></tr>'
    else:
        rows = []
        for e in entries:
            classes = " ".join(c for c in ("carded" if e.card else "",
                                           "dnp" if e.role == "DNP" else "") if c)
            score = f"<b>{e.team_score}</b>–{e.opp_score}" if e.score_known else "—"
            rows.append(
                f'<tr data-role="{e.role}" class="{classes}"><td>{html.escape(e.date)}</td>'
                f"<td>{html.escape(e.opponent or '—')}</td>"
                f"<td>{html.escape(e.competition)}</td>"
                f'<td class="num">{score}</td>'
                f'<td class="num">{html.escape(e.min_display)}</td>'
                f'<td><span class="role role-{e.role.lower()}">{html.escape(e.role)}</span></td>'
                f'<td class="num">{html.escape(e.card_display)}</td></tr>'
            )
        body = "".join(rows)
    return (f'<table class="detail games-{view}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>")


def _games_table(detail: PlayerDetail) -> str:
    if not detail.games and not detail.start_games and not detail.sub_games:
        return ""
    views = (_games_view(detail.games, "all")
             + _games_view(detail.start_games, "Start")
             + _games_view(detail.sub_games, "Sub"))
    return (
        '<div class="dsect">'
        f'<div class="dhead">Recent games{_FILTER_BTNS}'
        '<span class="muted vhint"> · Starts/Subs show the last 20 of that role</span>'
        '</div>'
        f'<div class="games" data-filter="all">{views}</div></div>'
    )


def _summary_line(detail: PlayerDetail) -> str:
    parts = []
    if detail.next_fixture is not None:
        nf = detail.next_fixture
        when = "Plays today" if nf.is_today else f"Next: {html.escape(nf.date_display)}"
        cls = ' class="today"' if nf.is_today else ""
        parts.append(
            f'<span{cls}>{when} vs {html.escape(nf.opponent)} '
            f'({html.escape(nf.competition)})</span>'
        )
    # Last 5 of the team's games (the log is newest-first).
    last5 = detail.games[:5]
    if last5:
        s = sum(1 for e in last5 if e.role == "Start")
        b = sum(1 for e in last5 if e.role == "Sub")
        d = sum(1 for e in last5 if e.role == "DNP")
        parts.append(f"<span>Last 5: {s} started · {b} sub · {d} DNP</span>")
    if not parts:
        return ""
    return f'<div class="summary">{" ".join(parts)}</div>'


def _detail_table(detail: PlayerDetail) -> str:
    if detail is None or (not detail.games and not detail.breakdown):
        return '<div class="empty">No game data in the store for this player.</div>'
    return (
        f'<div class="detailpanel">{_summary_line(detail)}'
        f"{_breakdown_table(detail)}{_games_table(detail)}</div>"
    )


def _book_popover(all_books: dict) -> str:
    """Hidden list of each bookmaker's price, shown when the price cell is clicked.

    Always lists both Bet365 and Sky Bet (— when a book doesn't price him), best
    first; clicking a price puts it in the Best Price box."""
    if not all_books:
        return ""
    items = []
    for book in sorted(("Bet365", "Sky Bet"), key=lambda b: -(all_books.get(b) or 0)):
        price = all_books.get(book)
        chip = _bookie_chip(book)
        if price:
            items.append(f'<button type="button" data-p="{float(price):.2f}">{chip} '
                         f'{float(price):.2f}</button>')
        else:
            items.append(f'<span class="nobook">{chip} —</span>')
    return f'<div class="bookpop" hidden>{"".join(items)}</div>'


def _bookie_chip(source: str) -> str:
    """A small coloured badge naming which bookmaker the cached price is from."""
    s = (source or "").lower()
    if "bet365" in s:
        return '<span class="bk bk-b365" title="Bet365">B365</span>'
    if "sky" in s:
        return '<span class="bk bk-sky" title="Sky Bet">Sky</span>'
    if "best" in s:
        return '<span class="bk bk-best" title="Best price across bookmakers">Best</span>'
    return ""


def _pick_score(r) -> int:
    """Blended 0–100 pick score from a row's signals (report or late target)."""
    fl = min(max(getattr(r, "fouls_per90", 0.0), 0.0) / 3.0, 1.0)
    if hasattr(r, "overdue_ratio"):  # daily/weekly overdue rows
        ov = min(max(r.overdue_ratio, 0.0) / 2.0, 1.0)
        ss = min(max(getattr(r, "start_share", 0.0), 0.0), 1.0)
        rt = min(max(getattr(r, "start_rate", 0.0), 0.0) / 0.4, 1.0)
        h = getattr(r, "h2h_cards", None) or 0.0
        h2 = min(max(h, 0.0) / 6.0, 1.0)
        return round(100 * (0.35 * ov + 0.25 * fl + 0.20 * ss + 0.10 * rt + 0.10 * h2))
    # Late target rows: weight the sample-adjusted cameo rate and overall rate.
    adj = min(max(getattr(r, "adj_sub_rate", 0.0), 0.0) / 0.5, 1.0)
    rt = min(max(getattr(r, "rate", 0.0), 0.0) / 0.4, 1.0)
    return round(100 * (0.45 * adj + 0.25 * rt + 0.30 * fl))


def _fair_odds_of(r, basis: str = "start_rate"):
    """Model fair odds: referee-adjusted if we have it, else 1 / the given rate.

    ``basis`` names which per-appearance rate the shown RecommendedOdds uses —
    ``start_rate`` for the overdue report, ``rate`` for late targets — so Edge
    prices against the same number.
    """
    ra = getattr(r, "ref_adj_odds", None)
    if ra:
        return ra
    v = getattr(r, basis, 0.0)
    return (1.0 / v) if v and v > 0 else None


def _section_table(columns: list, rows: list, logs: dict, extras: set | None = None,
                   fair_basis: str = "start_rate") -> str:
    """One ranked table; each player row toggles a hidden detail row below it.

    ``extras`` adds interactive HTML-only columns: "star" (shortlist), "pick"
    (blended score) and "edge" (a Your-price input plus computed edge).
    """
    extras = extras or set()
    star, pick, edge = ("star" in extras), ("pick" in extras), ("edge" in extras)
    lead = 1 if star else 0
    trail = (1 if pick else 0) + (2 if edge else 0)
    ncols = lead + 1 + len(columns) + trail  # star? + chev + data + trailing

    head = ""
    if star:
        head += '<th class="starcol"></th>'
    head += '<th class="chevcol"></th>'
    head += "".join(_th(h, num=right, sortable=True) for h, right, _ in columns)
    if pick:
        head += _th("Pick", num=True, sortable=True)
    if edge:
        head += _th("Best Price", num=True, sortable=True) + _th("Edge", num=True, sortable=True)

    body = []
    for i, r in enumerate(rows, start=1):
        cells = []
        for h, right, getter in columns:
            val = str(i) if getter is None else str(getter(r))
            data = f' data-sort="{getattr(r, "next_days", 9999)}"' if h == "Next" else ""
            if h == "H2H Cards":
                c, g = getattr(r, "vs_opp_cards", 0), getattr(r, "vs_opp_games", 0)
                # Sort by card rate in the fixture, more meetings breaking ties.
                data = f' data-sort="{(c / g if g else -1) + g / 1000:.4f}"'
                clubs = getattr(r, "vs_opp_clubs", None) or {}
                tip = ", ".join(f"{t} {cc}/{gg}" for t, (cc, gg) in clubs.items())
                level = h2h_mod.h2h_highlight(clubs) if clubs else (
                    "strong" if h2h_mod.is_strong_h2h(c, g) else "")
                if level or len(clubs) > 1:
                    note = {"multi": "Strong card record v this opponent, booked with "
                                     "more than one club",
                            "strong": "Strong card record v this opponent"}.get(level, "")
                    title = html.escape(" · ".join(x for x in (note, tip) if x))
                    cls = {"multi": " h2hmulti", "strong": " h2hhot"}.get(level, "")
                    cells.append(f'<td class="num{cls}" title="{title}"{data}>'
                                 f'{html.escape(val)}</td>')
                    continue
            if h == "Player" and getattr(r, "availability", "") == "warning":
                note = html.escape(getattr(r, "availability_note", "") or "one booking from a ban")
                val = (f'{html.escape(val)} <span class="warn" title="{note}">⚠</span>')
                cells.append(f'<td class="txt"{data}>{val}</td>')
            else:
                cells.append(f'<td class="{"num" if right else "txt"}"{data}>{html.escape(val)}</td>')
        player = getattr(r, "player", "")
        detail = logs.get(player)
        has_log = bool(detail and (detail.games or detail.breakdown))
        chev = '<span class="chev">▸</span>' if has_log else ""

        row = f'<tr class="row{" clickable" if has_log else ""}" {_row_attrs(r)} data-idx="{i}"'
        fair = _fair_odds_of(r, fair_basis)
        if fair:
            row += f' data-fair="{fair:.2f}"'
        fixture = getattr(r, "opponent", None)
        if fixture:
            row += f' data-fixture="{html.escape(getattr(r, "team", ""))} v {html.escape(fixture)}"'
        row += ">"
        if star:
            row += (f'<td class="starcol"><button type="button" class="star" '
                    f'data-key="{html.escape(player)}">☆</button>'
                    f'<button type="button" class="del" data-key="{html.escape(player)}" '
                    f'title="Remove this player (e.g. not in the squad / not on the bench)">✕</button>'
                    f'</td>')
        row += f'<td class="chevcol">{chev}</td>' + "".join(cells)
        if pick:
            sc = _pick_score(r)
            row += f'<td class="num pick" data-sort="{sc}" data-score="{sc}">{sc}</td>'
        if edge:
            bookie = getattr(r, "bookie_odds", None)
            src = getattr(r, "bookie_source", "") or ""
            attrs, cls, chip = "", "price", ""
            psort = "-999"
            if bookie:
                attrs = (f' value="{float(bookie):.2f}" data-bookie="{float(bookie):.2f}"'
                         f' title="{html.escape(src or "bookmaker")} — best of Bet365/Sky '
                         f'Bet (editable)"')
                cls += " prefilled"
                chip = _bookie_chip(src)
                psort = f"{float(bookie):.2f}"
            pop = _book_popover(getattr(r, "bookie_all", None) or {}) if bookie else ""
            if pop:
                cls += " hasbooks"
            row += (f'<td class="num pricecell" data-sort="{psort}">{chip}<input class="{cls}" '
                    f'inputmode="decimal" placeholder="—"{attrs}>{pop}</td>'
                    '<td class="num edge" data-sort="-999">—</td>')
        row += "</tr>"
        body.append(row)
        if has_log:
            body.append(
                f'<tr class="detailrow" hidden><td colspan="{ncols}">'
                f"{_detail_table(logs[player])}</td></tr>"
            )
    return (
        f'<table class="main"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


_CSS = """
:root{
  --bg:#f7f7f8; --panel:#ffffff; --ink:#16181d; --muted:#6b7280; --line:#e5e7eb;
  --accent:#2563eb; --carded:#fde68a; --carded-ink:#7c2d12; --hover:#eef2ff;
  --h2hmulti:#fdba74; --h2hmulti-ink:#7c2d12;
  --dnp:#f3b1b1; --dnp-ink:#7a3535;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg:#0f1115; --panel:#161a21; --ink:#e6e8eb; --muted:#9aa3af; --line:#232833;
  --accent:#60a5fa; --carded:#7c5e12; --carded-ink:#fde68a; --hover:#1b2130;
  --h2hmulti:#9a3412; --h2hmulti-ink:#fed7aa;
  --dnp:#3c1d20; --dnp-ink:#d19ea1;
}}
:root[data-theme="dark"]{
  --bg:#0f1115; --panel:#161a21; --ink:#e6e8eb; --muted:#9aa3af; --line:#232833;
  --accent:#60a5fa; --carded:#7c5e12; --carded-ink:#fde68a; --hover:#1b2130;
  --h2hmulti:#9a3412; --h2hmulti-ink:#fed7aa;
  --dnp:#3c1d20; --dnp-ink:#d19ea1;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1680px;margin:0 auto;padding:24px clamp(16px,3vw,40px) 64px;}
h1{font-size:22px;margin:0 0 4px;}
h2{font-size:16px;margin:28px 0 8px;color:var(--ink);}
.sub{color:var(--muted);font-size:13px;margin-bottom:8px;}
.hint{color:var(--muted);font-size:12.5px;margin:2px 0 12px;}
.notes{display:flex;flex-wrap:wrap;gap:6px 18px;margin:2px 0 14px;font-size:13px;}
.notes span{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:5px 11px;color:var(--ink);}
.controls{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;margin:0 0 14px;
  font-size:12.5px;color:var(--muted);}
.controls select{font:inherit;padding:3px 8px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);}
.msel{position:relative;display:inline-flex;align-items:center;gap:5px;}
.msel details{position:relative;}
.msel summary{list-style:none;cursor:pointer;padding:3px 8px;border-radius:8px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);min-width:60px;}
.msel summary::-webkit-details-marker{display:none;}
.msel summary::after{content:" ▾";color:var(--muted);}
.msel .opts{position:absolute;z-index:20;top:calc(100% + 4px);left:0;min-width:100%;
  max-height:300px;overflow:auto;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:4px 0;box-shadow:0 6px 18px rgba(0,0,0,.12);}
.msel .opts label{display:flex;padding:3px 12px;white-space:nowrap;color:var(--ink);}
.controls label{display:inline-flex;align-items:center;gap:5px;cursor:pointer;}
.daychips{display:inline-flex;flex-wrap:wrap;gap:5px;align-items:center;}
.daychips button{font:inherit;font-size:12px;padding:2px 10px;border-radius:999px;cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);}
.daychips button.on{background:var(--accent);color:#fff;border-color:var(--accent);}
.controls .reset{cursor:pointer;color:var(--accent);border:none;background:none;font:inherit;}
.controls .count{margin-left:auto;}
.presets{display:inline-flex;gap:4px;}
.colmenu{position:relative;display:inline-block;font-size:12px;}
.colmenu summary{cursor:pointer;list-style:none;padding:2px 10px;border:1px solid var(--line);
  border-radius:8px;}
.colmenu summary::after{content:" ▾";}
.colmenu-list{position:absolute;z-index:20;top:120%;left:0;min-width:180px;max-height:60vh;
  overflow:auto;padding:6px 10px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.15);}
.colmenu-list label{display:block;white-space:nowrap;padding:2px 0;cursor:pointer;}
.presets button,.controls .act{font:inherit;font-size:12px;padding:2px 10px;border-radius:8px;
  cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--ink);}
.presets button.on{background:var(--accent);color:#fff;border-color:var(--accent);}
.warn{color:#d97706;cursor:help;}
.starcol{width:42px;text-align:center;white-space:nowrap;}
button.star,button.del{border:none;background:none;cursor:pointer;font-size:13px;color:var(--muted);padding:0 2px;}
button.star.on{color:#f59e0b;}
button.del:hover{color:#dc2626;}
td.pick{font-weight:600;}
td.h2hhot{background:var(--carded);color:var(--carded-ink);font-weight:600;}
td.h2hmulti{background:var(--h2hmulti);color:var(--h2hmulti-ink);font-weight:600;}
input.price{width:56px;font:inherit;font-size:12.5px;text-align:right;padding:2px 6px;
  border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);}
input.price.prefilled{border-color:#3b82f6;border-style:dashed;}
td.pricecell{white-space:nowrap;}
td.pricecell .bk,input.price.hasbooks{cursor:pointer;}
.bookpop{display:flex;flex-direction:column;align-items:flex-end;gap:2px;margin-top:4px;
  padding:4px 6px;background:var(--panel);border:1px solid var(--line);border-radius:8px;}
.bookpop[hidden]{display:none;}
.bookpop button{font:inherit;font-size:12.5px;text-align:right;border:none;background:none;
  color:var(--ink);cursor:pointer;padding:1px 2px;}
.bookpop button:hover{text-decoration:underline;}
.bookpop .nobook{font-size:12.5px;color:var(--muted);text-align:right;}
.bk{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:.02em;
  padding:1px 4px;border-radius:4px;margin-right:4px;vertical-align:middle;color:#fff;}
.bk-b365{background:#14805e;}   /* Bet365 green */
.bk-sky{background:#0a1a5c;}     /* Sky Bet navy */
.bk-best{background:#6b7280;}    /* best across books (bookie unattributed) */
td.edge.pos{color:#16a34a;font-weight:600;}
td.edge.neg{color:#dc2626;}
tr.grouphdr td{background:var(--hover);color:var(--ink);font-weight:600;font-size:12px;
  padding:6px 10px;border-bottom:1px solid var(--line);}
.fixtures{margin:0 0 16px;border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:8px 12px;}
.fixtures summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--ink);}
.fxgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:6px 16px;
  margin-top:8px;font-size:12.5px;color:var(--muted);}
.fxcard{cursor:pointer;border-radius:6px;padding:2px 6px;margin:-2px -6px;
  border:1px solid transparent;}
.fxcard:hover{background:var(--hover,rgba(127,127,127,.08));}
.fxcard.on{border-color:var(--accent);background:var(--hover,rgba(127,127,127,.12));color:var(--ink);}
.fxcard .lg{display:inline-block;min-width:34px;color:var(--accent);font-weight:600;}
.fxcard .fxn{display:inline-block;background:var(--accent);color:#fff;border-radius:999px;
  padding:0 7px;font-size:11px;font-weight:600;}
.teamview{margin:0 0 16px;border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:8px 12px;}
.teamview summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--ink);}
.teamctl{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:10px 0;font-size:12.5px;}
.teamctl select{font:inherit;padding:3px 8px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);}
.teamctl .muted{color:var(--muted);}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel);}
table{border-collapse:collapse;width:100%;font-size:13.5px;}
table.main thead th{position:sticky;top:0;background:var(--panel);text-align:left;
  padding:9px 10px;border-bottom:1px solid var(--line);color:var(--muted);
  font-weight:600;white-space:nowrap;}
th[title]{cursor:help;}
th.sortable{cursor:pointer;}
th.sortable:hover{color:var(--ink);}
th[draggable="true"]:active{cursor:grabbing;}
th.dragover{box-shadow:inset 2px 0 0 var(--accent);}
.colx{margin-left:5px;color:var(--muted);opacity:0;cursor:pointer;font-weight:400;}
th:hover .colx{opacity:.5;}
.colx:hover{opacity:1;color:#dc2626;}
th.sorted-asc::after{content:" ▲";font-size:9px;}
th.sorted-desc::after{content:" ▼";font-size:9px;}
tr.row[hidden]{display:none;}
table.main th.num,table.main td.num{text-align:right;font-variant-numeric:tabular-nums;}
table.main tbody td{padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;}
tr.row.clickable{cursor:pointer;}
tr.row.clickable:hover{background:var(--hover);}
.chevcol{width:22px}
.chev{display:inline-block;color:var(--accent);transition:transform .12s;font-size:11px;}
tr.row.open .chev{transform:rotate(90deg);}
tr.detailrow>td{padding:0 10px 14px 34px;background:var(--panel);}
.detailpanel{display:flex;flex-wrap:wrap;gap:10px 40px;align-items:flex-start;}
.dsect{display:flex;flex-direction:column;align-items:flex-start;}
.summary{flex-basis:100%;display:flex;flex-wrap:wrap;gap:8px 18px;margin:8px 0 2px;font-size:12.5px;}
.summary span{color:var(--muted);}
.summary .today{color:var(--accent);font-weight:600;}
.dhead{color:var(--ink);font-weight:600;font-size:12.5px;margin:6px 0 2px;
  display:flex;align-items:center;gap:10px;}
.filters{display:inline-flex;gap:4px;}
.filters button{font:inherit;font-size:11px;padding:1px 9px;border-radius:999px;cursor:pointer;
  border:1px solid var(--line);background:transparent;color:var(--muted);}
.filters button.on{background:var(--accent);color:#fff;border-color:var(--accent);}
table.detail{width:auto;margin:2px 0 4px;font-size:12.5px;}
table.detail th{color:var(--muted);text-align:left;font-weight:600;padding:5px 14px 5px 0;white-space:nowrap;}
table.detail td{padding:4px 14px 4px 0;border-bottom:1px solid var(--line);white-space:nowrap;}
table.detail td.num{text-align:right;font-variant-numeric:tabular-nums;}
tr.carded{background:var(--carded);}
tr.carded td{color:var(--carded-ink);}
tr.dnp:not(.carded){background:var(--dnp);}
tr.dnp:not(.carded) td{color:var(--dnp-ink);}
table.detail td b{font-weight:700;}
.games table{display:none;}
.games[data-filter="all"] .games-all,
.games[data-filter="Start"] .games-Start,
.games[data-filter="Sub"] .games-Sub{display:table;}
.vhint{font-weight:400;font-size:11px;}
.brk table{display:none;}
.brk[data-filter="all"] .brk-all,
.brk[data-filter="Start"] .brk-Start,
.brk[data-filter="Sub"] .brk-Sub{display:table;}
.role{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);}
.role-start{color:var(--accent);}
.role-dnp{color:var(--muted);}
.empty{color:var(--muted);padding:8px 0;}
footer{color:var(--muted);font-size:12px;margin-top:28px;}
"""

_JS = """
function cellVal(row, idx, type){
  var td = row.children[idx];
  if(!td) return type === 'num' ? -Infinity : '';
  var ds = td.getAttribute('data-sort');
  if(ds !== null){ var f = parseFloat(ds); return isNaN(f) ? ds.toLowerCase() : f; }
  var t = td.textContent.trim();
  if(type === 'num'){ var m = t.match(/-?\\d+(\\.\\d+)?/); return m ? parseFloat(m[0]) : -Infinity; }
  return t.toLowerCase();
}
function sortTable(table, idx, type, asc){
  var tb = table.tBodies[0], kids = Array.prototype.slice.call(tb.children), pairs = [];
  for(var i=0;i<kids.length;i++){
    if(kids[i].classList.contains('row')){
      var pair=[kids[i]];
      if(kids[i+1] && kids[i+1].classList.contains('detailrow')){ pair.push(kids[i+1]); i++; }
      pairs.push(pair);
    }
  }
  pairs.sort(function(a,b){
    var va=cellVal(a[0],idx,type), vb=cellVal(b[0],idx,type);
    if(va<vb) return asc?-1:1; if(va>vb) return asc?1:-1; return 0;
  });
  pairs.forEach(function(p){ p.forEach(function(r){ tb.appendChild(r); }); });
}
// --- Drag a header to reorder columns (persisted across reports) ---
function thLabel(th){ return th.getAttribute('data-label') || th.textContent.replace(/×$/,'').trim(); }
function tableLabels(t){ return [].slice.call(t.tHead.rows[0].cells).map(thLabel); }
// --- Column visibility: a remembered set of hidden column labels ---
// The Default view hides these; the Columns dropdown (or a header's x) changes
// the set, which is remembered in this browser. Reset goes back to Default.
var DEFAULT_HIDDEN=['#','Comp','XI','Rate','CardsPerStart','SubCd','H2H','RecommendedOdds',
  'RefAdjOdds','Edge'];
var COLS_KEY='ov.hidecols';
function hiddenCols(){ var h=LS.get(COLS_KEY); return h===null||h===undefined ? DEFAULT_HIDDEN.slice() : h; }
function applyHidden(hidden){
  document.querySelectorAll('table.main').forEach(function(t){
    [].slice.call(t.tHead.rows[0].cells).forEach(function(th,idx){
      // Star / expand columns carry no data-label and are always shown.
      setColVisible(t, idx, !th.hasAttribute('data-label') || hidden.indexOf(thLabel(th))<0); });
  });
  syncColMenu(hidden); syncTeamColumn();
}
function setHidden(hidden, view){
  LS.set(COLS_KEY, hidden); LS.set(VIEW_KEY, view);
  document.querySelectorAll('.presets button').forEach(function(b){
    b.classList.toggle('on', b.getAttribute('data-view')===view); });
  applyHidden(hidden);
}
function hideColumnByLabel(label){
  var h=hiddenCols(); if(h.indexOf(label)<0) h.push(label); setHidden(h, 'Custom');
}
function showAllColumns(){ setHidden([], 'All'); }
function allColLabels(){
  var seen={}, out=[];
  document.querySelectorAll('table.main thead th[data-label]').forEach(function(th){
    var l=thLabel(th); if(!seen[l]){ seen[l]=1; out.push(l); } });
  return out;
}
function colMenuHtml(){
  return '<details class="colmenu"><summary>Columns</summary><div class="colmenu-list">'
    + allColLabels().map(function(l){
        var e=l.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
        return '<label><input type="checkbox" data-col="'+e+'"> '+e+'</label>'; }).join('')
    + '</div></details>';
}
function syncColMenu(hidden){
  document.querySelectorAll('.colmenu input[data-col]').forEach(function(cb){
    cb.checked = hidden.indexOf(cb.getAttribute('data-col'))<0; });
}
document.addEventListener('change', function(e){
  var cb=e.target.closest && e.target.closest('.colmenu input[data-col]');
  if(!cb) return;
  var h=hiddenCols().filter(function(l){ return l!==cb.getAttribute('data-col'); });
  if(!cb.checked) h.push(cb.getAttribute('data-col'));
  setHidden(h, 'Custom');
});
function reorderTable(t, desired){
  // desired: array of ORIGINAL cell indices in their new order.
  var rows=[t.tHead.rows[0]].concat([].slice.call(t.tBodies[0].rows).filter(function(r){return r.classList.contains('row');}));
  rows.forEach(function(row){
    var cells=[].slice.call(row.children);
    if(cells.length!==desired.length) return;   // skip detail/group rows (colspan)
    var frag=document.createDocumentFragment();
    desired.forEach(function(oi){ frag.appendChild(cells[oi]); });
    row.appendChild(frag);
  });
}
function saveColOrder(t){ try{ window.localStorage.setItem('ov.colorder', JSON.stringify(tableLabels(t))); }catch(e){} }
// Default column order until the user drags their own (star/expand columns first).
var DEFAULT_ORDER=['','Start%','Player','Pos','Team','Opp','Apps','Cards','FlsWon','Fouls90',
  'Carded Every','Cardless run','Overdue','H2H Cards','RefCd','Pick','Best Price','Edge'];
function applyColOrder(){
  var order=null; try{ order=JSON.parse(window.localStorage.getItem('ov.colorder')); }catch(e){}
  if(!order||!order.length) order=DEFAULT_ORDER;
  var rank={}; order.forEach(function(l,i){ if(!(l in rank)) rank[l]=i; });
  document.querySelectorAll('table.main').forEach(function(t){
    var labels=tableLabels(t);
    var desired=labels.map(function(l,i){return {i:i,r:(l in rank)?rank[l]:1000+i};})
      .sort(function(a,b){return a.r-b.r;}).map(function(x){return x.i;});
    reorderTable(t, desired);
  });
}
var _dragCol=null, _dragTable=null;
document.addEventListener('dragstart', function(e){
  var th=e.target.closest('th'); if(!th) return;
  var t=th.closest('table.main'); if(!t) return;
  _dragTable=t; _dragCol=th.cellIndex;
  try{ e.dataTransfer.effectAllowed='move'; e.dataTransfer.setData('text/plain','col'); }catch(_){}
});
document.addEventListener('dragover', function(e){
  var th=e.target.closest('th');
  if(th && _dragTable && th.closest('table.main')===_dragTable && _dragCol!==null){
    e.preventDefault();
    _dragTable.querySelectorAll('th.dragover').forEach(function(x){x.classList.remove('dragover');});
    if(th.cellIndex!==_dragCol) th.classList.add('dragover');
  }
});
document.addEventListener('dragend', function(){
  document.querySelectorAll('th.dragover').forEach(function(x){x.classList.remove('dragover');});
});
document.addEventListener('drop', function(e){
  var th=e.target.closest('th'); if(!th||_dragTable===null||_dragCol===null) return;
  document.querySelectorAll('th.dragover').forEach(function(x){x.classList.remove('dragover');});
  if(th.closest('table.main')!==_dragTable){ _dragCol=null; _dragTable=null; return; }
  e.preventDefault();
  var to=th.cellIndex, from=_dragCol;
  if(from!==to){
    var seq=tableLabels(_dragTable).map(function(_,i){return i;});
    seq.splice(to,0,seq.splice(from,1)[0]);
    reorderTable(_dragTable, seq); saveColOrder(_dragTable); applyColOrder();
  }
  _dragCol=null; _dragTable=null;
});
var CFG = window.OVERDUE_CFG || {};
var LS = { get:function(k){ try{return JSON.parse(localStorage.getItem(k));}catch(e){return null;} },
           set:function(k,v){ try{localStorage.setItem(k,JSON.stringify(v));}catch(e){} } };
var PRICE_KEY = 'ov.price.' + (CFG.date||'x'), STAR_KEY = 'ov.star', VIEW_KEY = 'ov.view2';

document.addEventListener('click', function(e){
  var cx = e.target.closest('.colx');
  if(cx){ e.stopPropagation(); hideColumnByLabel(thLabel(cx.closest('th'))); return; }
  var th = e.target.closest('th.sortable');
  if(th){
    var table = th.closest('table.main'), asc = th.getAttribute('data-asc') !== '1';
    table.querySelectorAll('th.sortable').forEach(function(h){
      h.removeAttribute('data-asc'); h.classList.remove('sorted-asc','sorted-desc'); });
    th.setAttribute('data-asc', asc ? '1':'0');
    th.classList.add(asc ? 'sorted-asc':'sorted-desc');
    sortTable(table, th.cellIndex, th.getAttribute('data-type'), asc);
    return;
  }
  var pb = e.target.closest('.presets button');
  if(pb){ applyPreset(pb.getAttribute('data-view')); return; }
  var star = e.target.closest('button.star');
  if(star){ toggleStar(star.getAttribute('data-key')); return; }
  var del = e.target.closest('button.del');
  if(del){ hideRow(del.getAttribute('data-key')); return; }
  var day = e.target.closest('.daychips button');
  if(day){ day.classList.toggle('on'); applyFilters(); return; }
  var expb = e.target.closest('.act.export');
  if(expb){ exportCsv(); return; }
  var fxc = e.target.closest('.fxcard');
  if(fxc){ toggleFixture(fxc); return; }
  var btn = e.target.closest('.filters button');
  if(btn){
    var box = btn.closest('.dhead').nextElementSibling;
    if(box){ box.setAttribute('data-filter', btn.getAttribute('data-f')); }
    btn.parentNode.querySelectorAll('button').forEach(function(b){
      b.classList.toggle('on', b === btn); });
    return;
  }
  var row = e.target.closest('tr.row.clickable');
  if(!row || e.target.closest('input,button,td.pricecell')) return;
  var d = row.nextElementSibling;
  if(d && d.classList.contains('detailrow')){
    d.hidden = !d.hidden; row.classList.toggle('open', !d.hidden);
  }
});
document.addEventListener('change', function(e){
  if(e.target.id==='f-group'){ setGroupAll(e.target.checked); return; }
  if(e.target.id==='teamsel'){ renderTeam(e.target.value); return; }
  if(e.target.closest('#controls')) applyFilters();
});
function esc(s){ var d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; }
function renderTeam(team){
  // Show the pre-rendered table for the chosen club, hide the rest.
  document.querySelectorAll('.teamtbl').forEach(function(d){
    d.hidden = (d.getAttribute('data-team') !== team);
  });
}
// --- Per-bookmaker prices: click the Best Price cell to see Bet365 and Sky Bet ---
document.addEventListener('click', function(e){
  var opt=e.target.closest('.bookpop button');
  if(opt){
    var oc=opt.closest('td.pricecell'), oi=oc.querySelector('input.price');
    if(oi){ oi.value=opt.getAttribute('data-p');
      oi.dispatchEvent(new Event('input',{bubbles:true})); }
    opt.closest('.bookpop').hidden=true; return;
  }
  var pc=e.target.closest('td.pricecell');
  document.querySelectorAll('.bookpop').forEach(function(p){
    if(!pc || !pc.contains(p)) p.hidden=true; });
  if(pc){ var pop=pc.querySelector('.bookpop'); if(pop) pop.hidden=false; }
});
document.addEventListener('input', function(e){
  if(e.target.id==='teamsel'){ renderTeam(e.target.value); return; }
  if(e.target.classList.contains('price')){
    var row=e.target.closest('tr.row'); computeEdge(row); savePrice(row);
    var vv=document.getElementById('f-value'); if(vv && vv.checked) applyFilters();
  }
});
// --- Edge from your typed price ---
function computeEdge(row){
  var inp=row.querySelector('input.price'), cell=row.querySelector('td.edge');
  if(!inp||!cell) return;
  var price=parseFloat(inp.value), fair=parseFloat(row.getAttribute('data-fair'));
  // Keep the Best Price cell sortable by its typed/cached value.
  var pcell=inp.closest('td.pricecell');
  if(pcell) pcell.setAttribute('data-sort', (price&&price>1)?price.toFixed(2):'-999');
  cell.className='num edge';
  if(!price||price<=1||!fair){ cell.textContent='—'; cell.setAttribute('data-sort','-999'); return; }
  var edge=(1/fair - 1/price)*100;
  cell.textContent=(edge>=0?'+':'')+edge.toFixed(1)+'%';
  cell.setAttribute('data-sort', edge.toFixed(2));
  cell.classList.add(edge>0?'pos':'neg');
}
function savePrice(row){
  var m=LS.get(PRICE_KEY)||{}, k=row.getAttribute('data-player'), v=row.querySelector('input.price').value;
  if(v) m[k]=v; else delete m[k]; LS.set(PRICE_KEY,m);
}
function loadPrices(){
  var m=LS.get(PRICE_KEY)||{};
  document.querySelectorAll('tr.row').forEach(function(r){
    var inp=r.querySelector('input.price'); if(!inp) return;
    var v=m[r.getAttribute('data-player')]; if(v){ inp.value=v; } computeEdge(r);
  });
}
// --- Pick heatmap ---
function heatPick(){
  var cells=[].slice.call(document.querySelectorAll('td.pick'));
  if(!cells.length) return;
  var vals=cells.map(function(c){return +c.getAttribute('data-score');});
  var mx=Math.max.apply(null,vals), mn=Math.min.apply(null,vals);
  cells.forEach(function(c){ var v=+c.getAttribute('data-score');
    var t=(mx>mn)?(v-mn)/(mx-mn):0;
    c.style.background='rgba(34,197,94,'+(0.06+0.5*t).toFixed(3)+')'; });
}
// --- Manual row removal (e.g. players not in the squad / on the bench) ---
var HIDE_KEY = 'ov.hidden.' + (CFG.date||'x');
function hiddenSet(){ return LS.get(HIDE_KEY)||{}; }
function hideRow(k){ var h=hiddenSet(); h[k]=1; LS.set(HIDE_KEY,h); applyFilters(); updateRestore(); }
function restoreHidden(){ LS.set(HIDE_KEY,{}); applyFilters(); updateRestore(); }
function updateRestore(){
  var n=Object.keys(hiddenSet()).length, b=document.getElementById('f-restore');
  if(b){ b.textContent='Restore removed ('+n+')'; b.style.display=n?'':'none'; }
}
// --- Shortlist stars ---
function starred(){ return LS.get(STAR_KEY)||{}; }
function renderStars(){ var s=starred();
  document.querySelectorAll('button.star').forEach(function(b){
    var on=!!s[b.getAttribute('data-key')]; b.textContent=on?'★':'☆'; b.classList.toggle('on',on); }); }
function toggleStar(k){ var s=starred(); if(s[k]) delete s[k]; else s[k]=1; LS.set(STAR_KEY,s);
  renderStars(); applyFilters(); }
// --- Column presets ---
var PRESETS={ All:null,
  Odds:['Rate','CardsPerStart','RecommendedOdds','RefAdjOdds','CardsPerSubCameo','AdjCameo','SubCd'],
  Form:['Fouls90','Cardless run','Carded Every','Overdue','Start%','SubCd'],
  'Ref/H2H':['Opp','Comp','H2H','RefCd','RefAdjOdds'] };
var ALWAYS=['','#','★','Player','Team','Pos','Next','Apps','Cards','Pick','Best Price','Edge'];
function setColVisible(table, idx, vis){ var d=vis?'':'none';
  var th=table.tHead.rows[0].cells[idx]; if(th) th.style.display=d;
  [].slice.call(table.tBodies[0].rows).forEach(function(r){
    if(r.classList.contains('row')){ var c=r.children[idx]; if(c) c.style.display=d; } }); }
function applyPreset(name){
  if(name==='Default'){ setHidden(DEFAULT_HIDDEN.slice(), name); return; }
  if(name==='Custom'){ setHidden(hiddenCols(), name); return; }
  var show=PRESETS[name];
  if(name==='All' || !show){ setHidden([], 'All'); return; }
  setHidden(allColLabels().filter(function(l){
    return ALWAYS.indexOf(l)<0 && show.indexOf(l)<0; }), name); }
// When a single team is selected in the Team filter, the Team column is the same
// value on every visible row, so hide it (it comes back when the filter clears).
function syncTeamColumn(){
  var tv=mselValues('f-team'); var hide=!!(tv && Object.keys(tv).length===1);
  document.querySelectorAll('table.main').forEach(function(t){
    var cells=t.tHead.rows[0].cells;
    for(var i=0;i<cells.length;i++){
      if(thLabel(cells[i])==='Team'){ setColVisible(t, i, !hide); break; } } }); }
// --- Group by fixture ---
function collectPairs(tb){ var kids=[].slice.call(tb.children), out=[];
  for(var i=0;i<kids.length;i++){ if(kids[i].classList.contains('grouphdr')) continue;
    if(kids[i].classList.contains('row')){ var p=[kids[i]];
      if(kids[i+1]&&kids[i+1].classList.contains('detailrow')){ p.push(kids[i+1]); i++; } out.push(p); } }
  return out; }
function setGroupAll(on){
  document.querySelectorAll('table.main').forEach(function(t){
    var tb=t.tBodies[0];
    [].slice.call(tb.querySelectorAll('tr.grouphdr')).forEach(function(x){x.remove();});
    var pairs=collectPairs(tb), ncols=t.tHead.rows[0].cells.length;
    pairs.sort(on ? function(a,b){ var fa=a[0].getAttribute('data-fixture')||'~',fb=b[0].getAttribute('data-fixture')||'~';
        return fa<fb?-1:fa>fb?1:0; }
      : function(a,b){ return (+a[0].getAttribute('data-idx'))-(+b[0].getAttribute('data-idx')); });
    var last=null;
    pairs.forEach(function(p){
      if(on){ var fx=p[0].getAttribute('data-fixture')||'—';
        if(fx!==last){ var hr=document.createElement('tr'); hr.className='grouphdr';
          hr.innerHTML='<td colspan="'+ncols+'">'+fx+'</td>'; tb.appendChild(hr); last=fx; } }
      p.forEach(function(r){ tb.appendChild(r); }); }); }); }
// --- Export ---
function exportCsv(){
  var t=document.querySelector('table.main'); if(!t) return;
  var heads=[].slice.call(t.tHead.rows[0].cells).map(thLabel);
  var lines=[heads.filter(function(h){return h!=='';}).join(',')];
  document.querySelectorAll('tr.row').forEach(function(r){ if(r.hidden) return;
    var cs=[].slice.call(r.children).map(function(c,i){
      if(heads[i]==='') return null;
      var inp=c.querySelector('input'); var v=inp?inp.value:c.textContent.trim();
      return '"'+v.replace(/"/g,'""')+'"'; }).filter(function(x){return x!==null;});
    lines.push(cs.join(',')); });
  var blob=new Blob([lines.join('\\n')],{type:'text/csv'});
  var a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='overdue-'+(CFG.date||'export')+'.csv'; a.click();
}
function uniq(sel){ var s={}; document.querySelectorAll('tr.row').forEach(function(r){
  var v=r.getAttribute(sel); if(v){ v.split(',').forEach(function(x){ x=x.trim(); if(x) s[x]=1; }); } });
  return Object.keys(s).sort(); }
function uniqMain(sel){ var s={}; document.querySelectorAll('tr.row').forEach(function(r){
  if(r.closest('.teamview')) return;   // main section rows only, not the explorer
  var v=r.getAttribute(sel); if(v){ s[v.trim()]=1; } });
  return Object.keys(s).sort(); }
// --- Multi-select dropdowns (Team / League / Pos) ---
function mselHtml(id, label, vals){
  return '<span class="msel">'+label+' <details id="'+id+'"><summary>All</summary><div class="opts">'
    + vals.map(function(x){ return '<label><input type="checkbox" value="'+esc(x)+'"> '+esc(x)+'</label>'; }).join('')
    + '</div></details></span>'; }
function mselValues(id){ var out={}, n=0, el=document.getElementById(id); if(!el) return null;
  el.querySelectorAll('input:checked').forEach(function(c){ out[c.value]=1; n++; });
  return n ? out : null; }   // null = nothing ticked = All
function mselSummary(id){ var el=document.getElementById(id); if(!el) return;
  var v=Object.keys(mselValues(id)||{});
  el.querySelector('summary').textContent = !v.length ? 'All' : (v.length<=2 ? v.join(', ') : v.length+' selected'); }
function mselClear(id){ var el=document.getElementById(id); if(!el) return;
  el.querySelectorAll('input:checked').forEach(function(c){ c.checked=false; }); mselSummary(id); }
document.addEventListener('click', function(e){   // close open dropdowns on outside click
  document.querySelectorAll('.msel details[open]').forEach(function(d){ if(!d.contains(e.target)) d.open=false; }); });
function selectedDays(){
  var set={}; document.querySelectorAll('.daychips button.on').forEach(function(b){
    set[b.getAttribute('data-day')]=1; }); return set;
}
// --- Filter to a single fixture (click a fixture card) ---
var FIXFILTER = null;   // {home, away} or null
function toggleFixture(card){
  var home=card.getAttribute('data-home'), away=card.getAttribute('data-away');
  var same = FIXFILTER && FIXFILTER.home===home && FIXFILTER.away===away;
  FIXFILTER = same ? null : {home:home, away:away};
  document.querySelectorAll('.fxcard').forEach(function(c){
    c.classList.toggle('on', !same && c===card); });
  applyFilters();
}
function edgePct(r){ var e=r.querySelector('td.edge'); if(!e) return null;
  var v=parseFloat(e.getAttribute('data-sort')); return (isFinite(v)&&v>-900)?v:null; }
function applyFilters(){
  ['f-team','f-league','f-pos'].forEach(mselSummary);
  var sl=document.getElementById('f-shortlist'), vv=document.getElementById('f-value');
  var L=mselValues('f-league'), P=mselValues('f-pos'), T=mselValues('f-team'), days=selectedDays(),
      nDays=Object.keys(days).length,
      onlyStar=sl?sl.checked:false, onlyValue=vv?vv.checked:false,
      s=starred(), hid=hiddenSet(), shown=0;
  document.querySelectorAll('tr.row').forEach(function(r){
    if(r.closest('.teamview')) return;   // team explorer rows aren't filtered here
    var team=r.getAttribute('data-team')||'';
    var ep=edgePct(r);
    var ok = !hid[r.getAttribute('data-player')]
      && (!FIXFILTER || team===FIXFILTER.home || team===FIXFILTER.away)
      && (!T || T[team])
      && (!L || L[r.getAttribute('data-league')])
      && (!P || (r.getAttribute('data-pos')||'').split(',').some(function(x){return P[x.trim()];}))
      && (nDays===0 || days[r.getAttribute('data-nextday')])
      && (!onlyStar || s[r.getAttribute('data-player')])
      && (!onlyValue || (ep!==null && ep>0));   // value = best price beats fair odds
    r.hidden = !ok;
    var d=r.nextElementSibling; if(d && d.classList.contains('detailrow')){ d.hidden=true; r.classList.remove('open'); }
    if(ok) shown++;
  });
  var c=document.getElementById('f-count'); if(c) c.textContent = shown + ' shown';
  syncTeamColumn();
}
document.addEventListener('DOMContentLoaded', function(){
  var host=document.getElementById('controls'); if(!host) return;
  var leagues=uniq('data-league'), poss=uniq('data-pos');
  var dayMap={}; document.querySelectorAll('tr.row[data-nextday]').forEach(function(r){
    var k=r.getAttribute('data-nextday'); if(k && k!=='9999') dayMap[k]=r.getAttribute('data-nextlabel')||k; });
  var dayKeys=Object.keys(dayMap).sort(function(a,b){return (+a)-(+b);});
  var h='';
  // Only offer a preset whose columns actually exist in this report's table
  // (e.g. Ref/H2H's Opp/Comp/H2H/Ref columns aren't in report-team / report-late).
  var hdrs={}; document.querySelectorAll('table.main thead th').forEach(function(th){hdrs[thLabel(th)]=1;});
  var views=['Default','All'].concat(['Odds','Form','Ref/H2H'].filter(function(v){
    return (PRESETS[v]||[]).some(function(c){return hdrs[c];}); }));
  h+='<span class="presets">View: '
    + views.map(function(v){
        return '<button type="button" data-view="'+v+'">'+v+'</button>'; }).join('') + '</span>';
  var teams=uniqMain('data-team');
  if(teams.length>1){ h+=mselHtml('f-team','Team',teams); }
  if(leagues.length>1){ h+=mselHtml('f-league','League',leagues); }
  if(poss.length>1){ h+=mselHtml('f-pos','Pos',poss); }
  if(dayKeys.length){ h+='<span class="daychips">Match day: '
    + dayKeys.map(function(k){ return '<button type="button" data-day="'+k+'">'+dayMap[k]+'</button>'; }).join('')
    + '</span>'; }
  if(CFG.group){ h+='<label><input type="checkbox" id="f-group"> Group by fixture</label>'; }
  if(CFG.edge){ h+='<label title="Only players whose best price beats the model\\'s fair odds '
    + '(positive edge). Click the Edge header to sort."><input type="checkbox" id="f-value"> '
    + '\\uD83D\\uDCB0 Value only</label>'; }
  if(CFG.star){ h+='<label><input type="checkbox" id="f-shortlist"> ★ only</label>'
    + '<button type="button" class="act export">Export CSV</button>'; }
  // Restore ✕-removed players — always available (row removal exists on every report).
  h+='<button type="button" class="act" id="f-restore" style="display:none"></button>';
  h+=colMenuHtml();
  h+='<button type="button" class="reset" id="f-reset">Reset</button>';
  h+='<span class="count" id="f-count"></span>';
  host.innerHTML=h;
  var rb=document.getElementById('f-reset');
  if(rb) rb.addEventListener('click', function(){
    ['f-team','f-league','f-pos'].forEach(mselClear);
    host.querySelectorAll('.daychips button.on').forEach(function(b){b.classList.remove('on');});
    FIXFILTER=null; document.querySelectorAll('.fxcard.on').forEach(function(c){c.classList.remove('on');});
    var sl=document.getElementById('f-shortlist'); if(sl) sl.checked=false;
    var vv=document.getElementById('f-value'); if(vv) vv.checked=false;
    var g=document.getElementById('f-group'); if(g && g.checked){ g.checked=false; setGroupAll(false); }
    applyPreset('Default');                   // back to the default columns
    restoreHidden();                          // bring back any ✕-removed players
    applyFilters();
  });
  var rest=document.getElementById('f-restore');
  if(rest) rest.addEventListener('click', restoreHidden);
  renderStars(); loadPrices(); heatPick(); updateRestore(); applyColOrder();
  applyPreset(LS.get(VIEW_KEY) || 'Default');
  applyFilters();
});
"""


def _fixtures_panel(fixtures: list, rows: list, tracked: "dict | set | None" = None) -> str:
    """A scannable overview of today's games with ref, H2H and candidate count.

    ``tracked`` (names of clubs we follow) filters out fixtures where neither
    side is tracked — e.g. FA Cup ties between two non-league clubs — which would
    otherwise clutter the panel without ever holding a candidate.
    """
    if not fixtures:
        return ""
    if tracked:
        from .teamnames import same_club
        names = list(tracked)

        def _is_tracked(club: str) -> bool:
            return any(same_club(club, t) for t in names)

        fixtures = [fx for fx in fixtures
                    if _is_tracked(str(fx.get("home", "")))
                    or _is_tracked(str(fx.get("away", "")))]
        if not fixtures:
            return ""
    by_fixture: dict[str, int] = {}
    for r in rows:
        key = f"{getattr(r, 'team', '')} v {getattr(r, 'opponent', '')}"
        by_fixture[key] = by_fixture.get(key, 0) + 1
    cards = []
    for fx in fixtures:
        home, away = str(fx.get("home", "")), str(fx.get("away", ""))
        n = by_fixture.get(f"{home} v {away}", 0) + by_fixture.get(f"{away} v {home}", 0)
        ref = fx.get("referee") or ""
        ypg = fx.get("referee_ypg")
        ref_txt = (f" · ref {html.escape(ref)}" + (f" ({ypg:.1f})" if ypg is not None else "")) if ref else ""
        h2h = fx.get("h2h_cards")
        h2h_txt = f" · H2H {h2h:.1f}" if h2h is not None else ""
        badge = f'<span class="fxn">{n}</span>' if n else ""
        cards.append(
            f'<div class="fxcard" role="button" tabindex="0" '
            f'data-home="{html.escape(home)}" data-away="{html.escape(away)}">'
            f'<span class="lg">{html.escape(str(fx.get("league","")))}</span> '
            f'{html.escape(home)} v {html.escape(away)}{ref_txt}{h2h_txt} {badge}</div>'
        )
    # Collapsed by default: a full slate (esp. FA Cup days) is long and noisy;
    # the summary stays clickable to expand.
    return (f'<details class="fixtures"><summary>Today\'s fixtures ({len(cards)}) '
            '<span class="muted">(click to expand · click a game to filter)</span></summary>'
            f'<div class="fxgrid">{"".join(cards)}</div></details>')


# Columns shown in the team explorer (a subset of the late-report columns).
def _team_explorer(teams: dict | None, logs: dict | None = None,
                   extras: set | None = None, fair_basis: str = "rate",
                   columns: list | None = None) -> str:
    """Type a club to see EVERY player's full card stats — same columns, odds and
    Edge as the main table, rendered with :func:`_section_table` (so sort, the
    cached-odds Edge input, the star/✕ controls and click-to-expand history all
    work here too). ``extras`` controls its columns; ``logs`` its expandable rows;
    ``columns`` overrides the column set (the late report passes an odds-free one)."""
    if not teams:
        return ""
    from .render_text import _TARGET_COLUMNS

    cols = columns if columns is not None else _TARGET_COLUMNS
    logs = logs or {}
    extras = extras if extras is not None else {"star", "pick", "edge"}
    opts = "".join(f'<option value="{html.escape(t)}"></option>' for t in teams)
    tables = []
    for team, rows in teams.items():
        tbl = _section_table(cols, rows, logs, extras, fair_basis)
        tables.append(f'<div class="teamtbl" data-team="{html.escape(team)}" hidden>'
                      f'<div class="tablewrap">{tbl}</div></div>')
    return (
        '<details class="teamview" open><summary>Team explorer — type a team to see '
        'every player\'s full card stats, odds &amp; Edge (starting &amp; off the bench)'
        '</summary>'
        '<div class="teamctl">Team '
        '<input id="teamsel" list="teamlist" autocomplete="off" '
        'placeholder="Type a team… e.g. Arsenal">'
        f'<datalist id="teamlist">{opts}</datalist>'
        '<span class="muted">Click a column to sort · type a price (or use the cached '
        'one) to see Edge.</span></div>'
        + "".join(tables) + '</details>'
    )


def render_page(
    title: str,
    subtitle: str,
    sections: list[tuple[str, list, list]],
    logs: dict,
    out_path: Path,
    hint: str = "Hover a header for what it means · click to sort · drag a header to reorder "
                "columns · star ☆ to shortlist · ✕ removes a player (e.g. not in the squad) · "
                "type a price to see your Edge · click a player to expand their last 20 games.",
    extras: set | None = None,
    fixtures: list | None = None,
    allow_group: bool = False,
    teams: dict | None = None,
    fair_basis: str = "start_rate",
    team_logs: dict | None = None,
    team_extras: set | None = None,
    team_columns: list | None = None,
    notes: list[str] | None = None,
) -> Path:
    """Write a self-contained HTML page.

    ``sections`` is a list of ``(heading, columns, rows)``. ``extras`` enables the
    interactive columns (see :func:`_section_table`); ``fixtures`` renders an
    overview panel; ``allow_group`` offers a group-by-fixture toggle; ``teams``
    (player rows grouped by club) renders the team explorer, with ``team_logs``
    for its expandable per-player history and ``team_extras`` for its columns.
    """
    extras = extras or set()
    all_rows = [r for _, _, rows in sections for r in rows]
    # Order: today's fixtures, the ranked/overdue section(s), then the team
    # explorer (kept last, directly under the overdue section).
    body_parts = [_fixtures_panel(fixtures or [], all_rows, tracked=teams)]
    if notes:
        items = "".join(f"<span>{html.escape(n)}</span>" for n in notes)
        body_parts.append(f'<div class="notes">{items}</div>')
    for heading, columns, rows in sections:
        body_parts.append(f"<h2>{html.escape(heading)}</h2>")
        if rows:
            body_parts.append(
                f'<div class="tablewrap">'
                f'{_section_table(columns, rows, logs, extras, fair_basis)}</div>')
        else:
            body_parts.append('<div class="hint">No players.</div>')
    body_parts.append(_team_explorer(teams, logs=team_logs,
                                     extras=team_extras if team_extras is not None else extras,
                                     columns=team_columns))

    cfg_js = ("window.OVERDUE_CFG=" + json.dumps({
        "group": bool(allow_group), "star": "star" in extras,
        "edge": "edge" in extras, "date": out_path.stem}) + ";")

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<h1>{html.escape(title)}</h1>
<div class="sub">{html.escape(subtitle)}</div>
<div class="hint">{html.escape(hint)}</div>
<div class="controls" id="controls"></div>
{''.join(body_parts)}
<footer>Card = yellow or red. Line-ups aren't known ahead of time; confirm the player starts.
Your prices, shortlist and view are saved in this browser only. Generated by Overdue.</footer>
</div>
<script>{cfg_js}</script>
<script>{_JS}</script>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


# --- Format-specific wrappers ---------------------------------------------

def render_report_html(report, logs: dict, target_day: date, out_dir: Path | None = None,
                       team_players: dict | None = None, team_logs: dict | None = None) -> Path:
    from .render_text import _COLUMNS

    out_dir = out_dir or config.REPORTS_DIR
    out = out_dir / f"{target_day.isoformat()}.html"
    subtitle = f"{report.target_day} · {len(report.rows)} overdue playing today"
    return render_page("Overdue — Yellow Card Watch", subtitle,
                       [("Overdue today", _COLUMNS, report.rows)], logs, out,
                       extras={"star", "pick", "edge"},
                       fixtures=getattr(report, "fixtures", None), allow_group=True,
                       teams=team_players, team_logs=team_logs)


def render_week_html(day_reports: list, start_day: date, logs: dict,
                     out_dir: Path | None = None, team_players: dict | None = None,
                     team_logs: dict | None = None) -> Path:
    from .render_text import _COLUMNS

    out_dir = out_dir or config.REPORTS_DIR
    out = out_dir / f"week-{start_day.isoformat()}.html"
    sections = [(f"{d.strftime('%A %d %B')} — {len(r.rows)} overdue", _COLUMNS, r.rows)
                for d, r in day_reports]
    subtitle = f"Week of {start_day.strftime('%a %d %b')} · {len(day_reports)} day(s)"
    return render_page("Overdue — Week Ahead", subtitle, sections, logs, out,
                       extras={"star", "pick", "edge"}, teams=team_players,
                       team_logs=team_logs)


def render_targets_html(rows: list, logs: dict, target_day: date,
                        out_dir: Path | None = None, team_players: dict | None = None,
                        team_logs: dict | None = None, title: str = "Late Card Targets",
                        subtitle: str | None = None, heading: str = "Ranked targets",
                        out_name: str | None = None, notes: list[str] | None = None) -> Path:
    from .render_text import _LATE_COLUMNS

    out_dir = out_dir or config.REPORTS_DIR
    out = out_dir / (out_name or f"late-targets-{target_day.isoformat()}.html")
    subtitle = subtitle or f"{target_day.isoformat()} · {len(rows)} late card targets"
    # No odds at all on the late report OR its team explorer — these are in-play
    # sub targets (the players aren't on the pitch), not pre-match bets, so both
    # the fair-odds column and the Best Price / Edge inputs are dropped.
    return render_page(title, subtitle,
                       [(heading, _LATE_COLUMNS, rows)], logs, out,
                       extras={"star", "pick"}, hint="", teams=team_players,
                       fair_basis="rate", team_logs=team_logs,
                       team_extras={"star", "pick"}, team_columns=_LATE_COLUMNS,
                       notes=notes)


def render_team_html(team_players: dict, target_day: date, title: str,
                     team_logs: dict | None = None, fixtures: list | None = None,
                     out_dir: Path | None = None, out_name: str | None = None,
                     notes: list[str] | None = None) -> Path:
    """A stand-alone page for one game's squads (report-team): every player's full
    card stats, odds and Edge, with click-to-expand history — one table per club,
    shown directly (not behind the team-explorer search). ``notes`` are shown as a
    text line above the table (fixture H2H card average, appointed referee)."""
    from .render_text import _TARGET_COLUMNS

    out_dir = out_dir or config.REPORTS_DIR
    out = out_dir / (out_name or f"team-{target_day.isoformat()}.html")
    # Both squads in ONE table (Team column + the Team filter let you show either);
    # ranked by cards then adjusted cameo rate across the combined field.
    all_rows = [r for rows in team_players.values() for r in rows]
    all_rows.sort(key=lambda r: (r.cards, r.adj_sub_rate), reverse=True)
    n = len(all_rows)
    subtitle = f"{target_day.isoformat()} · {len(team_players)} squad(s), {n} players"
    heading = "Both squads" if len(team_players) > 1 else "Squad"
    # Drop Team (there are only two, and the Team filter shows it) and Next (one
    # fixture, so every row is the same day) — they're pure noise on report-team.
    cols = [c for c in _TARGET_COLUMNS if c[0] not in ("Team", "Next")]
    sections = [(f"{heading} — {n} players", cols, all_rows)]
    return render_page(title, subtitle, sections, team_logs or {}, out,
                       extras={"star", "pick", "edge"}, fixtures=fixtures,
                       fair_basis="rate", notes=notes)
