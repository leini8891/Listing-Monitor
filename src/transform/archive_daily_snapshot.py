from __future__ import annotations

"""
Archive the current daily watchboard outputs into data/history/YYYY-MM-DD/.

This keeps listing facts, token-level market metrics, venue-level ticker
metrics, and derived leaderboard files as separate snapshot artifacts.
"""

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.paths import HISTORY_DIR, SNAPSHOT_SOURCE_FILES, ensure_directory_layout


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HISTORY_ROOT = HISTORY_DIR
SGT_TZ = timezone(timedelta(hours=8), name="SGT")


def log(message: str):
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[Archive Snapshot] [{timestamp}] {message}")


def default_snapshot_date() -> str:
    return datetime.now(timezone.utc).astimezone(SGT_TZ).date().isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive current daily watchboard outputs.")
    parser.add_argument(
        "--date",
        default=default_snapshot_date(),
        help="Snapshot date in YYYY-MM-DD format. Defaults to today in SGT.",
    )
    parser.add_argument(
        "--history-root",
        type=Path,
        default=HISTORY_ROOT,
        help=f"History root directory (default: {HISTORY_ROOT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files in the dated history folder.",
    )
    return parser.parse_args()


def archive_snapshot(snapshot_date: str, history_root: Path, overwrite: bool):
    ensure_directory_layout()
    target_dir = history_root / snapshot_date
    target_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = []
    missing = []

    for source in SNAPSHOT_SOURCE_FILES:
        filename = source.name
        target = target_dir / filename

        if not source.exists():
            missing.append(filename)
            continue

        if target.exists() and not overwrite:
            skipped.append(filename)
            continue

        shutil.copy2(source, target)
        copied += 1

    log(f"Archived snapshot to {target_dir}")
    log(f"Copied files: {copied}")
    if skipped:
        log(f"Skipped existing files: {', '.join(skipped)}")
    if missing:
        log(f"Missing source files: {', '.join(missing)}")


def main():
    args = parse_args()
    archive_snapshot(args.date, args.history_root, args.overwrite)


if __name__ == "__main__":
    main()
