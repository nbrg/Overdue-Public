"""Robust club-name matching across the naming systems we juggle.

Every source spells clubs differently:

* fixture *schedules* use short forms — "Tottenham", "Manchester Utd",
  "Nott'ham Forest", "Wolves", "Paris S-G".
* *player stats* (our appearance store) use fuller forms —
  "Tottenham Hotspur", "Manchester United", "Nottingham Forest".
* URL slugs — "tottenham-hotspur", "aston-villa".

Matching these with ``==`` silently drops fixtures, squads and prices. This
module gives one canonical form and a derby-safe equality test used wherever we
reconcile a name from one source against another.

The rule is **expand abbreviations, never drop distinguishing words** — so
"Manchester Utd" and "Manchester United" collapse to the same key while
"Manchester City" stays distinct.
"""

from __future__ import annotations

import re
import unicodedata

# Latin letters NFKD doesn't fold to ASCII (shared with lineups).
_LATIN_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l",
    "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ħ": "h",
})


def _unaccent(s: str) -> str:
    folded = str(s).translate(_LATIN_FOLD)
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c)
    )
    return stripped.replace("ß", "ss")


# Word-level expansions: abbreviation -> full word. Applied per token, so they
# never collapse two different clubs (we expand "utd"→"united", we don't drop
# "united"). Keys are already unaccented/lowercased.
_WORD_ALIASES = {
    "utd": "united",
    "nottm": "nottingham",
    "nottham": "nottingham",
    "wton": "wolverhampton",
    "spurs": "tottenham",
    "man": "manchester",
    "sthgn": "southampton",
    "wba": "westbromwich",
    "brommy": "westbromwich",
    "sant": "santander",       # "Racing Sant" -> Racing Santander
    "dep": "deportivo",        # "Dep. A Coruña" -> Deportivo La Coruña
    "atl": "atletico",
    "gladbach": "monchengladbach",
    # French gentilé forms: one source uses the city ("Rennes", "Brest") where
    # another uses the adjective ("Stade Rennais", "Stade Brestois").
    "rennais": "rennes",
    "brestois": "brest",
    "lyonnais": "lyon",
    # Native city spellings vs English ones:
    # "SK Slavia Praha" = "Slavia Prague", "FC Bayern München" = "Bayern Munich".
    "praha": "prague",
    "munchen": "munich",
    "koln": "cologne",
    "kobenhavn": "copenhagen",
    "beograd": "belgrade",
    "lisboa": "lisbon",
}

# Pure filler tokens that carry no distinguishing information across sources.
# Deliberately excludes "united"/"city"/"real"/"athletic" etc. — those tell
# clubs apart, so dropping them would merge derbies.
_FILLERS = {
    "fc", "cf", "afc", "sc", "cd", "ud", "sd", "ca", "club", "and", "de",
    "ss", "if", "bk", "fk", "rc", "cp", "sk",
    # Articles: a schedule may write "Dep. A Coruña" (Galician) where the store has
    # "Deportivo La Coruña" — dropping the article collapses both to the same key.
    "a", "la", "le", "el",
}

# The handful of short forms that don't reduce to their long form by
# abbreviation-expansion alone. Keyed on the *canonical* short form (after
# aliasing/fillers) → canonical long form. Add here when a new mismatch shows up
# via `overdue check-teams`.
_SHORT_FORMS = {
    "parissg": "parissaintgermain",
    "wolves": "wolverhamptonwanderers",
    "psg": "parissaintgermain",
    # "Paris FC" drops its "FC" filler to "paris", which would substring-match
    # "parissaintgermain" and pair Paris FC with PSG.
    "paris": "parisfc",
    # Serie A: "Inter Milan" must not substring-match "Milan" (a derby), so both
    # clubs collapse to their short names before any substring test.
    "intermilan": "inter",
    "internazionale": "inter",
    "internazionalemilano": "inter",
    "acmilan": "milan",
}


def expand_words(name: str) -> list[str]:
    """Tokenise a club name and expand known abbreviations, keeping word order.

    Unlike :func:`canonical` this keeps the words separate (so callers can rebuild
    a hyphenated slug) and does NOT drop fillers. "Racing Sant" → ``["racing",
    "santander"]``; "Manchester Utd" → ``["manchester", "united"]``.
    """
    s = _unaccent(str(name)).lower().replace("'", "").replace("’", "")
    toks = re.sub(r"[^a-z0-9]+", " ", s).split()
    return [_WORD_ALIASES.get(t, t) for t in toks]


def canonical(name: str) -> str:
    """A comparable key for a club name, abbreviation- and accent-insensitive."""
    s = _unaccent(str(name)).lower()
    s = s.replace("'", "").replace("’", "")  # nott'ham -> nottham (one token)
    s = s.replace("&", " and ")
    toks = re.sub(r"[^a-z0-9]+", " ", s).split()
    toks = [_WORD_ALIASES.get(t, t) for t in toks]
    toks = [t for t in toks if t not in _FILLERS]
    canon = "".join(toks)
    return _SHORT_FORMS.get(canon, canon)


def same_team(a: str, b: str) -> bool:
    """Whether two club names (from any of our sources) denote the same club.

    Derby-safe: "Manchester Utd" == "Manchester United" but != "Manchester City".
    Matches on canonical equality, or one canonical form being a substring of the
    other (so "Tottenham" resolves "Tottenham Hotspur"), with a length guard so a
    short token can't spuriously match.
    """
    ca, cb = canonical(a), canonical(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    shorter = ca if len(ca) <= len(cb) else cb
    longer = cb if shorter is ca else ca
    return len(shorter) >= 4 and shorter in longer


def resolve(name: str, candidates) -> str:
    """Return the candidate that best matches ``name`` (canonical), else "".

    Exact canonical match wins; otherwise the shortest substring match, which
    prefers the tightest name (e.g. "Tottenham" over a longer false neighbour).
    """
    if not name:
        return ""
    target = canonical(name)
    if not target:
        return ""
    exact = [c for c in candidates if canonical(c) == target]
    if exact:
        return min(exact, key=len)
    subs = [c for c in candidates if same_team(name, c)]
    return min(subs, key=len) if subs else ""


# Club-affix words that legitimately differ between a club's short (schedule)
# and full (store) name — "Tottenham" vs "Tottenham Hotspur", "Brighton" vs
# "Brighton & Hove Albion", "Betis" vs "Real Betis". Used by :func:`same_club`,
# which only tolerates *these* as the extra words, so "South Liverpool" or
# "Dover Athletic" never collapse onto "Liverpool" / "Athletic Club".
_AFFIX_WORDS = {
    "hotspur", "hove", "albion", "united", "city", "wanderers", "rovers",
    "town", "county", "real", "forest", "villa", "palace", "athletic",
}

# Short forms whose full name has several words — expanded to tokens so the
# whole-word matcher sees the real words (not one concatenated blob).
_SHORT_FORM_NAMES = {
    "wolves": "wolverhampton wanderers",
    "parissg": "paris saint germain",
    "psg": "paris saint germain",
}


def _club_tokens(name: str) -> list[str]:
    """Alias/filler-normalised word list for whole-word club matching."""
    s = _unaccent(str(name)).lower().replace("'", "").replace("’", "")
    s = s.replace("&", " and ")
    toks = [_WORD_ALIASES.get(t, t) for t in re.sub(r"[^a-z0-9]+", " ", s).split()]
    toks = [t for t in toks if t not in _FILLERS]
    canon = "".join(toks)
    if canon in _SHORT_FORM_NAMES:
        return _SHORT_FORM_NAMES[canon].split()
    return toks


def same_club(a: str, b: str) -> bool:
    """Strict whole-word club match: one name's words plus only club affixes.

    Unlike :func:`same_team` this never matches on a mid-word or coincidental
    substring, so it's safe for scanning a whole fixture list against the tracked
    clubs: "Chester" ≠ "Manchester City", "South Liverpool" ≠ "Liverpool",
    "Dover Athletic" ≠ "Athletic Club", "FC United" ≠ "Manchester United". But
    "Tottenham" == "Tottenham Hotspur" and "Betis" == "Real Betis".
    """
    ta, tb = _club_tokens(a), _club_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    n = len(short)
    for i in range(len(long) - n + 1):
        if long[i:i + n] == short:
            extra = long[:i] + long[i + n:]
            if all(t in _AFFIX_WORDS for t in extra):
                return True
    return False
