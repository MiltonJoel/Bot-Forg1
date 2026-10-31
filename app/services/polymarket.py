"""Polymarket's price for the exact map a forg1 market is about.

The owner's CounterPredictor (`--polymarket`, polymarket.py there) found that
Polymarket's closing price beats his model on CS2 series (log loss 0.589 vs
0.625 over 509 series) and that blending the two does not help. On the 49
logged forg1 map markets this exact code matches to a cached Polymarket price
(2026-10-19, pool at decision time): pool 0.789 log loss, Polymarket 0.622
(CI on the gap +0.027 to +0.336), CounterPredictor v1 0.637 on the same 49
(v1 minus Polymarket +0.015, CI -0.025 to +0.051: a tie, slightly ahead).

forg1's "Quien gana el 2do MAPA?" settles on one map, which is Polymarket's
"<A> vs <B> - Map 2 Winner" market inside the series event. Stdlib port of the
lookup, stricter than the CLI's on purpose, because here a wrong match stakes
real points:

  * both teams must match EXACTLY after normalisation (name or Polymarket's
    abbreviation), no fuzzy fallback;
  * exactly one such event starting between 12 h ago and 3 h from now;
  * exactly one "Map N Winner" market, and each outcome must map back to one
    forg1 label through the event's own team list (outcome names can differ
    from the labels: "Natus Vincere" for forg1's "NAVI").

Anything else returns None and the bot prices exactly as before.

Price: before the series start, the live midpoint (only if the spread is at
most 0.20, wider means nobody is trading); after it, the last traded price
before the start, so it is never an in-play number. For map 2+ that is the
pre-series price of that map.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.counter_predictor import key
from app.utils import http as http_ipv4

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com/prices-history"
CS2_SERIES = 10310
MAX_SPREAD = 0.20
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def _get(url: str, params: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    with http_ipv4.urlopen(request, timeout=20.0) as response:
        return json.loads(response.read())


def _same(label: str, team: dict[str, Any]) -> bool:
    names = {key(str(team.get("name") or "")), key(str(team.get("abbreviation") or ""))}
    return key(label) in names - {""}


def _events(now: datetime) -> list[dict[str, Any]]:
    """Open CS2 events listed around now (startDate is the listing date, so
    the window is wide; the real start is checked on startTime)."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _get(
            GAMMA,
            {
                "series_id": CS2_SERIES,
                "closed": "false",
                "limit": 100,
                "offset": offset,
                "start_date_min": (now - timedelta(days=10)).strftime("%Y-%m-%d"),
                "start_date_max": (now + timedelta(days=2)).strftime("%Y-%m-%d"),
            },
        )
        out += page
        if len(page) < 100:
            return out
        offset += 100


def _price(market: dict[str, Any], start: datetime, now: datetime) -> float | None:
    if now < start:
        bid, ask = market.get("bestBid"), market.get("bestAsk")
        if bid is None or ask is None or float(ask) - float(bid) > MAX_SPREAD:
            return None
        return (float(bid) + float(ask)) / 2
    token = json.loads(market["clobTokenIds"])[0]
    t = int(start.timestamp())
    history = _get(CLOB, {"market": token, "startTs": t - 2 * 86400, "endTs": t, "fidelity": 10})
    points = [float(h["p"]) for h in history.get("history", []) if h["t"] < t]
    return points[-1] if points else None


def map_probability(
    labels: tuple[str, str], number: int, now: datetime | None = None
) -> dict[str, float] | None:
    """{label: P(that team wins map `number`)} from Polymarket, or None
    unless the match is certain. Blocking I/O — call it off the event loop."""
    now = now or datetime.now(timezone.utc)
    a, b = labels
    if key(a) == key(b):
        return None
    hits = []
    for event in _events(now):
        teams = event.get("teams") or []
        if len(teams) != 2 or not event.get("startTime"):
            continue
        start = datetime.fromisoformat(str(event["startTime"]).replace("Z", "+00:00"))
        if not timedelta(hours=-12) <= start - now <= timedelta(hours=3):
            continue
        if (_same(a, teams[0]) and _same(b, teams[1])) or (
            _same(a, teams[1]) and _same(b, teams[0])
        ):
            hits.append((event, start))
    if len(hits) != 1:
        return None
    event, start = hits[0]
    markets = [
        m
        for m in event.get("markets") or []
        if str(m.get("question", "")).endswith(f"Map {number} Winner")
    ]
    if len(markets) != 1:
        return None
    market = markets[0]
    outcomes = json.loads(market["outcomes"])
    owners = []  # which forg1 label each outcome belongs to, via the event's team list
    for name in outcomes:
        team = [t for t in event["teams"] if t.get("name") == name]
        owner = [label for label in labels if team and _same(label, team[0])]
        if len(owner) != 1:
            return None
        owners.append(owner[0])
    if len(owners) != 2 or owners[0] == owners[1]:
        return None
    p = _price(market, start, now)
    if p is None or not 0.0 < p < 1.0:
        return None
    return {owners[0]: p, owners[1]: 1.0 - p}
