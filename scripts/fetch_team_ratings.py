"""Refresh .data/team_ratings.json from Valve's official CS2 regional standings.

Valve publishes the global standings as markdown in a public GitHub repo,
updated roughly monthly. HLTV was the obvious source but it is behind
Cloudflare and 403s every non-browser request; Valve's repo is the actual
official ranking and needs no scraping tricks.

    uv run python scripts/fetch_team_ratings.py

Run it after each monthly update (or from cron); the bot reads the JSON at
startup and works fine — just without the rating prior — if the file is absent.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_REPO = "ValveSoftware/counter-strike_regional_standings"
_RAW = f"https://raw.githubusercontent.com/{_REPO}/main"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
# "| 1        |   1993 | Spirit               | donk, magixx, ... |"
_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        data: bytes = response.read()
        return data


def _latest_standings_path() -> str:
    """Newest live/<year>/standings_global_<date>.md in the repo."""
    year = datetime.now(timezone.utc).year
    for candidate_year in (year, year - 1):
        listing = json.loads(
            _get(f"https://api.github.com/repos/{_REPO}/contents/live/{candidate_year}")
        )
        globals_ = sorted(
            str(entry["path"]) for entry in listing if entry["name"].startswith("standings_global_")
        )
        if globals_:
            return globals_[-1]
    raise RuntimeError("No standings_global_*.md found in Valve's repo.")


def main() -> int:
    path = _latest_standings_path()
    print(f"Fetching {path}")
    markdown = _get(f"{_RAW}/{path}").decode("utf-8", "replace")

    teams = {name: int(points) for _, points, name in _ROW_RE.findall(markdown)}
    if not teams:
        raise RuntimeError(f"Parsed 0 teams from {path} — the table format changed.")

    out = Path(".data/team_ratings.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"source": path, "fetched_at": datetime.now(timezone.utc).isoformat(), "teams": teams},
            indent=2,
            sort_keys=True,
        )
    )
    best = max(teams.items(), key=lambda kv: kv[1])
    print(f"Wrote {len(teams)} teams to {out} (top: {best[0]} {best[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
