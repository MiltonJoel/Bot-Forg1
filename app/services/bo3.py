"""bo3.gg's public JSON API: finished CS2 maps, and which map is live right now.

Why not HLTV, which is what the reference project (TaiZo1/cs2-win-prediction)
scrapes: HLTV 403s every non-browser request, and — measured 2026-08-28 — it
also serves a Cloudflare interstitial to real headful Chrome driven by
Playwright, so the persistent profile that gets us into forg1 does NOT get us
into HLTV. The reference project's answer is undetected-chromedriver, a
permanent arms race we are not going to run on a 2GB VPS next to the betting
browser. bo3.gg answers plain urllib with the same facts.

What it gives us, per map ("game") rather than per match, which is exactly the
grain forg1's "Quien gana el 2do MAPA?" markets settle on:

    map_name  de_inferno          number             2   (map 2 of the series)
    winner_clan_name / loser_clan_name               team names as plain strings
    winner_clan_score / loser_clan_score             13-10
    begin_at                                         for the date cutoff
    status    finished | current | upcoming

`map_name` is populated while a map is still `current`, which is what makes
live map resolution possible at all: forg1 opens the market as the map starts,
before anyone has won anything.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from loguru import logger

from app.utils import http as http_ipv4
from app.utils.http import CloudflareBlocked

_BASE = "https://api.bo3.gg/api/v1"
# Same browser UA the Kick checker uses. bo3.gg does not challenge us today;
# this is politeness plus insurance against a default-python-urllib blocklist.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
# The API caps a page at 100 no matter what you ask for (asking 500 returns 100).
PAGE_LIMIT = 100


def _get(path: str, params: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """One GET against the API. Raises CloudflareBlocked if we ever get walled.

    Note the query string must be percent-encoded: the API's filter syntax is
    `filter[games.begin_at][gt]=...` and raw brackets make the request fail
    before it reaches the server.
    """
    url = f"{_BASE}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with http_ipv4.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            reason = http_ipv4.cloudflare_reason(response.status, dict(response.headers), body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        reason = http_ipv4.cloudflare_reason(exc.code, dict(exc.headers), body)
        if reason:
            raise CloudflareBlocked(f"bo3.gg: {reason}") from exc
        raise
    if reason:
        raise CloudflareBlocked(f"bo3.gg: {reason}")
    parsed: dict[str, Any] = json.loads(body)
    return parsed


def _paginate(path: str, params: dict[str, str], timeout: float = 30.0) -> Iterator[dict[str, Any]]:
    """Walk every page of a listing endpoint.

    `total.count` is the count for the FILTER, not the page, so it is the loop
    bound. It can move while we walk (matches finish mid-scrape), hence the
    empty-page break rather than trusting the count alone.
    """
    offset = 0
    while True:
        page = dict(params, **{"page[limit]": str(PAGE_LIMIT), "page[offset]": str(offset)})
        payload = _get(path, page, timeout)
        results: list[dict[str, Any]] = payload.get("results", [])
        if not results:
            return
        yield from results
        offset += len(results)
        if offset >= payload.get("total", {}).get("count", 0):
            return


def finished_games(since: datetime, until: datetime | None = None) -> Iterator[dict[str, Any]]:
    """Every map played after `since`, oldest first.

    Sorted ascending on purpose: an incremental refresh resumes from the newest
    row it already has, and ascending order means a page boundary can't skip a
    row when new maps finish while we are walking.
    """
    params = {
        "sort": "begin_at",
        "filter[games.begin_at][gt]": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if until is not None:
        params["filter[games.begin_at][lt]"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    for game in _paginate("games", params):
        if game.get("status") == "finished" and game.get("winner_clan_name"):
            yield game


def live_matches(timeout: float = 15.0) -> list[dict[str, Any]]:
    """Matches currently being played. `slug` carries both team names
    ("sparta-vs-inner-circle-academy-28-08-2026"), which is how we find the
    match for a forg1 market — the teams are the only thing both sites agree on.
    """
    payload = _get(
        "matches",
        {
            "filter[matches.status][eq]": "current",
            "sort": "-start_date",
            "page[limit]": str(PAGE_LIMIT),
        },
        timeout,
    )
    matches: list[dict[str, Any]] = payload.get("results", [])
    return matches


def games_for_match(match_id: int, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Every map of one match, including the one in progress (`status=current`),
    which still carries its `map_name`."""
    payload = _get(
        "games",
        {
            "filter[games.match_id][eq]": str(match_id),
            "sort": "number",
            "page[limit]": str(PAGE_LIMIT),
        },
        timeout,
    )
    games: list[dict[str, Any]] = payload.get("results", [])
    return games


def trim(game: dict[str, Any]) -> dict[str, Any] | None:
    """The five fields the form model actually needs, or None for an unusable
    row. Storing the raw payload would be ~5x the disk for a demo_header blob
    we never read."""
    winner, loser = game.get("winner_clan_name"), game.get("loser_clan_name")
    if not winner or not loser or not game.get("map_name") or not game.get("begin_at"):
        return None
    return {
        "at": str(game["begin_at"])[:10],
        "map": str(game["map_name"]),
        "w": str(winner),
        "l": str(loser),
        "ws": int(game.get("winner_clan_score") or 0),
        "ls": int(game.get("loser_clan_score") or 0),
        # Map number within the series, and the match it belongs to. These are
        # what let the validation answer "which map WAS the 2do MAPA of that
        # night's match?" for the many forg1 markets that name only an ordinal.
        "n": int(game.get("number") or 0),
        "m": int(game.get("match_id") or 0),
    }


def log_block(exc: CloudflareBlocked) -> None:
    """bo3.gg being walled is not fatal anywhere: every caller falls back to the
    cached history, and the strategy falls back to the pool price."""
    logger.warning("bo3.gg is blocked ({}) — using the cached match history.", exc)
