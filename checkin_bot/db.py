from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .schemas import BookingRecord, DocumentRecord, GuestRecord, ReviewActionRecord


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS bookings (
                    id TEXT PRIMARY KEY,
                    external_booking_id TEXT NOT NULL UNIQUE,
                    jotform_submission_id TEXT UNIQUE,
                    lead_guest_name TEXT,
                    guest_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    instructions_sent INTEGER NOT NULL DEFAULT 0,
                    source_payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guests (
                    id TEXT PRIMARY KEY,
                    booking_id TEXT NOT NULL,
                    guest_index INTEGER NOT NULL,
                    full_name TEXT,
                    date_of_birth TEXT,
                    gender TEXT,
                    nationality TEXT,
                    passport_number TEXT,
                    visa_number TEXT,
                    review_status TEXT NOT NULL,
                    source_payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
                    UNIQUE (booking_id, guest_index)
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    guest_id TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    storage_path TEXT,
                    file_hash TEXT,
                    ocr_status TEXT NOT NULL,
                    ocr_engine TEXT,
                    ocr_raw_json TEXT,
                    extracted_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (guest_id) REFERENCES guests(id) ON DELETE CASCADE,
                    UNIQUE (guest_id, doc_type)
                );

                CREATE TABLE IF NOT EXISTS review_actions (
                    id TEXT PRIMARY KEY,
                    booking_id TEXT NOT NULL,
                    guest_id TEXT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    note TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
                    FOREIGN KEY (guest_id) REFERENCES guests(id) ON DELETE SET NULL
                );
                """
            )

    def upsert_booking(
        self,
        *,
        external_booking_id: str,
        jotform_submission_id: str | None,
        lead_guest_name: str,
        guest_count: int,
        status: str,
        source_payload: dict[str, Any],
    ) -> str:
        booking_id = self._find_booking_id(external_booking_id)
        now = utc_now()
        payload_json = json.dumps(source_payload, ensure_ascii=True)
        if booking_id is None:
            booking_id = str(uuid.uuid4())
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO bookings (
                        id, external_booking_id, jotform_submission_id, lead_guest_name,
                        guest_count, status, source_payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        booking_id,
                        external_booking_id,
                        jotform_submission_id,
                        lead_guest_name,
                        guest_count,
                        status,
                        payload_json,
                        now,
                        now,
                    ),
                )
        else:
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE bookings
                    SET jotform_submission_id = COALESCE(?, jotform_submission_id),
                        lead_guest_name = ?,
                        guest_count = ?,
                        status = ?,
                        source_payload_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        jotform_submission_id,
                        lead_guest_name,
                        guest_count,
                        status,
                        payload_json,
                        now,
                        booking_id,
                    ),
                )
        return booking_id

    def update_booking_status(self, booking_id: str, status: str, *, instructions_sent: bool | None = None) -> None:
        now = utc_now()
        with self.connect() as connection:
            if instructions_sent is None:
                connection.execute(
                    "UPDATE bookings SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, booking_id),
                )
            else:
                connection.execute(
                    "UPDATE bookings SET status = ?, instructions_sent = ?, updated_at = ? WHERE id = ?",
                    (status, int(instructions_sent), now, booking_id),
                )

    def update_booking_source_payload(self, booking_id: str, source_payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE bookings SET source_payload_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(source_payload, ensure_ascii=True), utc_now(), booking_id),
            )

    def upsert_guest(self, *, booking_id: str, guest_index: int, payload: dict[str, Any]) -> str:
        guest_id = self._find_guest_id(booking_id, guest_index)
        now = utc_now()
        payload_json = json.dumps(payload, ensure_ascii=True)
        if guest_id is None:
            guest_id = str(uuid.uuid4())
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO guests (
                        id, booking_id, guest_index, full_name, date_of_birth, gender,
                        nationality, passport_number, visa_number, review_status,
                        source_payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guest_id,
                        booking_id,
                        guest_index,
                        payload.get("full_name"),
                        payload.get("date_of_birth"),
                        payload.get("gender"),
                        payload.get("nationality"),
                        payload.get("passport_number"),
                        payload.get("visa_number"),
                        payload.get("review_status") or "pending_ocr",
                        payload_json,
                        now,
                        now,
                    ),
                )
        else:
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE guests
                    SET full_name = ?, date_of_birth = ?, gender = ?, nationality = ?,
                        passport_number = ?, visa_number = ?, review_status = ?,
                        source_payload_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload.get("full_name"),
                        payload.get("date_of_birth"),
                        payload.get("gender"),
                        payload.get("nationality"),
                        payload.get("passport_number"),
                        payload.get("visa_number"),
                        payload.get("review_status") or "pending_ocr",
                        payload_json,
                        now,
                        guest_id,
                    ),
                )
        return guest_id

    def update_guest_fields(self, guest_id: str, *, review_status: str | None = None, fields: dict[str, Any] | None = None) -> None:
        fields = fields or {}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        if review_status is not None:
            assignments.append("review_status = ?")
            values.append(review_status)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(guest_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE guests SET {', '.join(assignments)} WHERE id = ?", values)

    def upsert_document(self, *, guest_id: str, doc_type: str, source_url: str) -> str:
        document_id = self._find_document_id(guest_id, doc_type)
        now = utc_now()
        if document_id is None:
            document_id = str(uuid.uuid4())
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO documents (
                        id, guest_id, doc_type, source_url, ocr_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (document_id, guest_id, doc_type, source_url, "pending", now, now),
                )
        else:
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE documents
                    SET source_url = ?, ocr_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (source_url, "pending", now, document_id),
                )
        return document_id

    def update_document_after_ocr(
        self,
        document_id: str,
        *,
        ocr_status: str,
        ocr_engine: str,
        ocr_raw: dict[str, Any] | None,
        extracted: dict[str, Any] | None,
        last_error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE documents
                SET ocr_status = ?, ocr_engine = ?, ocr_raw_json = ?, extracted_json = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ocr_status,
                    ocr_engine,
                    json.dumps(ocr_raw, ensure_ascii=True) if ocr_raw is not None else None,
                    json.dumps(extracted, ensure_ascii=True) if extracted is not None else None,
                    last_error,
                    utc_now(),
                    document_id,
                ),
            )

    def add_review_action(
        self,
        *,
        booking_id: str,
        guest_id: str | None,
        actor: str,
        action: str,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        review_action_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO review_actions (id, booking_id, guest_id, actor, action, note, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_action_id,
                    booking_id,
                    guest_id,
                    actor,
                    action,
                    note,
                    json.dumps(payload or {}, ensure_ascii=True),
                    utc_now(),
                ),
            )
        return review_action_id

    def delete_review_actions(
        self,
        *,
        booking_id: str,
        actor: str | None = None,
        actions: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        clauses = ["booking_id = ?"]
        values: list[Any] = [booking_id]
        if actor is not None:
            clauses.append("actor = ?")
            values.append(actor)
        if actions:
            placeholders = ", ".join("?" for _ in actions)
            clauses.append(f"action IN ({placeholders})")
            values.extend(actions)
        with self.connect() as connection:
            connection.execute(
                f"DELETE FROM review_actions WHERE {' AND '.join(clauses)}",
                values,
            )

    def fetch_booking(self, booking_id: str) -> BookingRecord | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return None if row is None else BookingRecord.from_row(row)

    def fetch_booking_by_external_booking_id(self, external_booking_id: str) -> BookingRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bookings WHERE external_booking_id = ?",
                (external_booking_id,),
            ).fetchone()
        return None if row is None else BookingRecord.from_row(row)

    def fetch_booking_by_submission_id(self, jotform_submission_id: str) -> BookingRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bookings WHERE jotform_submission_id = ?",
                (jotform_submission_id,),
            ).fetchone()
        return None if row is None else BookingRecord.from_row(row)

    def list_recent_bookings(self, limit: int = 5) -> list[BookingRecord]:
        limit = max(1, min(limit, 20))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bookings ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [BookingRecord.from_row(row) for row in rows]

    def list_guests(self, booking_id: str) -> list[GuestRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM guests WHERE booking_id = ? ORDER BY guest_index",
                (booking_id,),
            ).fetchall()
        return [GuestRecord.from_row(row) for row in rows]

    def fetch_document(self, document_id: str) -> DocumentRecord | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return None if row is None else DocumentRecord.from_row(row)

    def list_documents_for_guest(self, guest_id: str) -> list[DocumentRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM documents WHERE guest_id = ? ORDER BY doc_type",
                (guest_id,),
            ).fetchall()
        return [DocumentRecord.from_row(row) for row in rows]

    def list_review_actions(self, booking_id: str) -> list[ReviewActionRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_actions WHERE booking_id = ? ORDER BY created_at",
                (booking_id,),
            ).fetchall()
        return [ReviewActionRecord.from_row(row) for row in rows]

    def list_bookings_by_status(self, statuses: list[str] | tuple[str, ...]) -> list[BookingRecord]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM bookings WHERE status IN ({placeholders}) ORDER BY updated_at",
                list(statuses),
            ).fetchall()
        return [BookingRecord.from_row(row) for row in rows]

    def _find_booking_id(self, external_booking_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM bookings WHERE external_booking_id = ?",
                (external_booking_id,),
            ).fetchone()
        return None if row is None else str(row["id"])

    def _find_guest_id(self, booking_id: str, guest_index: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM guests WHERE booking_id = ? AND guest_index = ?",
                (booking_id, guest_index),
            ).fetchone()
        return None if row is None else str(row["id"])

    def _find_document_id(self, guest_id: str, doc_type: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM documents WHERE guest_id = ? AND doc_type = ?",
                (guest_id, doc_type),
            ).fetchone()
        return None if row is None else str(row["id"])
