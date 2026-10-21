"""Read-only aggregate SQLite usage counts; never emits booking-level records."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def summary(db_path: Path) -> dict[str, int]:
    db_path = db_path.expanduser().resolve(strict=True)
    if not db_path.is_file():
        raise ValueError("Database path is not a file.")
    # URI mode=ro prevents writing even when the current account can modify files.
    uri = db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return {
            "bookings_total": connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0],
            "bookings_last_30_days": connection.execute(
                "SELECT COUNT(*) FROM bookings WHERE datetime(created_at) >= datetime('now', '-30 days')"
            ).fetchone()[0],
            "guest_rows_total": connection.execute("SELECT COUNT(*) FROM guests").fetchone()[0],
            "document_rows_total": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/checkin.db"))
    args = parser.parse_args()
    try:
        stats = summary(args.db)
    except (OSError, ValueError, sqlite3.Error):
        print("Unable to read local SQLite schema; verify --db and file permissions.")
        return 1
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("Aggregate counts only; no guest-level records were printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
