"""Prediction assessment — reconstruct the flagged set and score it (no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from overdue import assessment
from overdue.config import ModelConfig

CFG = ModelConfig(start_minutes=30, min_appearances=5, min_cards=2, overdue_threshold=1.5,
                  min_start_share=0.0, max_days_since_last_appearance=400,
                  start_share_window=5)


def _app(player, team, league, d, minutes, y, red=0, game_id=None):
    return {"league": league, "season": "2627", "game_id": game_id, "game": f"{d} {team}",
            "date": pd.Timestamp(d), "team": team, "player": player,
            "minutes": minutes, "yellow": y, "red": red}


def test_assess_day_scores_flagged_players():
    target = date(2026, 9, 20)
    rows = []
    # Streaky: 2 early cards then a long clean run -> overdue going into target.
    rows.append(_app("Streaky", "TeamA", "ENG-Premier League", "2026-08-01", 90, 1, game_id="a0"))
    rows.append(_app("Streaky", "TeamA", "ENG-Premier League", "2026-08-08", 90, 1, game_id="a1"))
    for i in range(6):
        rows.append(_app("Streaky", "TeamA", "ENG-Premier League",
                         f"2026-08-{15 + i:02d}", 90, 0, game_id=f"a{i + 2}"))
    # Target day: TeamA play, Streaky features and IS carded (a hit).
    rows.append(_app("Streaky", "TeamA", "ENG-Premier League", "2026-09-20", 90, 1, game_id="d1"))
    # Other domestic players that day (for the base rate): one carded, two not.
    rows.append(_app("OtherA", "TeamB", "ENG-Premier League", "2026-09-20", 90, 1, game_id="d1"))
    rows.append(_app("OtherB", "TeamB", "ENG-Premier League", "2026-09-20", 90, 0, game_id="d1"))
    rows.append(_app("OtherC", "TeamA", "ENG-Premier League", "2026-09-20", 90, 0, game_id="d1"))
    appearances = pd.DataFrame(rows)

    schedule = pd.DataFrame([{
        "league": "ENG-Premier League", "season": "2627", "game": "g", "game_id": "d1",
        "date": pd.Timestamp("2026-09-20"), "home_team": "TeamA", "away_team": "TeamB",
        "referee": "", "notes": "", "played": True,
    }])

    result = assessment.assess_day(appearances, schedule, target, cfg=CFG)
    assert result.n_flagged >= 1
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert streaky.played and streaky.carded
    assert result.n_carded >= 1
    assert result.hit_rate == 1.0            # the only flagged player was carded
    assert 0 < result.base_rate < 1          # 2 of 4 domestic players carded that day
    assert result.lift > 1                    # model beat the base rate


def test_append_log_roundtrip(tmp_path):
    result = assessment.Assessment(
        date="2026-09-20", n_flagged=1, n_played=1, n_carded=1,
        hit_rate=1.0, base_rate=0.5, lift=2.0, flagged=[],
    )
    path = assessment.append_log(result, tmp_path / "log.jsonl")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1 and '"date": "2026-09-20"' in lines[0]
    # A second call appends rather than overwrites.
    assessment.append_log(result, path)
    assert len(path.read_text().strip().splitlines()) == 2


def test_assess_day_with_positions_and_no_domestic_games():
    # International-break day: no domestic rows. With positions supplied, the GK
    # filter used to return an empty object Series that dropped every column,
    # crashing with KeyError: 'player'.
    target = date(2026, 9, 10)
    appearances = pd.DataFrame([
        _app("Streaky", "TeamA", "ENG-Premier League", "2026-08-01", 90, 1, game_id="a0"),
        _app("Streaky", "TeamA", "ENG-Premier League", "2026-09-01", 90, 0, game_id="a1"),
    ])
    result = assessment.assess_day(appearances, None, target,
                                   positions={"Streaky": "DF"}, cfg=CFG)
    assert result.base_rate == 0.0
    assert result.n_played == 0


def test_assess_day_credits_super_sub():
    # Streaky is flagged, plays 60' without a card, and is replaced by Bench, who
    # IS carded — bookmakers pay out on the "super sub" rule.
    target = date(2026, 9, 20)
    rows = []
    rows.append(_app("Streaky", "TeamA", "ENG-Premier League", "2026-08-01", 90, 1, game_id="a0"))
    rows.append(_app("Streaky", "TeamA", "ENG-Premier League", "2026-08-08", 90, 1, game_id="a1"))
    for i in range(6):
        rows.append(_app("Streaky", "TeamA", "ENG-Premier League",
                         f"2026-08-{15 + i:02d}", 90, 0, game_id=f"a{i + 2}"))
    rows.append(_app("Streaky", "TeamA", "ENG-Premier League", "2026-09-20", 60, 0, game_id="d1"))
    rows.append(_app("Bench", "TeamA", "ENG-Premier League", "2026-09-20", 30, 1, game_id="d1"))
    rows.append(_app("OtherB", "TeamB", "ENG-Premier League", "2026-09-20", 90, 0, game_id="d1"))
    appearances = pd.DataFrame(rows)
    schedule = pd.DataFrame([{
        "league": "ENG-Premier League", "season": "2627", "game": "g", "game_id": "d1",
        "date": pd.Timestamp("2026-09-20"), "home_team": "TeamA", "away_team": "TeamB",
        "referee": "", "notes": "", "played": True,
    }])
    events = pd.DataFrame([
        {"game_id": "d1", "date": pd.Timestamp("2026-09-20"), "team": "TeamA",
         "player": "Bench", "player_off": "Streaky", "minute": 60, "kind": "sub"},
        {"game_id": "d1", "date": pd.Timestamp("2026-09-20"), "team": "TeamA",
         "player": "Bench", "player_off": None, "minute": 77, "kind": "yellow"},
    ])

    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, events=events)
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert not streaky.carded
    assert streaky.replaced_by == "Bench" and streaky.sub_carded and streaky.bet_won
    assert result.n_carded == 0 and result.hit_rate == 0.0     # plain rate unchanged
    assert result.n_sub_carded == 1 and result.hit_rate_with_subs == 1.0
    text = assessment.summary_text(result)
    assert "sub carded (Bench)" in text and "incl. super sub" in text

    # Without events, the bench player whose minutes fill the rest of the 90 is
    # still picked up as the replacement.
    plain = assessment.assess_day(appearances, schedule, target, cfg=CFG)
    assert plain.n_sub_carded == 1


def _super_sub_fixture(sub_minutes=13, extra_bench=None):
    target = date(2026, 9, 20)
    rows = [
        _app("Streaky", "TeamA", "ENG-Premier League", "2026-08-01", 90, 1, game_id="a0"),
        _app("Streaky", "TeamA", "ENG-Premier League", "2026-08-08", 90, 1, game_id="a1"),
    ]
    for i in range(6):
        rows.append(_app("Streaky", "TeamA", "ENG-Premier League",
                         f"2026-08-{15 + i:02d}", 90, 0, game_id=f"a{i + 2}"))
    day = [_app("Streaky", "TeamA", "ENG-Premier League", "2026-09-20", 77, 0, game_id="d1"),
           _app("Bench", "TeamA", "ENG-Premier League", "2026-09-20", sub_minutes, 1, game_id="d1"),
           _app("OtherB", "TeamB", "ENG-Premier League", "2026-09-20", 90, 0, game_id="d1")]
    day[0]["started"], day[1]["started"], day[2]["started"] = 1, 0, 1
    for name in extra_bench or []:
        r = _app(name, "TeamA", "ENG-Premier League", "2026-09-20", sub_minutes, 0, game_id="d1")
        r["started"] = 0
        day.append(r)
    for r in rows:
        r["started"] = 1
    schedule = pd.DataFrame([{
        "league": "ENG-Premier League", "season": "2627", "game": "g", "game_id": "d1",
        "date": pd.Timestamp("2026-09-20"), "home_team": "TeamA", "away_team": "TeamB",
        "referee": "", "notes": "", "played": True,
    }])
    return pd.DataFrame(rows + day), schedule, target


def test_super_sub_inferred_from_minutes_without_events():
    appearances, schedule, target = _super_sub_fixture()
    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, events=None)
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert streaky.subbed_off and streaky.replaced_by == "Bench" and streaky.sub_carded


def test_super_sub_ambiguous_minutes_not_credited():
    appearances, schedule, target = _super_sub_fixture(extra_bench=["Bench2"])
    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, events=None)
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert streaky.replaced_by is None and not streaky.sub_carded
    assert "? sub maybe carded (Bench or Bench2)" in assessment.summary_text(result)
    assert result.n_sub_carded == 0                      # not counted: can't tell which
    assert "Bench2" in assessment.explain_subs(appearances, None, result)


def test_super_sub_event_with_reversed_names():
    # The event lists the starter as "on" — a starter can only have gone off.
    appearances, schedule, target = _super_sub_fixture(sub_minutes=20)  # minutes won't fit
    events = pd.DataFrame([{"game_id": "d1", "date": pd.Timestamp("2026-09-20"),
                            "team": "TeamA", "player": "Streaky", "player_off": "Bench",
                            "minute": 77, "kind": "sub"}])
    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, events=events)
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert streaky.replaced_by == "Bench" and streaky.sub_carded


def test_summary_splits_subs_and_shows_overdue_and_odds():
    def o(player, role, odds=None):
        return assessment.FlaggedOutcome(
            player=player, team="T", opponent="O", overdue_ratio=2.564, rate=0.2,
            start_rate=0.2, start_share=1.0, streak=10, played=True, minutes=90 if role == "Start" else 20,
            role=role, carded=False, bookie_odds=odds)
    result = assessment.Assessment(date="2026-09-20", n_flagged=2, n_played=2, n_carded=0,
                                   hit_rate=0.0, base_rate=0.1, lift=0.0,
                                   flagged=[o("Starter", "Start", 4.5), o("Benchy", "Sub")])
    text = assessment.summary_text(result)
    starters, bench = text.split("Came off the bench (1)")
    assert "Starter" in starters and "Benchy" not in starters
    assert "Benchy" in bench
    assert "2.56" in text and "4.50" in text and "Overdue" in text and "Odds" in text


def test_assess_day_attaches_logged_odds():
    appearances, schedule, target = _super_sub_fixture()
    from overdue import odds_book as od
    idx = {od._player_key("Streaky"): {"name": "Streaky", "price": 3.25, "book": "Bet365",
                                       "teams": ["TeamA", "TeamB"]}}
    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, odds_index=idx)
    streaky = next(x for x in result.flagged if x.player == "Streaky")
    assert streaky.bookie_odds == 3.25


def test_ambiguous_subs_none_carded_shows_plain_no_card():
    o = assessment.FlaggedOutcome(
        player="Dias", team="T", opponent="O", overdue_ratio=1.65, rate=0.14, start_rate=0.14,
        start_share=1.0, streak=12, played=True, minutes=45, role="Start", carded=False,
        subbed_off=True, sub_candidates=["A", "B"], sub_maybe_carded=False)
    r = assessment.Assessment(date="2026-10-14", n_flagged=1, n_played=1, n_carded=0,
                              hit_rate=0.0, base_rate=0.1, lift=0.0, flagged=[o])
    line = next(l for l in assessment.summary_text(r).splitlines() if "Dias" in l)
    assert line.rstrip().endswith("· no card")


def test_minutes_pairing_finds_exact_sub_when_two_come_on_a_minute_apart():
    # Cucurella off at 78' (77 min) for Carreras (carded); another starter off at
    # 79' (78 min) for Espí. Carreras and Espí both "roughly fit" Cucurella, but
    # pairing every change in order settles it.
    appearances, schedule, target = _super_sub_fixture(sub_minutes=13)  # Bench = Carreras
    extra = []
    for name, mins, started, y in (("Other Starter", 78, 1, 0), ("Espi", 12, 0, 0)):
        r = _app(name, "TeamA", "ENG-Premier League", "2026-09-20", mins, y, game_id="d1")
        r["started"] = started
        extra.append(r)
    appearances = pd.concat([appearances, pd.DataFrame(extra)], ignore_index=True)
    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, events=None)
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert streaky.replaced_by == "Bench" and streaky.sub_carded


def test_minutes_pairing_other_rounding_convention():
    # Same match, but the data gave the subs one minute less (Carreras 12, Espí 11).
    appearances, schedule, target = _super_sub_fixture(sub_minutes=12)
    extra = []
    for name, mins, started in (("Other Starter", 78, 1), ("Espi", 11, 0)):
        r = _app(name, "TeamA", "ENG-Premier League", "2026-09-20", mins, 0, game_id="d1")
        r["started"] = started
        extra.append(r)
    appearances = pd.concat([appearances, pd.DataFrame(extra)], ignore_index=True)
    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, events=None)
    streaky = next(o for o in result.flagged if o.player == "Streaky")
    assert streaky.replaced_by == "Bench" and streaky.sub_carded


def test_summary_tables_sorted_by_overdue():
    def o(player, ratio, carded=False):
        return assessment.FlaggedOutcome(
            player=player, team="T", opponent="O", overdue_ratio=ratio, rate=0.2,
            start_rate=0.2, start_share=1.0, streak=10, played=True, minutes=90,
            role="Start", carded=carded)
    r = assessment.Assessment(date="2026-10-14", n_flagged=3, n_played=3, n_carded=1,
                              hit_rate=0.33, base_rate=0.1, lift=3.3,
                              flagged=[o("Low", 1.45, carded=True), o("High", 2.56), o("Mid", 1.9)])
    text = assessment.summary_text(r)
    assert text.index("High") < text.index("Mid") < text.index("Low")


def test_same_minute_tie_settled_by_position():
    # Real Madrid 14 Oct: Cucurella (DF) and Diomandé (FW) both 77 min; Carreras
    # (DF) and Espí (FW) both 13 min. Minutes can't split them; positions can.
    appearances, schedule, target = _super_sub_fixture(sub_minutes=13)   # Bench carded
    extra = []
    for name, mins, started in (("Winger", 77, 1), ("Striker Sub", 13, 0)):
        r = _app(name, "TeamA", "ENG-Premier League", "2026-09-20", mins, 0, game_id="d1")
        r["started"] = started
        extra.append(r)
    appearances = pd.concat([appearances, pd.DataFrame(extra)], ignore_index=True)
    positions = {"Streaky": "DF", "Bench": "DF", "Winger": "FW,MF", "Striker Sub": "FW"}

    tied = assessment.assess_day(appearances, schedule, target, cfg=CFG)
    s = next(o for o in tied.flagged if o.player == "Streaky")
    assert s.replaced_by is None and s.sub_maybe_carded

    result = assessment.assess_day(appearances, schedule, target, cfg=CFG, positions=positions)
    s = next(o for o in result.flagged if o.player == "Streaky")
    assert s.replaced_by == "Bench" and s.sub_carded and s.sub_by_position
    assert "sub carded (Bench, same position)" in assessment.summary_text(result)
