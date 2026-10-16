"""Card-odds cache, pasted-price parsing and name matching — no network."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from overdue import odds_book as od


def test_to_decimal_handles_fractional_and_decimal():
    assert od._to_decimal("3.5") == 3.5
    assert od._to_decimal("5/2") == 3.5          # 1 + 5/2
    assert od._to_decimal("evs") is None
    assert od._to_decimal("0.9") is None         # <= 1 is not a real price
    assert od._to_decimal("") is None


def _market_def(mid, type_name):
    return (f'"ocMarketId":{mid},"ocMarketSiblingId":1,'
            f'"marketName":"A v B#{type_name}","marketTypeName":"{type_name}"')


@dataclass
class _Row:
    player: str
    bookie_odds: float | None = None
    bookie_source: str = ""


def test_attach_odds_matches_by_player_key():
    idx = {od._player_key("Jean-Philippe Mateta"): {"price": 3.0, "book": "Bet365"}}
    rows = [_Row("J.P. Mateta"), _Row("Someone Else")]
    n = od.attach_odds(rows, idx)
    assert n == 1
    assert rows[0].bookie_odds == 3.0 and rows[0].bookie_source == "Bet365"
    assert rows[1].bookie_odds is None


def test_parse_pasted_odds_fraction_and_decimal():
    text = """
    Wesley Fofana 12/5
    Pep Chavarria (11/5)
    Cole Palmer 2.75
    -- Reece James — 4/1
    garbage line with no price
    """
    parsed = od.parse_pasted_odds(text, book="Bet365")
    assert parsed[od._player_key("Cole Palmer")]["price"] == 2.75
    assert parsed[od._player_key("Wesley Fofana")]["price"] == 3.4   # 12/5
    assert parsed[od._player_key("Reece James")]["price"] == 5.0     # 4/1, dashes stripped
    assert all(r["book"] == "Bet365" for r in parsed.values())
    assert len(parsed) == 4


def test_attach_odds_matches_mononyms_and_apostrophes():
    from types import SimpleNamespace
    # Bookmaker uses fuller / differently-punctuated names than the store.
    index = {od._player_key(n): {"name": n, "price": 5.75, "book": "Sky"}
             for n in ("Abner Vinicius", "Alejandro Catena", "Dara O'Shea",
                       "Bruno Fernandes", "Bruno Guimaraes")}
    rows = [SimpleNamespace(player=p, bookie_odds=None, bookie_source="")
            for p in ("Abner", "Catena", "Dara O'Shea")]
    assert od.attach_odds(rows, index) == 3
    assert all(r.bookie_odds == 5.75 for r in rows)
    # A shared first name must NOT resolve a mononym (ambiguous → no price).
    ambiguous = [SimpleNamespace(player="Bruno", bookie_odds=None, bookie_source="")]
    assert od.attach_odds(ambiguous, index) == 0


def test_merge_pasted_odds_keeps_higher_price(tmp_path):
    from datetime import date
    path = tmp_path / "odds.json"
    b365 = od.parse_pasted_odds("Cole Palmer 2.5\nReece James 4/1", book="Bet365")
    od.merge_pasted_odds(b365, date(2026, 9, 18), path=path)
    sky = od.parse_pasted_odds("Cole Palmer 2.75\nReece James 3/1", book="Sky Bet")
    merged = od.merge_pasted_odds(sky, date(2026, 9, 18), path=path)["players"]
    # Palmer: Sky 2.75 > B365 2.5 → Sky wins; James: B365 5.0 > Sky 4.0 → B365 stays.
    assert merged[od._player_key("Cole Palmer")] == {"name": "Cole Palmer", "price": 2.75,
                                                    "book": "Sky Bet", "source": "pasted"}
    assert merged[od._player_key("Reece James")]["price"] == 5.0
    assert merged[od._player_key("Reece James")]["book"] == "Bet365"


def test_odds_index_is_date_scoped(tmp_path):
    path = tmp_path / "odds.json"
    od.save_odds({"date": "2026-09-18", "players": {"x|y": {"price": 2.5, "book": "Sky Bet"}}},
                 path=path)
    assert od.odds_index_for(date(2026, 9, 18), path=path) == {"x|y": {"price": 2.5, "book": "Sky Bet"}}
    assert od.odds_index_for(date(2026, 9, 19), path=path) == {}   # stale → ignored


def _idx(names, teams=("Arsenal", "Lille"), price=4.0):
    return {od._player_key(n): {"name": n, "price": price, "book": "Bet365",
                                "teams": list(teams)} for n in names}


def _rows(*pairs):
    from types import SimpleNamespace
    return [SimpleNamespace(player=p, team=t, bookie_odds=None, bookie_source="")
            for p, t in pairs]


def test_attach_odds_uses_builtin_alias_for_unrelated_names():
    # The bookmaker lists Alexsandro Ribeiro (Lille) as "Victor" — nothing in common.
    index = _idx(["Victor", "Declan Rice", "Benjamin André"])
    rows = _rows(("Alexsandro Ribeiro", "Lille"), ("Declan Rice", "Arsenal"))
    assert od.attach_odds(rows, index, aliases=od.load_aliases(Path("/nonexistent"))) == 2
    assert rows[0].bookie_odds == 4.0


def test_load_aliases_merges_local_file(tmp_path):
    f = tmp_path / "player_aliases.json"
    f.write_text('{"_comment": "x", "Some Player": "Nick", "Alexsandro Ribeiro": ["Alex"]}',
                 encoding="utf-8")
    al = od.load_aliases(f)
    assert al["Some Player"] == ["Nick"]
    assert al["Alexsandro Ribeiro"] == ["Victor", "Alex"]
    assert "_comment" not in al
    index = _idx(["Nick"])
    rows = _rows(("Some Player", "Arsenal"))
    assert od.attach_odds(rows, index, aliases=al) == 1


def test_loose_match_bookie_mononym_and_subset_names():
    index = _idx(["Gabriel", "Gabriel Martinelli Silva", "Heung-min Son"],
                 teams=("Arsenal", "Tottenham"))
    rows = _rows(("Gabriel Magalhães", "Arsenal"), ("Gabriel Martinelli", "Arsenal"),
                 ("Son Heung-min", "Tottenham"))
    assert od.attach_odds(rows, index, aliases={}) == 2
    # "Gabriel" appears in two bookie names → too ambiguous for Magalhães.
    assert rows[0].bookie_odds is None
    assert rows[1].bookie_odds == 4.0 and rows[2].bookie_odds == 4.0


def test_loose_match_spelling_variant_and_rejects_shared_first_name():
    index = _idx(["Yehor Yarmoliuk", "Joao Palhinha"], teams=("Brentford", "Spurs"))
    rows = _rows(("Yegor Yarmolyuk", "Brentford"), ("Joao Pedro", "Brentford"))
    assert od.attach_odds(rows, index, aliases={}) == 1
    assert rows[0].bookie_odds == 4.0 and rows[1].bookie_odds is None


def test_loose_match_stays_within_the_players_fixture():
    index = _idx(["Forbs"], teams=("Club Brugge", "Atletico"))
    index.update(_idx(["Declan Rice"], teams=("Arsenal", "Lille")))
    rows = _rows(("Carlos Forbs", "Arsenal"))  # wrong game: must not take Brugge's Forbs
    assert od.attach_odds(rows, index, aliases={}) == 0


def test_unpriced_hints_lists_spare_names_in_the_game():
    index = _idx(["Victor", "Declan Rice"])
    rows = _rows(("Alexsandro Ribeiro", "Lille"), ("Declan Rice", "Arsenal"),
                 ("Someone", "Chelsea"))
    hints = od.unpriced_hints(rows, index, aliases={})
    assert hints == ["Alexsandro Ribeiro (Lille): no matching name; "
                     "unmatched in that game: Victor"]


def test_alias_matches_when_bookie_name_contains_it():
    # The bookmaker sometimes lists him as "Alexsandro Victor", not just "Victor".
    index = _idx(["Alexsandro Victor", "Declan Rice"])
    rows = _rows(("Alexsandro Ribeiro", "Lille"))
    assert od.attach_odds(rows, index, aliases=od.load_aliases(Path("/nonexistent"))) == 1


def test_save_odds_archives_by_day_for_later_assess(tmp_path, monkeypatch):
    p = tmp_path / "odds.json"
    monkeypatch.setattr(od, "ODDS_PATH", p)
    od.save_odds({"date": "2026-09-19", "players": {"a|b": {"price": 2.0, "book": "Sky"}}})
    od.save_odds({"date": "2026-09-20", "players": {"c|d": {"price": 3.0, "book": "Sky"}}})
    # The live cache moved on, but the archive still answers for the 19th.
    assert od.odds_index_for(date(2026, 9, 19)) == {}
    assert od.odds_index_for(date(2026, 9, 19), history=True)["a|b"]["price"] == 2.0


class _FakeDriver:
    def __init__(self, **_):
        self.refreshes = 0

    def set_window_size(self, *_):
        pass

    def uc_open_with_reconnect(self, *_, **__):
        pass

    def refresh(self):
        self.refreshes += 1

    def get_page_source(self):
        return "<html></html>"

    def quit(self):
        pass


@dataclass
class _TeamRow:
    player: str
    team: str
    bookie_odds: float | None = None
    bookie_source: str = ""


def test_same_initial_surname_in_another_game_lends_no_price():
    # Jacob Bruun Larsen (Burnley v Wolves) must not price Jørgen Strand Larsen.
    idx = {od._player_key("Jacob Bruun Larsen"): {
        "name": "Jacob Bruun Larsen", "price": 9.0, "book": "Sky Bet",
        "all_books": {}, "teams": ["Burnley", "Wolves"]}}
    rows = [_TeamRow("Jørgen Strand Larsen", "Crystal Palace"),
            _TeamRow("Jacob Bruun Larsen", "Burnley")]
    assert od.attach_odds(rows, idx) == 1
    assert rows[0].bookie_odds is None and rows[1].bookie_odds == 9.0


def test_both_same_key_players_priced_from_their_own_games():
    idx = {
        "j|larsen": {"name": "Jacob Bruun Larsen", "price": 9.0, "book": "Sky Bet",
                     "all_books": {}, "teams": ["Burnley", "Wolves"]},
        "j|larsen#jorgen strand larsen": {
            "name": "Jorgen Strand Larsen", "price": 3.75, "book": "Sky Bet",
            "all_books": {}, "teams": ["Brighton", "Crystal Palace"]},
    }
    rows = [_TeamRow("Jørgen Strand Larsen", "Crystal Palace"),
            _TeamRow("Jacob Bruun Larsen", "Burnley")]
    assert od.attach_odds(rows, idx) == 2
    assert rows[0].bookie_odds == 3.75 and rows[1].bookie_odds == 9.0
