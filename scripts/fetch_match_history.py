"""Refresh .data/match_history.json — every professional CS2 map bo3.gg has,
with its date, map name, and who won.

    uv run python scripts/fetch_match_history.py            # incremental
    uv run python scripts/fetch_match_history.py --days 730 # backfill

This is the data source behind MapForm (app/services/match_history.py), the
map-aware strength model. It replaces the HLTV scrape in the reference project
(TaiZo1/cs2-win-prediction) — see app/services/bo3.py for why HLTV is not an
option here.

Incremental by default: it resumes from the newest map already stored and asks
bo3.gg only for what came after, which is a handful of requests a day. The
first run has nothing to resume from and backfills `--days` (default 730).

Two years is deliberate. Elo needs history to converge, but CS2 rosters turn
over fast enough that 2021 results say nothing about who wins tonight, and the
fit in scripts/fit_map_form.py down-weights old maps anyway.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import bo3
from app.utils.http import CloudflareBlocked

DEFAULT_PATH = Path(".data/match_history.json")


def load(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        stored: list[dict[str, object]] = json.loads(path.read_text())["games"]
        return stored
    except (json.JSONDecodeError, OSError, KeyError):
        print(f"{path} is unreadable — starting over.")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=730, help="backfill window on a first run")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--full", action="store_true", help="ignore what is stored and refetch")
    args = parser.parse_args()

    games = [] if args.full else load(args.path)
    if games:
        # Resume one day before the newest stored map: bo3 backfills late
        # results, so re-walking a day is cheap insurance against a hole.
        newest = max(str(g["at"]) for g in games)
        since = datetime.strptime(newest, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(
            days=1
        )
        print(f"{len(games):,} maps stored, newest {newest} — fetching from {since:%Y-%m-%d}")
    else:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        print(f"No history stored — backfilling {args.days} days from {since:%Y-%m-%d}")

    seen = {(str(g["at"]), str(g["map"]), str(g["w"]), str(g["l"])) for g in games}
    added = 0
    started = time.monotonic()
    try:
        for raw in bo3.finished_games(since):
            trimmed = bo3.trim(raw)
            if trimmed is None:
                continue
            key = (trimmed["at"], trimmed["map"], trimmed["w"], trimmed["l"])
            if key in seen:
                continue
            seen.add(key)
            games.append(trimmed)
            added += 1
            if added % 500 == 0:
                print(f"  +{added:,} ({time.monotonic() - started:.0f}s)", flush=True)
    except CloudflareBlocked as exc:
        print(f"Blocked partway through ({exc}); saving what we got.")
    except KeyboardInterrupt:
        print("Interrupted; saving what we got.")

    if not games:
        print("Fetched nothing and had nothing stored — leaving the file alone.")
        return 1

    games.sort(key=lambda g: (str(g["at"]), str(g["map"]), str(g["w"])))
    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(
        json.dumps(
            {
                "source": "bo3.gg/api/v1/games",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "games": games,
            },
            separators=(",", ":"),
        )
    )
    maps = len({str(g["map"]) for g in games})
    teams = len({str(g["w"]) for g in games} | {str(g["l"]) for g in games})
    span = f"{min(str(g['at']) for g in games)} .. {max(str(g['at']) for g in games)}"
    size = args.path.stat().st_size / 1e6
    print(f"+{added:,} new. {len(games):,} maps, {teams:,} teams, {maps} map names, {span}")
    print(f"Wrote {args.path} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
