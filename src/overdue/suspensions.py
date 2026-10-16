"""Yellow-card accumulation suspension rules, per competition.

Bookings accumulate **within a single competition** — a Premier League yellow
never counts toward an FA Cup ban, and vice versa (English domestic cards have
been competition-siloed since 2019/20; La Liga, Ligue 1 and UEFA have always
been). So the warning that matters for a player is the one for the competition
of *today's* fixture, counting only that competition's yellows this season.

Each rule is a list of ``(threshold, deadline_games)`` tiers: a player is banned
on reaching ``threshold`` yellows, and ``deadline_games`` (if set) is the number
of the team's matches in that competition by which the threshold must be reached
for it to apply (the Premier League's matchweek cut-offs). ``None`` = no deadline.

Rules current as of the 2025/26 domestic and 2026/27 UEFA seasons; approximate
(they cycle/reset and cup counters wipe at the quarter-finals — not modelled).
Sources: Premier League, LFP (Ligue 1), UEFA, The FA disciplinary guidance.
"""

from __future__ import annotations

from .config import LEAGUE_BY_ID

# threshold yellows -> ban, with optional "by the team's Nth game" deadline.
SUSPENSION_RULES: dict[str, list[tuple[int, int | None]]] = {
    "ENG-Premier League": [(5, 19), (10, 32), (15, None)],
    "ESP-La Liga": [(5, None), (10, None), (15, None), (20, None)],
    "FRA-Ligue 1": [(5, None), (10, None), (15, None), (20, None)],
    # Serie A: bans at the 5th, 10th, 14th and 17th yellow, then every one after.
    "ITA-Serie A": [(5, None), (10, None), (14, None), (17, None), (18, None), (19, None),
                    (20, None)],
    # UEFA 2026/27: 4 yellows, then every even caution.
    "INT-Champions League": [(4, None), (6, None), (8, None), (10, None)],
    "INT-Europa League": [(4, None), (6, None), (8, None), (10, None)],
    "INT-Conference League": [(4, None), (6, None), (8, None), (10, None)],
    # English cups: 2 yellows -> miss next game in that cup.
    "ENG-FA Cup": [(2, None), (4, None), (6, None)],
    "ENG-EFL Cup": [(2, None), (4, None), (6, None)],
}


def warning_for(
    competition: str, comp_yellows: int, team_comp_games: int
) -> tuple[str, str]:
    """Return (status, note) — status is "warning" when one booking from a ban.

    ``comp_yellows`` = the player's yellows in ``competition`` this season;
    ``team_comp_games`` = the team's matches played in that competition so far
    (used only to check the Premier League matchweek deadlines).
    """
    rules = SUSPENSION_RULES.get(competition)
    if not rules:
        return "", ""
    short = LEAGUE_BY_ID[competition].short if competition in LEAGUE_BY_ID else competition
    for threshold, deadline in rules:
        if comp_yellows == threshold - 1 and (deadline is None or team_comp_games < deadline):
            return "warning", f"{comp_yellows} {short} yellows — one booking from a ban"
    return "", ""
