"""Robust club-name matching across schedule / store / slug spellings."""

from __future__ import annotations

from overdue.teamnames import canonical, resolve, same_team


def test_schedule_short_forms_match_store_long_forms():
    # Schedule (left) vs appearance store (right) — the pairs that broke.
    pairs = [
        ("Tottenham", "Tottenham Hotspur"),
        ("Manchester Utd", "Manchester United"),
        ("Newcastle Utd", "Newcastle United"),
        ("Nott'ham Forest", "Nottingham Forest"),
        ("Wolves", "Wolverhampton Wanderers"),
        ("Paris S-G", "Paris Saint-Germain"),
        ("Brighton", "Brighton & Hove Albion"),
        ("West Ham", "West Ham United"),
        ("Betis", "Real Betis"),
    ]
    for short, long in pairs:
        assert same_team(short, long), f"{short!r} should match {long!r}"
        assert resolve(short, [long, "Chelsea", "Arsenal"]) == long


def test_derbies_stay_distinct():
    # Expanding abbreviations must NOT merge two different clubs.
    assert not same_team("Manchester Utd", "Manchester City")
    assert not same_team("Manchester United", "Manchester City")
    assert not same_team("Sheffield United", "Sheffield Wednesday")
    assert not same_team("Real Madrid", "Real Sociedad")
    assert not same_team("Athletic Club", "Atlético Madrid")


def test_accents_and_punctuation_insensitive():
    assert same_team("Alavés", "Deportivo Alavés")
    assert same_team("Atlético Madrid", "Atletico Madrid")
    assert canonical("Paris S-G") == canonical("Paris Saint-Germain")


def test_french_gentile_and_short_forms():
    # City name vs adjective / full club name.
    assert same_team("Stade Rennais", "Rennes")
    assert same_team("Stade Brestois", "Brest")
    assert same_team("Olympique Lyonnais", "Lyon")
    assert same_team("Dep. A Coruña", "Deportivo La Coruña")
    # Different clubs must stay distinct.
    assert not same_team("Rennes", "Reims")
    assert not same_team("Lyon", "Lorient")


def test_same_club_is_whole_word_and_affix_aware():
    from overdue.teamnames import same_club
    # Legit short/full pairs (schedule vs store) match.
    assert same_club("Tottenham", "Tottenham Hotspur")
    assert same_club("Brighton", "Brighton & Hove Albion")
    assert same_club("West Ham", "West Ham United")
    assert same_club("Betis", "Real Betis")
    assert same_club("Newcastle Utd", "Newcastle United")
    # Non-league clubs that merely contain a tracked club's name must NOT match.
    assert not same_club("Chester", "Manchester City")       # mid-word substring
    assert not same_club("South Liverpool", "Liverpool")     # extra distinctive word
    assert not same_club("Dover Athletic", "Athletic Club")
    assert not same_club("FC United", "Manchester United")
    assert not same_club("Ossett United", "Newcastle United")
    # Derbies still distinct.
    assert not same_club("Manchester City", "Manchester United")


def test_resolve_prefers_tightest_and_returns_empty_on_miss():
    teams = ["Tottenham Hotspur", "Tottenham Hotspur U21"]
    # Shortest substring match wins (the senior side, not the U21 shadow).
    assert resolve("Tottenham", teams) == "Tottenham Hotspur"
    assert resolve("Barcelona", ["Chelsea", "Arsenal"]) == ""
    assert resolve("", ["Chelsea"]) == ""


def test_native_city_spellings_match_english_ones():
    from overdue.teamnames import same_team

    assert same_team("Slavia Prague", "SK Slavia Praha")
    assert same_team("Bayern Munich", "FC Bayern München")
    assert same_team("Viking", "Viking FK")


def test_milan_derby_is_not_merged():
    from overdue.teamnames import same_team
    assert not same_team("Milan", "Inter Milan")
    assert not same_team("AC Milan", "Internazionale")
    assert same_team("Inter", "FC Internazionale Milano")
    assert same_team("Milan", "AC Milan")
    assert same_team("Roma", "AS Roma")


def test_paris_fc_is_not_psg():
    from overdue.teamnames import same_team
    assert not same_team("Paris FC", "Paris SG")
    assert not same_team("Paris FC", "Paris Saint-Germain")
    assert same_team("Paris FC", "Paris")
    assert same_team("Paris SG", "PSG")
