"""Card prices: the local odds cache and matching prices to report rows.

Prices live in ``data/odds.json`` keyed to a date and by player, as
``{"date": ..., "players": {player_key: {name, price, book, all_books?}}}``.
They get there by pasting bookmaker lines (``overdue refresh-odds --paste``) or
from any feed that writes the same shape. Report rows are then paired with a
price by name (accent-folded, with mononym and alias handling), which fills the
Best Price and Edge columns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .lineups import _player_key, _unaccent
from .teamnames import same_team


ODDS_PATH = config.DATA_DIR / "odds.json"


TARGET_BOOKS = ("Bet365", "Sky Bet")


@dataclass
class PlayerOdds:
    price: float          # decimal odds for the chosen bookmaker
    book: str             # which bookmaker the price is from
    all_books: dict       # {book_name: price} across all books seen


def _to_decimal(raw: str) -> float | None:
    """Parse an odds string — decimal ('3.5') or fractional ('5/2') — to decimal."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    if "/" in raw:
        try:
            n, d = raw.split("/", 1)
            return round(1 + float(n) / float(d), 3)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 1 else None


# A pasted line: "Wesley Fofana 12/5", "Pep Chavarria (11/5)", "Cole Palmer 2.75".
_PASTE_RE = re.compile(r'^(.*?)[\s(]+((?:\d{1,3}/\d{1,3})|(?:\d+(?:\.\d+)?))\)?\s*$')


def parse_pasted_odds(text: str, book: str = "Bet365") -> dict[str, dict]:
    """Parse hand-pasted 'Player price' lines into ``player_key -> record``.

    Accepts fractional ("11/5") or decimal ("3.2") prices, with or without
    parentheses — i.e. what you get copying a bookmaker's card market list.
    ``book`` labels which bookmaker the pasted prices are from.
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _PASTE_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip().strip("-–—").strip()
        price = _to_decimal(m.group(2))
        if name and price and len(name) >= 3:
            out[_player_key(name)] = {"name": name, "price": price, "book": book,
                                      "source": "pasted"}
    return out


def merge_pasted_odds(parsed: dict[str, dict], today, path: Path | None = None) -> dict:
    """Merge pasted prices into today's cache, keeping the higher price per player.

    Lets you paste Bet365's list then Sky Bet's list and end with the better of
    the two per player, alongside anything already cached for today.
    """
    want = today.isoformat() if hasattr(today, "isoformat") else str(today)
    stored = load_odds(path)
    players = dict(stored.get("players", {})) if stored.get("date") == want else {}
    for key, rec in parsed.items():
        cur = players.get(key)
        if not cur or rec["price"] > cur.get("price", 0):
            players[key] = rec
    out = {"date": want, "players": players, "source_urls": stored.get("source_urls", [])}
    save_odds(out, path)
    return out


def _is_pasted(rec: dict) -> bool:
    # Older pasted entries carry no marker, but only feed entries have all_books.
    return rec.get("source") == "pasted" or "all_books" not in rec


def history_path(day) -> Path:
    """Per-day archive copy of the odds cache, so ``assess`` can show the price a
    player was at after ``odds.json`` has moved on to a later day."""
    want = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return ODDS_PATH.parent / "odds_history" / f"odds-{want}.json"


def save_odds(stored: dict, path: Path | None = None) -> None:
    archive = path is None and bool(stored.get("date"))
    path = path or ODDS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(stored, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    if archive and stored.get("players"):
        try:
            hp = history_path(stored["date"])
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(text, encoding="utf-8")
        except OSError:
            pass  # the archive is a convenience; the live cache is what matters


def load_odds(path: Path | None = None) -> dict:
    path = path or ODDS_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def odds_index_for(day, path: Path | None = None, history: bool = False) -> dict[str, dict]:
    """Return ``player_key -> {price, book}`` for ``day`` (empty if stale/none).

    Prices are date-specific, so a cache dated other than ``day`` is ignored —
    unless ``history`` is set and a per-day archive copy exists for ``day``.
    """
    stored = load_odds(path)
    want = day.isoformat() if hasattr(day, "isoformat") else str(day)
    if stored.get("date") != want:
        if history and path is None:
            archived = load_odds(history_path(want))
            if archived.get("date") == want:
                return archived.get("players", {})
        return {}
    return stored.get("players", {})


_REPORT_ROW = re.compile(r'<tr class="row[^"]*"([^>]*)>(.*?)</tr>', re.S)


def odds_from_report_html(day, reports_dir: Path | None = None) -> dict[str, dict]:
    """``player -> {price, book}`` read back from that day's saved HTML report.

    The report page stores each priced player's Best Price (``data-bookie``), so
    a past day's odds can be recovered even when ``odds.json`` has since been
    replaced by a later day's prices (and predates the per-day archive).
    """
    want = day.isoformat() if hasattr(day, "isoformat") else str(day)
    path = (reports_dir or config.REPORTS_DIR) / f"{want}.html"
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    import html as _html
    out: dict[str, dict] = {}
    for attrs, body in _REPORT_ROW.findall(text):
        player = re.search(r'data-player="([^"]*)"', attrs)
        price = re.search(r'data-bookie="([0-9.]+)"', body)
        if not (player and price):
            continue
        book = re.search(r'title="([^"]*?) — best of', body)
        out.setdefault(_html.unescape(player.group(1)), {
            "price": float(price.group(1)),
            "book": _html.unescape(book.group(1)) if book else ""})
    return out


# Players whose bookmaker name shares nothing usable with their store name, so no
# automatic rule can pair them. Store name -> the name(s) the bookmaker lists them
# under. Extend locally (without touching code) via data/player_aliases.json, e.g.
#   {"Alexsandro Ribeiro": "Victor", "Some Player": ["Nickname", "Other Form"]}
PLAYER_ALIASES: dict[str, tuple[str, ...]] = {
    # Lille CB; full name Alexsandro Victor de Souza Ribeiro.
    "Alexsandro Ribeiro": ("Victor",),
}


ALIASES_PATH = config.DATA_DIR / "player_aliases.json"


def load_aliases(path: Path | None = None) -> dict[str, list[str]]:
    """Built-in :data:`PLAYER_ALIASES` merged with the optional local alias file."""
    merged = {k: list(v) for k, v in PLAYER_ALIASES.items()}
    path = path or ALIASES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        return merged
    for store_name, bookie in data.items():
        if str(store_name).startswith("_"):  # allow "_comment" keys
            continue
        names = [bookie] if isinstance(bookie, str) else (
            [b for b in bookie if isinstance(b, str)] if isinstance(bookie, list) else [])
        merged[store_name] = list(dict.fromkeys(merged.get(store_name, []) + names))
    return merged


def _name_tokens(name: str) -> list[str]:
    """Accent-folded, punctuation-free word tokens of a player name."""
    n = _unaccent(str(name)).lower().replace("'", "").replace("’", "")
    return [t for t in re.sub(r"[^a-z0-9]+", " ", n).split() if t]


def _full_key(name: str) -> str:
    return " ".join(_name_tokens(name))


def _mononym_match(name: str, tok_index: dict[str, list]) -> dict | None:
    """Fuzzy match for single-name (mononym) players across name forms.

    The match data lists some players by one name — "Abner", "Catena", "Raphinha" — while
    the bookmaker shows the full name ("Abner Vinicius", "Alejandro Catena"), so
    the initial+surname key misses. When one side is a single distinctive token,
    match the unique bookmaker entry that contains that token (in any position).
    """
    toks = _name_tokens(name)
    if len(toks) == 1 and len(toks[0]) >= 4:
        cands = {id(r): r for r in tok_index.get(toks[0], [])}
        if len(cands) == 1:
            return next(iter(cands.values()))
    return None


def _in_fixture(rec: dict, team: str) -> bool:
    teams = rec.get("teams") or []
    return bool(team) and any(same_team(team, t) for t in teams)


def _fixture_ok(rec: dict, team: str) -> bool:
    """Whether a price can belong to a player of ``team``: its fixture includes
    the team, or we don't know one side (old/pasted records, no team)."""
    return not rec.get("teams") or not team or _in_fixture(rec, team)


def _unique(cands: list[dict]) -> dict | None:
    uniq = {id(c): c for c in cands}
    return next(iter(uniq.values())) if len(uniq) == 1 else None


def _fixture_pool(team: str, records: list[dict]) -> list[dict]:
    """The bookmaker records from ``team``'s fixture; all records if unknown.

    Records cached before fixtures were stored (or a team name we can't pair)
    fall back to the whole day's list, where the uniqueness rules still apply.
    """
    pool = [r for r in records if _in_fixture(r, team)]
    return pool or records


def _loose_match(name: str, pool: list[dict], claimed: set[int]) -> dict | None:
    """Pair a name the exact key missed with one bookmaker record in its fixture.

    Conservative by design — a wrong price is worse than none — so it only
    accepts a single unambiguous candidate:

    * **one-word name on either side** ("Gabriel" ↔ "Gabriel Magalhaes"): that
      word must appear in exactly one bookmaker name in the fixture, so it can't
      pick between "Gabriel Magalhaes" and "Gabriel Martinelli";
    * **one name's words all inside the other's** (2+ words: "Gabriel
      Martinelli" ↔ "Gabriel Martinelli Silva", "Heung-min Son" ↔ "Son
      Heung-min");
    * **same number of words, near-identical spelling** ("Yegor Yarmolyuk" ↔
      "Yehor Yarmoliuk"), with a clear margin over the next-best name.

    Records already matched to another player (``claimed``) are skipped.
    """
    from difflib import SequenceMatcher

    toks = _name_tokens(name)
    if not toks:
        return None
    tset = set(toks)
    free = [r for r in pool if id(r) not in claimed]
    # How many bookmaker names (claimed or not) carry each token, for ambiguity.
    tok_count: dict[str, int] = {}
    for r in pool:
        for t in set(_name_tokens(r.get("name", ""))):
            tok_count[t] = tok_count.get(t, 0) + 1

    cands: list[dict] = []
    for r in free:
        rtoks = _name_tokens(r.get("name", ""))
        rset = set(rtoks)
        if not rtoks:
            continue
        if len(toks) == 1 or len(rtoks) == 1:
            word = toks[0] if len(toks) == 1 else rtoks[0]
            other = rset if len(toks) == 1 else tset
            if len(word) >= 4 and word in other and tok_count.get(word, 0) == 1:
                cands.append(r)
        elif tset <= rset or rset <= tset:
            cands.append(r)
    if len(cands) == 1:
        return cands[0]
    if cands:
        return None  # ambiguous

    full = " ".join(toks)
    scored = sorted(
        ((SequenceMatcher(None, full, _full_key(r.get("name", ""))).ratio(), i)
         for i, r in enumerate(free)
         if len(_name_tokens(r.get("name", ""))) == len(toks)),
        reverse=True)
    if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return free[scored[0][1]]
    return None


def _match_rows(rows: list, odds_index: dict[str, dict],
                aliases: dict[str, list[str]] | None) -> dict[int, dict]:
    """``id(row) -> bookmaker record`` for every row we can price."""
    if aliases is None:
        aliases = load_aliases()
    records = list(odds_index.values())
    by_full: dict[str, list[dict]] = {}
    by_key: dict[str, list[dict]] = {}
    tok_index: dict[str, list] = {}
    for k, rec in odds_index.items():
        by_full.setdefault(_full_key(rec.get("name", "")), []).append(rec)
        # A nameless record is known only by its index key.
        nkey = _player_key(rec["name"]) if rec.get("name") else k.split("#")[0]
        by_key.setdefault(nkey, []).append(rec)
        for t in set(_name_tokens(rec.get("name", ""))):
            tok_index.setdefault(t, []).append(rec)
    alias_full = {_full_key(k): [_full_key(v) for v in vs] for k, vs in aliases.items()}

    matched: dict[int, dict] = {}
    # Pass 1: explicit aliases, then the exact initial+surname key, then mononyms.
    for r in rows:
        name = getattr(r, "player", "")
        team = getattr(r, "team", "")
        rec = None
        for bookie in alias_full.get(_full_key(name), []):
            # The alias may be only part of the bookmaker's name ("Victor" vs
            # "Alexsandro Victor"), so accept any name containing all its words.
            want = set(bookie.split())
            cands = by_full.get(bookie, []) or [
                rec for rec in records
                if want and want <= set(_name_tokens(rec.get("name", "")))]
            scoped = [c for c in cands if _in_fixture(c, team)]
            pick = scoped or cands
            if len(pick) == 1:
                rec = pick[0]
                break
        if not rec:
            # Same full name, then the initial+surname key, then a mononym —
            # each only from this player's own fixture, so a same-key player
            # in another game (J. Larsen of Burnley vs Palace) can't lend his price.
            rec = (_unique([c for c in by_full.get(_full_key(name), [])
                            if _fixture_ok(c, team)])
                   or _unique([c for c in by_key.get(_player_key(name), [])
                               if _fixture_ok(c, team)]))
        if not rec:
            mono = _mononym_match(name, tok_index)
            rec = mono if mono and _fixture_ok(mono, team) else None
        if rec:
            matched[id(r)] = rec

    # Pass 2: looser rules for the rest, only against records nobody claimed.
    claimed = {id(rec) for rec in matched.values()}
    for r in rows:
        if id(r) in matched:
            continue
        pool = _fixture_pool(getattr(r, "team", ""), records)
        rec = _loose_match(getattr(r, "player", ""), pool, claimed)
        if rec:
            matched[id(r)] = rec
            claimed.add(id(rec))
    return matched


def attach_odds(rows: list, odds_index: dict[str, dict],
                aliases: dict[str, list[str]] | None = None) -> int:
    """Set ``bookie_odds`` / ``bookie_source`` on each row from the odds index.

    Matching, most to least certain: a known alias (:data:`PLAYER_ALIASES` plus
    data/player_aliases.json), the accent-folded initial+surname key line-ups
    uses, a mononym fallback (:func:`_mononym_match`), then conservative
    in-fixture rules for differing name forms (:func:`_loose_match`). Returns how
    many rows got a price. Rows whose dataclass lacks the fields are skipped.
    """
    if not odds_index:
        return 0
    n = 0
    matched = _match_rows(rows, odds_index, aliases)
    for r in rows:
        rec = matched.get(id(r))
        if not rec:
            continue
        try:
            r.bookie_odds = float(rec["price"])
            r.bookie_source = str(rec.get("book", ""))
            try:
                r.bookie_all = {str(b): float(p) for b, p in
                                (rec.get("all_books") or {}).items()
                                if b in TARGET_BOOKS and p}
            except (AttributeError, TypeError, ValueError):
                pass
            n += 1
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return n


def unpriced_hints(rows: list, odds_index: dict[str, dict],
                   aliases: dict[str, list[str]] | None = None) -> list[str]:
    """One line per row whose fixture was priced but the player wasn't matched.

    Lists the bookmaker names in that fixture nobody was matched to, so a name
    mismatch (e.g. "Alexsandro Ribeiro" listed as "Victor") is easy to spot and
    add to data/player_aliases.json.
    """
    if not odds_index:
        return []
    matched = _match_rows(rows, odds_index, aliases)
    claimed = {id(rec) for rec in matched.values()}
    records = list(odds_index.values())
    out = []
    for r in rows:
        if id(r) in matched:
            continue
        team = getattr(r, "team", "")
        in_fx = [rec for rec in records if _in_fixture(rec, team)]
        if not in_fx:
            continue  # this game wasn't priced at all — not a name problem
        spare = sorted(rec.get("name", "") for rec in in_fx if id(rec) not in claimed)
        out.append(f"{getattr(r, 'player', '')} ({team}): no matching name; "
                   f"unmatched in that game: {', '.join(spare) or '(none)'}")
    return out


