"""Interactive HTML rendering — structure checks, no browser."""

from __future__ import annotations

from datetime import date

import pandas as pd

from overdue import webview
from overdue.config import ModelConfig
from overdue.report import build_report
from overdue.targets import game_logs_for, sub_card_targets, team_next_fixtures

CFG = ModelConfig(start_minutes=30, min_appearances=5, min_cards=2, overdue_threshold=1.5)


def _app(player, team, d, minutes, y, gid):
    return {"league": "ENG-Premier League", "season": "2627", "game_id": gid,
            "game": f"{d} {team}", "date": pd.Timestamp(d), "team": team,
            "player": player, "minutes": minutes, "yellow": y, "red": 0}


def test_report_html_has_clickable_player_and_detail(tmp_path):
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
        rows.append(_app("Mate", "Arsenal", f"2026-08-{i + 1:02d}", 90, 0, f"o{i}"))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    nbt = team_next_fixtures(fx, date(2026, 9, 19))
    logs = game_logs_for(apps, report.rows, next_by_team=nbt)
    path = webview.render_report_html(report, logs, date(2026, 9, 19), out_dir=tmp_path)

    assert path.name == "2026-09-19.html"
    html = path.read_text()
    assert "<!doctype html>" in html.lower()
    assert "Overdue Guy" in html
    assert 'data-player="Overdue Guy"' in html
    assert "detailrow" in html                 # expandable detail present
    assert "prefers-color-scheme" in html      # dark-mode aware
    assert "RecommendedOdds" in html           # shares the text columns
    assert "By season" in html                 # season/comp breakdown
    assert 'data-f="Start"' in html            # start/sub filter buttons
    assert "Last 5:" in html                   # last-5 summary
    assert "Plays today" in html               # next-fixture summary (fx is today)
    assert "th.sortable" in html or "sortable" in html   # sortable headers
    assert 'title="' in html                   # column tooltips
    assert 'id="controls"' in html             # filter bar mount point
    assert 'class="dnp"' in html or "dnp" in html        # DNP grey rows possible
    # Interactive daily-report extras.
    assert 'class="star"' in html              # shortlist star
    assert ">Pick<" in html                    # pick-score column
    assert "Best Price" in html and ">Edge<" in html         # price input + edge
    assert 'class="price"' in html             # editable price input
    assert "OVERDUE_CFG" in html               # runtime config (group/star/edge)
    assert "Group by fixture" in html          # group toggle offered
    assert 'id="f-value"' in html and "Value only" in html   # value-picks toggle
    assert "function edgePct" in html          # value filter reads the Edge column
    assert 'class="del"' in html and "hideRow" in html   # manual row removal


def test_targets_html_has_day_filter_attributes(tmp_path):
    from datetime import date
    rows = []
    for i in range(6):
        rows.append(_app("Sub Guy", "Arsenal", f"2026-09-{i + 1:02d}", 15,
                         1 if i < 3 else 0, f"s{i}"))
    for i in range(4):
        rows.append(_app("Sub Guy", "Arsenal", f"2026-10-{i + 1:02d}", 90,
                         1 if i == 0 else 0, f"st{i}"))
    apps = pd.DataFrame(rows)
    sched = pd.DataFrame([{
        "league": "ENG-Premier League", "season": "2627", "home_team": "Arsenal",
        "away_team": "Chelsea", "date": pd.Timestamp("2026-10-10"),
        "played": False, "notes": "",
    }])
    today = date(2026, 10, 5)
    nbt = team_next_fixtures(sched, today)
    tg = sub_card_targets(apps, CFG, min_sub_apps=4, min_sub_cards=2, next_by_team=nbt)
    logs = game_logs_for(apps, tg, next_by_team=nbt)
    path = webview.render_targets_html(tg, logs, today, out_dir=tmp_path)
    html = path.read_text()
    assert 'data-nextday="5"' in html         # Arsenal play in 5 days
    assert 'data-nextlabel=' in html
    assert "daychips" in html                  # day-filter builder present
    assert ">Pick<" in html                    # Pick score kept
    # report-late has NO odds at all (in-play sub targets, not pre-match) — no
    # Best Price / Edge inputs AND no fair-odds column.
    assert ">Best Price<" not in html and ">Edge<" not in html
    assert 'class="price"' not in html
    assert ">RecommendedOdds<" not in html


def test_fixture_cards_are_clickable_filters(tmp_path):
    rows = [_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                 1 if i < 2 else 0, f"o{i}") for i in range(10)]
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(pd.DataFrame(rows), fx, date(2026, 9, 19), CFG)
    path = webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path)
    html = path.read_text()
    assert 'class="fxcard" role="button"' in html    # cards are clickable
    assert 'data-home="Arsenal"' in html and 'data-away="Chelsea"' in html
    assert 'data-team="Arsenal"' in html             # rows carry their team
    assert "function toggleFixture" in html          # click → fixture filter


def test_fixtures_panel_hides_untracked_fixtures():
    # A fixture with a tracked club is kept; one between two non-tracked clubs
    # (e.g. a lower-league FA Cup tie) is dropped from the panel.
    fixtures = [
        {"home": "Arsenal", "away": "Chelsea", "league": "EPL"},
        {"home": "AFC Emley", "away": "Spennymoor", "league": "FA Cup"},
        # Non-league ties whose names merely CONTAIN a tracked club as a substring
        # ("Chester" ⊂ Manchester, "South Liverpool" ⊃ Liverpool) must still drop.
        {"home": "Lower Breck", "away": "Chester", "league": "FA Cup"},
        {"home": "Warrington Town", "away": "South Liverpool", "league": "FA Cup"},
        {"home": "Dover Athletic", "away": "Folkestone Invicta", "league": "FA Cup"},
    ]
    tracked = {"Arsenal", "Chelsea", "Manchester City", "Liverpool", "Athletic Club"}
    html = webview._fixtures_panel(fixtures, [], tracked=tracked)
    assert "Arsenal" in html and "Chelsea" in html
    for junk in ("AFC Emley", "Spennymoor", "Chester", "South Liverpool", "Dover Athletic"):
        assert junk not in html
    assert "Today's fixtures (1)" in html            # only the Arsenal tie survives
    # Without a tracked set, nothing is filtered (back-compat).
    html_all = webview._fixtures_panel(fixtures, [])
    assert "AFC Emley" in html_all


def test_cached_odds_prefill_edge_input(tmp_path):
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    assert report.rows, "expected an overdue row to price"
    report.rows[0].bookie_odds = 3.5
    report.rows[0].bookie_source = "Bet365"
    path = webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path)
    html = path.read_text()
    assert 'class="price prefilled"' in html   # cached price marked
    assert 'value="3.50"' in html              # pre-filled so Edge auto-computes
    assert "Bet365" in html                    # bookmaker named in the tooltip


def test_daily_report_includes_team_explorer(tmp_path):
    from overdue.targets import team_player_rows
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    tp = team_player_rows(apps, CFG)
    path = webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path,
                                      team_players=tp)
    html = path.read_text()
    assert "Team explorer" in html            # explorer now on the daily page
    assert 'id="teamsel"' in html
    assert 'list="teamlist"' in html          # type-to-search input, not a bare select
    assert 'data-team="Arsenal"' in html      # a pre-rendered team table
    assert "RecommendedOdds" in html          # full report columns, not a stripped view


def test_report_team_combined_table_with_team_filter(tmp_path):
    from datetime import date
    from overdue.targets import team_player_rows
    rows = []
    for team in ("Arsenal", "Brighton"):
        for pl in (team + " A", team + " B"):
            for i in range(10):
                rows.append(_app(pl, team, f"2026-09-{i + 1:02d}", 90 if i % 2 else 18,
                                 1 if i < 2 else 0, f"{team}{pl}{i}"))
    apps = pd.DataFrame(rows)
    tp = team_player_rows(apps, CFG)
    path = webview.render_team_html(tp, date(2026, 9, 18), "Arsenal v Brighton — Card Watch",
                                    out_dir=tmp_path,
                                    notes=["H2H: 4.2 cards/game over 6 past meetings",
                                           "Referee: Anthony Taylor — 4.8 cards/game"])
    html = path.read_text(encoding="utf-8")
    # One combined table (not one per team) — heading names both squads.
    assert "Both squads" in html
    assert html.count('<table class="main"') == 1          # single table, not split
    assert 'data-team="Arsenal"' in html and 'data-team="Brighton"' in html
    assert "uniqMain" in html and "f-team" in html         # Team filter wired up
    assert ">Team</th>" not in html and ">Next</th>" not in html   # redundant cols dropped
    assert "applyColOrder" in html and "reorderTable" in html      # drag-to-reorder columns
    assert 'class="colx"' in html and "hideColumnByLabel" in html  # per-column remove ✕
    assert 'class="notes"' in html and "Referee: Anthony Taylor" in html  # H2H/ref text
    # Full field set present (not just the cameo columns).
    assert ">Start%<" in html and ">Overdue<" in html and ">Cardless run<" in html


def test_team_explorer_embeds_all_teams(tmp_path):
    from datetime import date
    from overdue.targets import team_player_rows
    rows = []
    for team in ("Arsenal", "Chelsea"):
        for pi, pl in enumerate([team + " A", team + " B"]):
            for i in range(10):
                rows.append(_app(pl, team, f"2026-09-{i + 1:02d}", 90 if i % 2 else 18,
                                 1 if i <= pi else 0, f"{team}{pi}{i}"))
    apps = pd.DataFrame(rows)
    tp = team_player_rows(apps, CFG)
    path = webview.render_targets_html([], {}, date(2026, 9, 17), out_dir=tmp_path, team_players=tp)
    html = path.read_text()
    assert "Team explorer" in html
    assert 'id="teamsel"' in html
    assert 'class="teamtbl"' in html                       # pre-rendered per-team tables
    assert 'data-team="Arsenal"' in html and 'data-team="Chelsea"' in html
    assert "<title>Late Card Targets</title>" in html
    assert "Hover a column header" not in html   # hint removed


def test_league_filter_uses_todays_competition():
    # Arsenal v Lille is a UCL game: the league filter must say UCL, not EPL.
    from types import SimpleNamespace
    ucl = SimpleNamespace(player="P", competition="UCL", league_short="EPL")
    assert 'data-league="UCL"' in webview._row_attrs(ucl)
    no_fixture = SimpleNamespace(player="Q", competition="", league_short="EPL")
    assert 'data-league="EPL"' in webview._row_attrs(no_fixture)


def test_report_html_odds_can_be_read_back_for_assess(tmp_path):
    # assess recovers a past day's prices from that day's saved report page.
    from overdue import odds_book
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    report.rows[0].bookie_odds = 3.5
    report.rows[0].bookie_source = "Bet365"
    webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path)
    got = odds_book.odds_from_report_html(date(2026, 9, 19), reports_dir=tmp_path)
    assert got["Overdue Guy"] == {"price": 3.5, "book": "Bet365"}
    assert odds_book.odds_from_report_html(date(2026, 9, 20), reports_dir=tmp_path) == {}


def test_price_cell_lists_both_bookmakers(tmp_path):
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    from overdue import odds_book
    idx = {odds_book._player_key("Overdue Guy"): {
        "name": "Overdue Guy", "price": 4.5, "book": "Bet365",
        "all_books": {"Bet365": 4.5, "Sky Bet": 4.33, "William Hill": 5.0},
        "teams": ["Arsenal", "Chelsea"]}}
    assert odds_book.attach_odds(report.rows, idx) == 1
    assert report.rows[0].bookie_all == {"Bet365": 4.5, "Sky Bet": 4.33}   # only our two books
    html = webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path).read_text()
    assert 'class="bookpop" hidden' in html
    assert 'data-p="4.50"' in html and 'data-p="4.33"' in html
    # assess can still read the best price back from the page.
    got = odds_book.odds_from_report_html(date(2026, 9, 19), reports_dir=tmp_path)
    assert got["Overdue Guy"]["price"] == 4.5


def test_h2h_cards_column_shows_record_and_highlights(tmp_path):
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
    # Two earlier meetings with Chelsea, booked in both.
    for i, gid in enumerate(["o0", "o1"]):
        rows.append(_app("Blue Man", "Chelsea", f"2026-08-{i + 1:02d}", 90, 0, gid))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    r = report.rows[0]
    assert (r.vs_opp_cards, r.vs_opp_games) == (2, 2)
    html = webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path).read_text()
    assert 'class="num h2hhot"' in html and ">2/2</td>" in html
    # Visible by default: not in the Default view's hidden list.
    hidden = html.split("var DEFAULT_HIDDEN=")[1].split(";")[0]
    assert "'H2H Cards'" not in hidden


def test_h2h_cards_counts_former_club_and_uses_multi_colour(tmp_path):
    rows = []
    for i in range(10):
        rows.append(_app("Overdue Guy", "Arsenal", f"2026-08-{i + 1:02d}", 90,
                         1 if i < 2 else 0, f"o{i}"))
    rows.append(_app("Blue Man", "Chelsea", "2026-08-01", 90, 0, "o0"))  # booked for Arsenal
    # Last season, for Brentford, also booked against Chelsea.
    rows.append(_app("Overdue Guy", "Brentford", "2025-03-01", 90, 1, "b1"))
    rows.append(_app("Blue Man", "Chelsea", "2025-03-01", 90, 0, "b1"))
    apps = pd.DataFrame(rows)
    fx = pd.DataFrame([{"league": "ENG-Premier League", "season": "2627", "game": "g",
                        "game_id": "fx", "date": pd.Timestamp("2026-09-19"),
                        "home_team": "Arsenal", "away_team": "Chelsea", "played": False}])
    report = build_report(apps, fx, date(2026, 9, 19), CFG)
    r = next(x for x in report.rows if x.player == "Overdue Guy")
    assert (r.vs_opp_cards, r.vs_opp_games) == (2, 2)
    assert r.vs_opp_clubs == {"Arsenal": (1, 1), "Brentford": (1, 1)}
    html = webview.render_report_html(report, {}, date(2026, 9, 19), out_dir=tmp_path).read_text()
    assert 'class="num h2hmulti"' in html and "Arsenal 1/1, Brentford 1/1" in html
