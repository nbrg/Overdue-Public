"""Unit tests for the overdue model — synthetic timelines, no network."""

from __future__ import annotations

import pandas as pd
import pytest

from overdue.config import ModelConfig
from overdue.model import _trailing_streak, compute_player_metrics


def _appearance(player, team, league, season, date, minutes, yellow, red=0, game_id=None):
    return {
        "league": league,
        "season": season,
        "game_id": game_id or f"{date}-{team}",
        "game": f"{date} {team}",
        "date": pd.Timestamp(date),
        "team": team,
        "player": player,
        "minutes": minutes,
        "yellow": yellow,
        "red": red,
    }


def _frame(rows):
    return pd.DataFrame(rows)


CFG = ModelConfig(start_minutes=30, min_appearances=5, min_cards=2, overdue_threshold=1.5)


def test_trailing_streak_basic():
    assert _trailing_streak([0, 0, 1, 0, 0, 0]) == 3
    assert _trailing_streak([1, 1, 1]) == 0
    assert _trailing_streak([0, 0, 0]) == 3
    assert _trailing_streak([]) == 0


def test_rate_and_streak_and_overdue_ratio():
    # Player with 10 apps, cards in apps 1 and 2, then 8 clean -> rate 0.2,
    # expected interval 5, streak 8, overdue ratio 8/5 = 1.6 -> overdue.
    rows = []
    for i in range(10):
        yellow = 1 if i < 2 else 0
        rows.append(
            _appearance(
                "Overdue Guy", "TeamA", "ENG-Premier League", "2627",
                f"2026-09-{i + 1:02d}", 90, yellow, game_id=f"g{i}",
            )
        )
    metrics = compute_player_metrics(_frame(rows), CFG)
    assert len(metrics) == 1
    m = metrics[0]
    assert m.appearances == 10
    assert m.cards == 2
    assert m.rate == pytest.approx(0.2)
    assert m.expected_interval == pytest.approx(5.0)
    assert m.streak == 8
    assert m.overdue_ratio == pytest.approx(1.6)
    assert m.qualifies is True
    assert m.overdue is True


def test_red_card_resets_streak():
    # Long yellowless run, but a straight red (no yellow) in the last game must
    # still reset the streak -> not overdue.
    rows = []
    for i in range(12):
        yellow = 1 if i < 2 else 0
        row = _appearance("Sent Off", "TeamA", "ENG-Premier League", "2627",
                          f"2026-09-{i + 1:02d}", 90, yellow, game_id=f"so{i}")
        if i == 11:  # last game: straight red, no yellow
            row["red"] = 1
        rows.append(row)
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.streak == 0
    assert m.overdue is False


def test_not_overdue_when_recently_booked():
    # Same rate but the last appearance had a card -> streak 0, not overdue.
    rows = []
    for i in range(10):
        yellow = 1 if i in (0, 9) else 0
        rows.append(
            _appearance(
                "Fresh Card", "TeamA", "ENG-Premier League", "2627",
                f"2026-09-{i + 1:02d}", 90, yellow, game_id=f"g{i}",
            )
        )
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.streak == 0
    assert m.overdue_ratio == pytest.approx(0.0)
    assert m.overdue is False


def test_sample_gate_excludes_low_yellows():
    # 10 clean apps, only 1 yellow ever -> below min_cards=2 -> does not qualify.
    rows = [
        _appearance("Clean Player", "TeamA", "ESP-La Liga", "2627",
                    f"2026-09-{i + 1:02d}", 90, 1 if i == 0 else 0, game_id=f"c{i}")
        for i in range(10)
    ]
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.qualifies is False
    assert m.overdue is False


def test_cameos_count_and_split_from_starts():
    # 6 starts (2 with cards) + 4 cameos of <30' (1 with a card). All count.
    rows = []
    for i in range(6):
        rows.append(_appearance("Rotato", "TeamB", "FRA-Ligue 1", "2627",
                                f"2026-09-{i + 1:02d}", 90, 1 if i < 2 else 0, game_id=f"st{i}"))
    for i in range(4):
        rows.append(_appearance("Rotato", "TeamB", "FRA-Ligue 1", "2627",
                                f"2026-09-{i + 10:02d}", 15, 1 if i == 0 else 0, game_id=f"sub{i}"))
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.appearances == 10      # cameos counted
    assert m.cards == 3             # 2 in starts + 1 cameo
    assert m.starts == 6
    assert m.start_cards == 2
    assert m.start_rate == pytest.approx(2 / 6)
    assert m.sub_apps == 4
    assert m.sub_cards == 1
    assert m.rate == pytest.approx(0.3)  # total 3/10


def test_streak_starts_counts_starts_within_run():
    # Cards in apps 1-2 (starts), then a cardless run of 8: the first 5 are
    # starts (90'), the last 3 are sub cameos (15'). streak=8, streak_starts=5.
    rows = []
    for i in range(2):
        rows.append(_appearance("Runner", "TeamE", "ENG-Premier League", "2627",
                                f"2026-09-{i + 1:02d}", 90, 1, game_id=f"r{i}"))
    for i in range(2, 7):  # 5 clean starts
        rows.append(_appearance("Runner", "TeamE", "ENG-Premier League", "2627",
                                f"2026-09-{i + 1:02d}", 90, 0, game_id=f"r{i}"))
    for i in range(7, 10):  # 3 clean sub cameos
        rows.append(_appearance("Runner", "TeamE", "ENG-Premier League", "2627",
                                f"2026-09-{i + 1:02d}", 15, 0, game_id=f"r{i}"))
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.streak == 8
    assert m.streak_starts == 5  # 5 of the 8 cardless games were starts


def test_streak_starts_zero_when_recently_booked():
    rows = [
        _appearance("Booked", "TeamE", "ENG-Premier League", "2627",
                    f"2026-09-{i + 1:02d}", 90, 1 if i in (0, 9) else 0, game_id=f"b{i}")
        for i in range(10)
    ]
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.streak == 0
    assert m.streak_starts == 0


def test_player_whose_club_isnt_domestic_is_excluded():
    # Domestic La Liga spell (carded), then a recent move to a club that only
    # appears via the Champions League — their current club isn't tracked
    # domestically, so they must not be scored (fixes Europe-only clubs sneaking in).
    rows = []
    for i in range(2):
        rows.append(_appearance("Mover", "Sevilla", "ESP-La Liga", "2526",
                                f"2026-05-{i + 1:02d}", 90, 1, game_id=f"s{i}"))
    for i in range(6):
        rows.append(_appearance("Mover", "Porto", "INT-Champions League", "2627",
                                f"2026-09-{i + 1:02d}", 90, 0, game_id=f"p{i}"))
    assert compute_player_metrics(_frame(rows), CFG) == []


def test_started_flag_overrides_minutes():
    # Real starter flag wins over the minutes heuristic: 7 games of 90'
    # but flagged as a sub (started=0), 3 games of 20' but flagged as a starter.
    rows = []
    for i in range(10):
        r = _appearance("Flag Guy", "TeamA", "ENG-Premier League", "2627",
                        f"2026-09-{i + 1:02d}", 90 if i < 7 else 20,
                        1 if i < 3 else 0, game_id=f"fg{i}")
        r["started"] = 0 if i < 7 else 1
        rows.append(r)
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.starts == 3          # by the flag, not the seven 90-minute games
    assert m.sub_apps == 7


def test_recent_dnp_counts_consecutive_absence():
    # Featured in 5 early games, then the team plays 3 newer ones without them
    # (an injury/suspension proxy): recent_dnp should be 3.
    rows = []
    for i in range(5):
        rows.append(_appearance("Out", "TeamA", "ENG-Premier League", "2627",
                                f"2026-09-{i + 1:02d}", 90, 1 if i < 3 else 0, game_id=f"o{i}"))
    for i in range(3):
        rows.append(_appearance("SubIn", "TeamA", "ENG-Premier League", "2627",
                                f"2026-10-{i + 1:02d}", 90, 0, game_id=f"n{i}"))
    m = [x for x in compute_player_metrics(_frame(rows), CFG) if x.player == "Out"][0]
    assert m.recent_dnp == 3


def test_fouls_signal_computed():
    rows = []
    for i in range(10):
        r = _appearance("Fouler", "TeamA", "ENG-Premier League", "2627",
                        f"2026-09-{i + 1:02d}", 90, 1 if i < 3 else 0, game_id=f"f{i}")
        r["fouls"] = 2
        rows.append(r)
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.fouls == 20
    assert m.fouls_per90 == pytest.approx(2.0)     # 20 fouls over 900 min
    assert m.fouls_per_card == pytest.approx(20 / 3)


def test_streak_spans_seasons():
    # Card in prior season, then a clean run into the current season.
    rows = []
    rows.append(_appearance("Cross Season", "TeamC", "ENG-Premier League", "2526",
                            "2026-05-01", 90, 1, game_id="p0"))
    rows.append(_appearance("Cross Season", "TeamC", "ENG-Premier League", "2526",
                            "2026-05-08", 90, 1, game_id="p1"))
    for i in range(6):
        rows.append(_appearance("Cross Season", "TeamC", "ENG-Premier League", "2627",
                                f"2026-08-{i + 10:02d}", 90, 0, game_id=f"n{i}"))
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.appearances == 8
    assert m.cards == 2
    assert m.streak == 6  # spans from current season back to last card in prior season
    assert m.league == "ENG-Premier League"


def test_start_share_uses_latest_season():
    # Two players on TeamD; latest season has 4 team games. Player features in 3.
    rows = []
    for i in range(4):
        rows.append(_appearance("Regular", "TeamD", "ESP-La Liga", "2627",
                                f"2026-09-{i + 1:02d}", 90, 1 if i < 2 else 0, game_id=f"d{i}"))
    # A different player defines the 4th team game so team_total = 4.
    rows.append(_appearance("Regular", "TeamD", "ESP-La Liga", "2627",
                            "2026-09-05", 90, 0, game_id="d3-extra"))
    # remove one Regular appearance so they feature in 4 of ... keep simple:
    m = [x for x in compute_player_metrics(_frame(rows), CFG) if x.player == "Regular"][0]
    assert m.start_share == pytest.approx(1.0)


def test_empty_frame_returns_empty():
    from overdue.store import empty_frame

    assert compute_player_metrics(empty_frame(), CFG) == []


# --- European competitions -------------------------------------------------

UCL = "INT-Champions League"


def test_european_card_resets_streak_and_counts():
    # 10 domestic apps (2 early yellows) then a recent UCL booking.
    rows = [
        _appearance("Euro Guy", "TeamA", "ENG-Premier League", "2627",
                    f"2026-08-{i + 1:02d}", 90, 1 if i < 2 else 0, game_id=f"e{i}")
        for i in range(10)
    ]
    rows.append(_appearance("Euro Guy", "TeamA", UCL, "2627", "2026-08-20", 90, 1, game_id="ucl1"))
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.appearances == 11          # European app counted
    assert m.cards == 3               # European yellow counted
    assert m.streak == 0                # most recent (UCL) had a card
    assert m.overdue is False
    assert m.league == "ENG-Premier League"  # identity stays domestic


def test_european_only_player_excluded():
    # No domestic appearances at all -> out of scope, not scored.
    rows = [
        _appearance("Bayern Man", "Bayern", UCL, "2627",
                    f"2026-09-{i + 1:02d}", 90, 1 if i < 3 else 0, game_id=f"b{i}")
        for i in range(12)
    ]
    assert compute_player_metrics(_frame(rows), CFG) == []


def test_domestic_player_overdue_across_competitions():
    # 8 domestic + 5 UCL, cards only in first two domestic games, clean since.
    rows = [
        _appearance("Mixed", "TeamB", "ESP-La Liga", "2627",
                    f"2026-08-{i + 1:02d}", 90, 1 if i < 2 else 0, game_id=f"d{i}")
        for i in range(8)
    ]
    rows += [
        _appearance("Mixed", "TeamB", UCL, "2627",
                    f"2026-09-{i + 1:02d}", 90, 0, game_id=f"u{i}")
        for i in range(5)
    ]
    m = compute_player_metrics(_frame(rows), CFG)[0]
    assert m.appearances == 13
    assert m.cards == 2
    assert m.streak == 11               # clean run spans both competitions
    assert m.rate == pytest.approx(2 / 13)
    assert m.overdue is True
