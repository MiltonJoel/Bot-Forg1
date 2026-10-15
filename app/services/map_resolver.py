"""Which CS2 map is a forg1 market actually about?

The reference project (TaiZo1/cs2-win-prediction) can key everything on the map
because it starts from a demo file that says so. We start from a Spanish market
title, and only ONE of the 88 logged markets ever named a map outright
("Quien gana el INFERNO?"). The other 55 head-to-heads say "1er MAPA", "2do
MAPA", "3er MAPA" — an ordinal within a series whose veto forg1 never shows.

So the map has to be recovered from outside: the two outcome labels are the two
teams, the title gives the ordinal, and bo3.gg knows which map that series is
playing. It works live because bo3 populates `map_name` while a map is still
`status=current`, which is exactly when forg1's market is open.

Everything here degrades to None rather than guessing. A wrong map is worse
than no map: it would price Vitality's Nuke form onto their Mirage.
"""

import re

from app.services import bo3
from app.services.match_history import normalize_map
from app.services.team_ratings import _normalize
from app.utils.http import CloudflareBlocked

# forg1's ordinals, as actually written in the logged titles.
_ORDINALS = {
    "1er": 1,
    "1ro": 1,
    "1": 1,
    "primer": 1,
    "primero": 1,
    "2do": 2,
    "2da": 2,
    "2": 2,
    "segundo": 2,
    "3er": 3,
    "3ro": 3,
    "3": 3,
    "tercer": 3,
    "tercero": 3,
    "4to": 4,
    "4": 4,
    "cuarto": 4,
    "5to": 5,
    "5": 5,
    "quinto": 5,
}
# The active pool plus the maps bo3 still returns results on.
_MAP_NAMES = frozenset(
    {
        "inferno",
        "mirage",
        "nuke",
        "dust2",
        "ancient",
        "anubis",
        "train",
        "overpass",
        "vertigo",
        "cache",
        "cobblestone",
        "office",
        "italy",
    }
)
_WORD = re.compile(r"[a-z0-9]+")


def map_in_title(title: str) -> str | None:
    """'Quien gana el INFERNO?' -> 'inferno'. None when the title names no map."""
    words: list[str] = _WORD.findall(title.lower())
    for word in words:
        if word in _MAP_NAMES:
            return word
        stripped = normalize_map(word)
        if stripped in _MAP_NAMES:
            return stripped
    return None


def map_number_in_title(title: str) -> int | None:
    """'Quien gana el 2do MAPA?' -> 2. Only counts when the word MAPA is
    actually there, so 'Cuantas kills DONK? MAPA 2' resolves and 'Quien gana
    el OT?' does not."""
    words: list[str] = _WORD.findall(title.lower())
    if not any(w.startswith("mapa") or w.startswith("map") for w in words):
        return None
    for index, word in enumerate(words):
        if word in _ORDINALS and (
            (index + 1 < len(words) and words[index + 1].startswith("map"))
            or (index and words[index - 1].startswith("map"))
        ):
            return _ORDINALS[word]
    return None


def _side_matches(slug_side: str, label: str) -> bool:
    """Does this half of a bo3 slug name this forg1 team?

    Exact match, or one name contained in the other — bo3 carries academy and
    sponsor suffixes forg1 drops ('inner-circle-academy' vs 'Inner Circle',
    '9z Globant' vs '9z'). The 3-character floor is what stops 'LP' from being
    served 'LPH': short labels must match exactly.
    """
    side, team = _normalize(slug_side), _normalize(label)
    if not side or not team:
        return False
    if side == team:
        return True
    shorter, longer = sorted((side, team), key=len)
    return len(shorter) >= 4 and shorter in longer


def match_for(slug: str, labels: tuple[str, str]) -> bool:
    """True when a bo3 match slug is this market's match.

    BOTH teams have to line up, in either orientation. One-sided matching would
    happily pair 'Spirit vs G2' with 'Spirit vs FaZe' playing on another stream
    at the same moment.
    """
    head = slug.rsplit("-", 3)[0] if re.search(r"-\d{2}-\d{2}-\d{4}$", slug) else slug
    if "-vs-" not in head:
        return False
    left, right = head.split("-vs-", 1)
    first, second = labels
    return (_side_matches(left, first) and _side_matches(right, second)) or (
        _side_matches(left, second) and _side_matches(right, first)
    )


def resolve_live_map(title: str, labels: tuple[str, str]) -> str | None:
    """The map this market is about, from bo3.gg's live matches. Blocking I/O —
    call it off the event loop.

    Returns None on anything unclear: no live match for these teams, several
    matching (two of the same pairing), or no game at that ordinal yet. The
    caller then prices without a map, which is the honest fallback.
    """
    named = map_in_title(title)
    if named:
        return named
    number = map_number_in_title(title)
    if number is None:
        return None
    try:
        candidates = [m for m in bo3.live_matches() if match_for(str(m.get("slug", "")), labels)]
        if len(candidates) != 1:
            return None
        for game in bo3.games_for_match(int(candidates[0]["id"])):
            if int(game.get("number") or 0) == number and game.get("map_name"):
                return normalize_map(str(game["map_name"]))
    except CloudflareBlocked as exc:
        bo3.log_block(exc)
    except (KeyError, ValueError, TypeError, OSError):
        return None
    return None


def resolve_stored_map(
    games: list[dict[str, object]], date: str, labels: tuple[str, str], number: int | None
) -> str | None:
    """Same answer, offline, from .data/match_history.json — this is how a
    finished market gets its map back so the model can be scored on it.

    `date` is the day the market resolved; a series can straddle midnight UTC,
    so the day before counts too. Ambiguity (the same two teams twice) returns
    None rather than picking one.
    """
    first, second = labels
    hits = {
        str(g["map"])
        for g in games
        if str(g["at"]) <= date
        and str(g["at"]) >= _day_before(date)
        and (
            (_side_matches(str(g["w"]), first) and _side_matches(str(g["l"]), second))
            or (_side_matches(str(g["w"]), second) and _side_matches(str(g["l"]), first))
        )
        and (number is None or int(str(g.get("n") or 0)) == number)
    }
    return normalize_map(hits.pop()) if len(hits) == 1 else None


def _day_before(date: str) -> str:
    from datetime import datetime, timedelta

    return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
