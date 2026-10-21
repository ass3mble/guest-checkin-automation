"""SQLite tests; no production database, accounts, or customer data required."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkin_bot.config import load_settings
from checkin_bot.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        env = patch.dict(
            os.environ,
            {
                "APP_BASE_DIR": self.temp_dir.name,
                "DATABASE_URL": "sqlite:///data/test.db",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.db = Database(load_settings())
        self.db.init()

    def test_database_is_created_inside_temporary_directory(self):
        self.assertEqual(
            self.db.path,
            Path(self.temp_dir.name) / "data" / "test.db",
        )
        self.assertTrue(self.db.path.is_file())

    def test_booking_upsert_updates_existing_record(self):
        record_id = self.db.upsert_booking(
            external_booking_id="synthetic-booking-001",
            jotform_submission_id="synthetic-form-001",
            lead_guest_name="Sample Guest",
            guest_count=1,
            status="pending",
            source_payload={"source": "test"},
        )
        same_id = self.db.upsert_booking(
            external_booking_id="synthetic-booking-001",
            jotform_submission_id=None,
            lead_guest_name="Updated Sample Guest",
            guest_count=1,
            status="review",
            source_payload={"source": "updated-test"},
        )
        self.assertEqual(record_id, same_id)
        record = self.db.fetch_booking(record_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "review")
        self.assertEqual(record.lead_guest_name, "Updated Sample Guest")
        self.assertEqual(
            self.db.fetch_booking_by_external_booking_id("synthetic-booking-001").id,
            record_id,
        )
        self.assertEqual(len(self.db.list_recent_bookings()), 1)

    def test_guest_document_and_review_round_trip(self):
        booking_id = self.db.upsert_booking(
            external_booking_id="synthetic-booking-002",
            jotform_submission_id=None,
            lead_guest_name="Sample Guest",
            guest_count=1,
            status="pending",
            source_payload={},
        )
        guest_id = self.db.upsert_guest(
            booking_id=booking_id,
            guest_index=1,
            payload={"full_name": "Sample Guest"},
        )
        self.db.update_guest_fields(
            guest_id, review_status="approved", fields={"nationality": "TEST"}
        )
        guest = self.db.list_guests(booking_id)[0]
        self.assertEqual(guest.id, guest_id)
        self.assertEqual(guest.review_status, "approved")
        self.assertEqual(guest.nationality, "TEST")

        document_id = self.db.upsert_document(
            guest_id=guest_id,
            doc_type="passport",
            source_url="https://example.invalid/synthetic-document",
        )
        self.db.update_document_after_ocr(
            document_id,
            ocr_status="completed",
            ocr_engine="fake",
            ocr_raw={"test": True},
            extracted={"full_name": "Sample Guest"},
        )
        document = self.db.fetch_document(document_id)
        self.assertEqual(document.ocr_status, "completed")
        self.assertEqual(document.extracted["full_name"], "Sample Guest")
        self.assertEqual(len(self.db.list_documents_for_guest(guest_id)), 1)

        self.db.add_review_action(
            booking_id=booking_id,
            guest_id=guest_id,
            actor="test-reviewer",
            action="approved",
        )
        self.assertEqual(len(self.db.list_review_actions(booking_id)), 1)
        self.db.delete_review_actions(booking_id=booking_id, actor="test-reviewer")
        self.assertEqual(self.db.list_review_actions(booking_id), [])


if __name__ == "__main__":
    unittest.main()
