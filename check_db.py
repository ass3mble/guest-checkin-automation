"""Simple helper to inspect the checkin DB for debugging.

Usage (from project root):
    python check_db.py
    python check_db.py latest
    python check_db.py docs <booking_id>
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("data/checkin.db")

def connect():
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def show_latest(limit=5):
    conn = connect()
    print("=== Latest bookings ===")
    rows = conn.execute(
        "SELECT id, external_booking_id, status, created_at, updated_at "
        "FROM bookings ORDER BY datetime(updated_at) DESC LIMIT ?",
        (limit,)
    ).fetchall()
    for r in rows:
        print(dict(r))
    conn.close()

def show_docs(booking_id=None):
    conn = connect()
    if not booking_id:
        row = conn.execute(
            "SELECT id FROM bookings ORDER BY datetime(updated_at) DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No bookings found")
            conn.close()
            return
        booking_id = row["id"]
        print(f"Using latest booking: {booking_id}\n")

    print(f"=== Documents for booking {booking_id} ===")
    rows = conn.execute(
        """
        SELECT d.doc_type, d.ocr_engine, d.ocr_status, d.last_error,
               g.guest_index, g.full_name
        FROM documents d
        JOIN guests g ON d.guest_id = g.id
        WHERE g.booking_id = ?
        ORDER BY g.guest_index, d.doc_type
        """,
        (booking_id,)
    ).fetchall()

    if not rows:
        print("  (no documents)")
    for r in rows:
        err = (r["last_error"] or "")[:120]
        print({
            "guest": r["guest_index"],
            "doc_type": r["doc_type"],
            "engine": r["ocr_engine"],
            "status": r["ocr_status"],
            "error": err if err else None
        })
    conn.close()

def show_summary():
    conn = connect()
    print("=== Status counts ===")
    for r in conn.execute("SELECT status, COUNT(*) as c FROM bookings GROUP BY status").fetchall():
        print(dict(r))

    print("\n=== Recent review actions ===")
    rows = conn.execute(
        "SELECT booking_id, action, actor, created_at FROM review_actions "
        "ORDER BY datetime(created_at) DESC LIMIT 8"
    ).fetchall()
    for r in rows:
        print(dict(r))
    conn.close()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "latest"

    if cmd == "latest":
        show_latest()
    elif cmd == "docs":
        bid = sys.argv[2] if len(sys.argv) > 2 else None
        show_docs(bid)
    elif cmd == "summary":
        show_summary()
    else:
        print("Usage:")
        print("  python check_db.py            # latest bookings")
        print("  python check_db.py latest")
        print("  python check_db.py docs [booking_id]")
        print("  python check_db.py summary")
