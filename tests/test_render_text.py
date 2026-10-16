"""Tests for the Markdown report and terminal summary rendering."""

from __future__ import annotations

from datetime import date

import pandas as pd

from overdue import render_text
from overdue.config import ModelConfig
from overdue.report import build_report

CFG = ModelConfig(start_minutes=30, min_appearances=10, min_cards=2, overdue_threshold=1.5)


def _overdue_rows(player, team, league, gid_prefix):
    return [
        dict(league=league, season="2627", game_id=f"{gid_prefix}{i}", game=f"g{i}",
             date=pd.Timestamp(f"2026-08-{i + 1:02d}"), team=team, player=player,
             minutes=90, yellow=1 if i < 3 else 0, red=0)
        for i in range(12)
    ]


def _report():
    df = pd.DataFrame(_overdue_rows("Rice", "Arsenal", "ENG-Premier League", "ar"))
    fixtures = pd.DataFrame(
        [dict(league="INT-Champions League", season="2627", game="g", game_id="gx",
              date=pd.Timestamp("2026-09-16"), home_team="Arsenal", away_team="PSV",
              played=False)]
    )
    return build_report(df, fixtures, date(2026, 9, 16), CFG)


def test_markdown_report_written(tmp_path):
    report = _report()
    path = render_text.render_markdown(report, date(2026, 9, 16), out_dir=tmp_path)
    assert path.name == "2026-09-16.md"
    text = path.read_text()
    assert "Overdue — Yellow Card Watch" in text
    assert "Rice" in text
    assert "Comp" in text              # competition column header present
    assert "UCL" in text               # today's fixture competition
    assert "Overdue" in text           # overdue-ratio column
    assert "Arsenal v PSV" in text


def test_terminal_summary_lists_players():
    report = _report()
    summary = render_text.terminal_summary(report)
    assert "Rice" in summary
    assert "UCL" in summary
    assert "Overdue" in summary


def test_recommended_odds_column():
    # CardsPerStart of 0.20 -> RecommendedOdds 5.00 (1 / rate); shorter isn't value.
    assert render_text._fair_odds(0.20) == "5.00"
    assert render_text._fair_odds(0.50) == "2.00"
    assert render_text._fair_odds(0.0) == "—"
    report = _report()
    text = render_text.terminal_summary(report)
    assert "CardsPerStart" in text
    assert "RecommendedOdds" in text
    assert "StRate" not in text  # renamed


def test_terminal_summary_empty():
    df = pd.DataFrame(_overdue_rows("Rice", "Arsenal", "ENG-Premier League", "ar"))
    empty_fx = pd.DataFrame(
        columns=["league", "season", "game", "game_id", "date", "home_team", "away_team", "played"]
    )
    report = build_report(df, empty_fx, date(2026, 9, 16), CFG)
    assert "No overdue players" in render_text.terminal_summary(report)
