"""Sub-cameo card-target ranking — synthetic data, no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import date as _date

import pandas as pd

from overdue.config import ModelConfig
from overdue.targets import (
    game_logs_for,
    player_game_log,
    player_season_breakdown,
    sub_card_targets,
    team_next_fixtures,
    team_player_rows,
)

CFG = ModelConfig(start_minutes=30, min_appearances=5, min_cards=2, overdue_threshold=1.5)


def _app(player, team, d, minutes, yellow, red=0, game_id=None,
         league="ENG-Premier League", season="2627"):
    return {
        "league": league,
        "season": season,
        "game_id": game_id or f"{d}-{team}-{player}",
        "game": f"{d} {team}",
        "date": pd.Timestamp(d),
        "team": team,
        "player": player,
        "minutes": minutes,
        "yellow": yellow,
        "red": red,
    }


def test_sub_card_targets_ranks_and_filters():
    rows = []
    # "Cameo Kicker": 6 sub cameos (15'), booked in 3 of them -> sub_rate 0.5.
    for i in range(6):
        rows.append(_app("Cameo Kicker", "TeamA", f"2026-09-{i + 1:02d}", 15,
                         1 if i < 3 else 0, game_id=f"ck{i}"))
    # plus a couple of starts so they clear the overall sample gate.
    for i in range(4):
        rows.append(_app("Cameo Kicker", "TeamA", f"2026-10-{i + 1:02d}", 90,
                         1 if i == 0 else 0, game_id=f"cks{i}"))

    # "Pure Starter": only starts, never a cameo -> excluded (0 sub apps).
    for i in range(8):
        rows.append(_app("Pure Starter", "TeamB", f"2026-09-{i + 1:02d}", 90,
                         1 if i < 3 else 0, game_id=f"ps{i}"))

    # "Rare Sub": 5 cameos but only 1 booking -> below min_sub_cards -> excluded.
    for i in range(5):
        rows.append(_app("Rare Sub", "TeamC", f"2026-09-{i + 1:02d}", 20,
                         1 if i == 0 else 0, game_id=f"rs{i}"))

    targets = sub_card_targets(pd.DataFrame(rows), CFG, min_sub_apps=4, min_sub_cards=2)
    names = [t.player for t in targets]
    assert names == ["Cameo Kicker"]  # only one qualifies
    t = targets[0]
    assert t.sub_apps == 6 and t.sub_cards == 3
    assert t.sub_rate == 0.5


def test_player_game_log_start_sub_dnp_and_cards():
    rows = []
    # Team plays 4 games; a teammate defines every game (so the DNP shows up).
    for i, gid in enumerate(["g1", "g2", "g3", "g4"]):
        rows.append(_app("Teammate", "TeamA", f"2026-09-1{i}", 90, 0, game_id=gid))
    # Target: g1 started + carded, g2 sub cameo, g3 started clean, g4 DNP (no row).
    rows.append(_app("Target", "TeamA", "2026-09-10", 90, 1, game_id="g1"))
    rows.append(_app("Target", "TeamA", "2026-09-11", 20, 0, game_id="g2"))
    rows.append(_app("Target", "TeamA", "2026-09-12", 90, 0, game_id="g3"))
    # Opponents so opponent lookup works on g1.
    rows.append(_app("Rival", "TeamB", "2026-09-10", 90, 0, game_id="g1"))

    log = player_game_log(pd.DataFrame(rows), "Target", "TeamA", CFG, window=20)
    # Newest first: g4, g3, g2, g1.
    assert [e.role for e in log] == ["DNP", "Start", "Sub", "Start"]
    assert [e.minutes for e in log] == [0, 90, 20, 90]
    assert log[-1].card == "Y" and log[-1].opponent == "TeamB"
    assert log[0].card == ""  # DNP, no card


def _event(player, gid, minute, minute_plus, kind, team="TeamA",
           league="ENG-Premier League", season="2627"):
    return {"league": league, "season": season, "game": gid, "game_id": gid,
            "date": pd.Timestamp("2026-09-10"), "team": team, "player": player,
            "player_off": "", "minute": minute, "minute_plus": minute_plus, "kind": kind}


def test_sub_entry_minute_estimated_from_minutes():
    # No events: a 34-minute sub is shown as having entered in the 57th minute
    # (91 - 34), while a starter keeps their plain minutes.
    rows = [
        _app("Teammate", "TeamA", "2026-09-10", 90, 0, game_id="g1"),
        _app("Teammate", "TeamA", "2026-09-11", 90, 0, game_id="g2"),
        _app("Target", "TeamA", "2026-09-10", 90, 0, game_id="g1"),   # starter
        _app("Target", "TeamA", "2026-09-11", 34, 0, game_id="g2"),   # sub
    ]
    log = player_game_log(pd.DataFrame(rows), "Target", "TeamA", CFG, window=20)
    by_gid = {e.date: e for e in log}
    sub = by_gid["2026-09-11"]
    start = by_gid["2026-09-10"]
    assert sub.role == "Sub" and sub.entry_minute == 57
    assert sub.min_display == "57th min"
    assert start.role == "Start" and start.min_display == "90"


def test_card_minute_from_events():
    rows = [
        _app("Teammate", "TeamA", "2026-09-10", 90, 0, game_id="g1"),
        _app("Teammate", "TeamA", "2026-09-11", 90, 0, game_id="g2"),
        _app("Booked", "TeamA", "2026-09-10", 90, 1, game_id="g1"),        # YC in 87'
        _app("Booked", "TeamA", "2026-09-11", 90, 1, game_id="g2"),        # YC in 90+3'
    ]
    events = pd.DataFrame([
        _event("Booked", "g1", 87, 0, "yellow"),
        _event("Booked", "g2", 93, 3, "yellow"),
    ])
    log = player_game_log(pd.DataFrame(rows), "Booked", "TeamA", CFG,
                          window=20, events=events)
    by_gid = {e.date: e for e in log}
    assert by_gid["2026-09-10"].card_display == "87'"
    assert by_gid["2026-09-11"].card_display == "90+3'"


def test_card_display_falls_back_to_letter_without_events():
    rows = [
        _app("Teammate", "TeamA", "2026-09-10", 90, 0, game_id="g1"),
        _app("Booked", "TeamA", "2026-09-10", 90, 1, red=1, game_id="g1"),
    ]
    log = player_game_log(pd.DataFrame(rows), "Booked", "TeamA", CFG, window=20)
    assert log[0].card == "Y+R" and log[0].card_display == "Y+R"


def test_real_sub_event_overrides_estimate():
    rows = [
        _app("Teammate", "TeamA", "2026-09-10", 90, 0, game_id="g1"),
        _app("Target", "TeamA", "2026-09-10", 34, 0, game_id="g1"),   # 91-34 -> 57
    ]
    events = pd.DataFrame([_event("Target", "g1", 62, 0, "sub")])
    log = player_game_log(pd.DataFrame(rows), "Target", "TeamA", CFG,
                          window=20, events=events)
    assert log[0].entry_minute == 62 and log[0].min_display == "62nd min"


def test_player_role_log_spans_beyond_team_window():
    from overdue.targets import player_role_log
    rows = []
    # 10 team games; the player subs in the even ones, starts the odd ones.
    for i in range(10):
        gid = f"g{i}"
        rows.append(_app("Teammate", "TeamA", f"2026-09-{i + 1:02d}", 90, 0, game_id=gid))
        mins = 20 if i % 2 == 0 else 90            # even = sub cameo, odd = start
        rows.append(_app("Rotato", "TeamA", f"2026-09-{i + 1:02d}", mins, 0, game_id=gid))
    df = pd.DataFrame(rows)
    # The team-window view (last 3 games: g9,g8,g7) has only ONE sub (g8).
    log = player_game_log(df, "Rotato", "TeamA", CFG, window=3)
    assert sum(1 for e in log if e.role == "Sub") == 1
    # The role view returns the last 3 SUB cameos (g8,g6,g4), spanning further back.
    subs = player_role_log(df, "Rotato", "TeamA", "Sub", CFG, window=3)
    assert [e.role for e in subs] == ["Sub", "Sub", "Sub"]
    assert [e.date for e in subs] == ["2026-09-09", "2026-09-07", "2026-09-05"]
    starts = player_role_log(df, "Rotato", "TeamA", "Start", CFG, window=3)
    assert all(e.role == "Start" for e in starts) and len(starts) == 3


def test_player_game_log_computes_score():
    rows = [
        # g1: Target's team scores 2 (one an opponent own goal), opponent scores 1.
        _app("Target", "TeamA", "2026-09-10", 90, 0, game_id="g1"),
        _app("Rival", "TeamB", "2026-09-10", 90, 0, game_id="g1"),
    ]
    df = pd.DataFrame(rows)
    df["goals"] = [1, 1]        # a Target-team goal and a TeamB goal
    df["own_goals"] = [0, 1]    # TeamB player OG counts for TeamA
    log = player_game_log(df, "Target", "TeamA", CFG, window=20)
    e = log[0]
    assert e.score_known
    assert e.team_score == 2 and e.opp_score == 1


def test_game_logs_for_matches_standalone_builders():
    # The shared-index build (game_logs_for) must produce the same game logs and
    # season breakdown as the standalone per-player helpers.
    rows = []
    for i, gid in enumerate(["g1", "g2", "g3", "g4"]):
        rows.append(_app("Mate", "TeamA", f"2026-09-1{i}", 90, 0, game_id=gid))
    rows.append(_app("P", "TeamA", "2026-09-10", 90, 1, game_id="g1"))          # start + YC
    rows.append(_app("P", "TeamA", "2026-09-11", 20, 0, game_id="g2"))          # sub
    rows.append(_app("P", "TeamA", "2026-09-12", 90, 0, game_id="g3"))          # start
    rows.append(_app("Rival", "TeamB", "2026-09-10", 90, 0, game_id="g1"))
    df = pd.DataFrame(rows)

    class R:
        def __init__(s, p, t):
            s.player, s.team = p, t

    logs = game_logs_for(df, [R("P", "TeamA")])
    d = logs["P"]
    assert [(e.date, e.role, e.card) for e in d.games] == \
        [(e.date, e.role, e.card) for e in player_game_log(df, "P", "TeamA", CFG, 20)]
    assert [(b.season, b.competition, b.apps, b.starts, b.subs, b.yellows) for b in d.breakdown] == \
        [(b.season, b.competition, b.apps, b.starts, b.subs, b.yellows)
         for b in player_season_breakdown(df, "P", CFG)]
    assert [e.date for e in d.sub_games] == ["2026-09-11"]


def test_game_logs_for_dedupes_players():
    rows = [_app("P", "TeamA", f"2026-09-{i + 1:02d}", 90, 1 if i < 2 else 0, game_id=f"g{i}")
            for i in range(6)]

    class R:  # minimal row object with player/team
        def __init__(self, p, t):
            self.player, self.team = p, t

    logs = game_logs_for(pd.DataFrame(rows), [R("P", "TeamA"), R("P", "TeamA")])
    assert set(logs) == {"P"} and len(logs["P"].games) == 6


def test_adjusted_rate_lets_bigger_sample_win():
    # Low-rate filler players pull the league cameo baseline down, so a tiny high
    # raw rate (2/4) gets shrunk below a bigger, slightly lower one (9/20).
    rows = []
    for j in range(5):
        for i in range(8):
            rows.append(_app(f"Filler{j}", "TF", f"2026-09-{i + 1:02d}", 15, 0,
                             game_id=f"f{j}-{i}"))
    for i in range(4):
        rows.append(_app("Small", "TA", f"2026-09-{i + 1:02d}", 15, 1 if i < 2 else 0,
                         game_id=f"s{i}"))
    for i in range(20):
        rows.append(_app("Big", "TB", f"2026-08-{i + 1:02d}", 15, 1 if i < 9 else 0,
                         game_id=f"b{i}"))

    # These synthetic players are pure subs (no starts), so disable the recency
    # gate here — this test is about the adjusted-rate ranking, not that filter.
    cfg = replace(CFG, min_recent_starts=0)
    tg = sub_card_targets(pd.DataFrame(rows), cfg, min_sub_apps=4, min_sub_cards=2)
    names = [t.player for t in tg]
    assert "Big" in names and "Small" in names
    assert names.index("Big") < names.index("Small")  # bigger sample ranks higher
    small = next(t for t in tg if t.player == "Small")
    assert small.sub_rate == 0.5                        # raw rate still shown
    assert small.adj_sub_rate < small.sub_rate          # but shrunk toward baseline


def test_recency_gate_hides_players_out_of_the_xi():
    rows = []
    # Faded: a run of cameo bookings, but ONLY early — none in the team's last 5.
    for i in range(6):
        rows.append(_app("Faded", "TeamZ", f"2026-09-{i + 1:02d}", 18,
                         1 if i < 3 else 0, game_id=f"f{i}"))
    # A regular defines 5 newer team games (so those are the last 5), Faded absent.
    for i in range(5):
        rows.append(_app("Regular", "TeamZ", f"2026-10-{i + 1:02d}", 90, 0, game_id=f"r{i}"))
    tg = sub_card_targets(pd.DataFrame(rows), CFG, min_sub_apps=4, min_sub_cards=2)
    assert "Faded" not in [t.player for t in tg]   # 0 starts in the last 5 -> hidden


def test_injured_player_hidden_from_targets_and_team_view():
    rows = []
    # A sub-cameo booker who then goes absent for the team's last 3 games (injury).
    for i in range(6):
        rows.append(_app("Crocked", "TeamZ", f"2026-09-{i + 1:02d}", 18,
                         1 if i < 3 else 0, game_id=f"c{i}"))
    for i in range(3):
        rows.append(_app("Fit", "TeamZ", f"2026-10-{i + 1:02d}", 90, 0, game_id=f"g{i}"))
    df = pd.DataFrame(rows)
    tg = sub_card_targets(df, CFG, min_sub_apps=4, min_sub_cards=2)
    assert "Crocked" not in [t.player for t in tg]        # excluded from the shortlist
    tv = team_player_rows(df, CFG)
    # The team explorer also drops players absent from the team's recent games
    # (injury/suspension proxy) so a squad view lists only pickable players.
    assert "Crocked" not in [r.player for r in tv["TeamZ"]]
    assert "Fit" in [r.player for r in tv["TeamZ"]]


def test_player_season_breakdown_totals():
    rows = [
        _app("P", "TA", "2026-09-01", 90, 1, game_id="a"),          # 26/27 EPL start+YC
        _app("P", "TA", "2026-09-08", 15, 0, game_id="b"),          # 26/27 EPL sub
        _app("P", "TA", "2026-03-01", 90, 1, game_id="c",
             league="INT-Champions League"),                        # 26/27 UCL start+YC
    ]
    bd = player_season_breakdown(pd.DataFrame(rows), "P", CFG)
    assert len(bd) == 2                     # EPL and UCL split out
    assert sum(x.apps for x in bd) == 3
    assert sum(x.starts for x in bd) == 2
    assert sum(x.subs for x in bd) == 1
    assert sum(x.yellows for x in bd) == 2


def test_breakdown_sums_card_counts_not_matches():
    # A single game with two yellows counts as 2 total yellows (matches FotMob),
    # not 1 "match booked".
    rows = [
        _app("P", "TA", "2026-09-01", 90, 2, red=1, game_id="a"),  # second-yellow game
        _app("P", "TA", "2026-09-08", 90, 1, game_id="b"),
    ]
    bd = player_season_breakdown(pd.DataFrame(rows), "P", CFG)
    assert sum(x.yellows for x in bd) == 3   # 2 + 1
    assert sum(x.reds for x in bd) == 1


def test_team_next_fixtures_picks_earliest_upcoming():
    sched = pd.DataFrame([
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Leeds",
         "date": pd.Timestamp("2026-09-10"), "played": True, "notes": ""},   # past
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Spurs",
         "date": pd.Timestamp("2026-09-27"), "played": False, "notes": ""},
        {"league": "ENG-Premier League", "home_team": "Arsenal", "away_team": "Chelsea",
         "date": pd.Timestamp("2026-09-20"), "played": False, "notes": ""},  # next
    ])
    nf = team_next_fixtures(sched, _date(2026, 9, 18))
    assert nf["Arsenal"].opponent == "Chelsea"
    assert nf["Arsenal"].is_today is False
    assert nf["Chelsea"].opponent == "Arsenal"


def test_precomputed_metrics_match_internal_computation():
    from overdue.model import compute_player_metrics
    rows = []
    for i in range(6):
        rows.append(_app("Cameo", "TeamA", f"2026-09-{i + 1:02d}", 15,
                         1 if i < 3 else 0, game_id=f"c{i}"))
    for i in range(4):
        rows.append(_app("Cameo", "TeamA", f"2026-10-{i + 1:02d}", 90,
                         1 if i == 0 else 0, game_id=f"cs{i}"))
    df = pd.DataFrame(rows)
    metrics = compute_player_metrics(df, CFG)
    # Passing precomputed metrics yields identical output to computing internally.
    a = team_player_rows(df, CFG)
    b = team_player_rows(df, CFG, metrics=metrics)
    assert [(r.player, r.cards, r.sub_cards) for r in a["TeamA"]] == \
           [(r.player, r.cards, r.sub_cards) for r in b["TeamA"]]
    ta = sub_card_targets(df, CFG, min_sub_apps=4, min_sub_cards=2)
    tb = sub_card_targets(df, CFG, min_sub_apps=4, min_sub_cards=2, metrics=metrics)
    assert [r.player for r in ta] == [r.player for r in tb]


def test_sub_card_targets_excludes_goalkeepers():
    rows = []
    for i in range(6):
        rows.append(_app("Keeper Sub", "TeamA", f"2026-09-{i + 1:02d}", 15,
                         1 if i < 3 else 0, game_id=f"ks{i}"))
    for i in range(4):
        rows.append(_app("Keeper Sub", "TeamA", f"2026-10-{i + 1:02d}", 90,
                         1 if i == 0 else 0, game_id=f"kss{i}"))
    targets = sub_card_targets(pd.DataFrame(rows), CFG,
                               positions={"Keeper Sub": "GK"},
                               min_sub_apps=4, min_sub_cards=2)
    assert targets == []
