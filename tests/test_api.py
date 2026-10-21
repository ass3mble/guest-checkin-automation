"""Offline API checks. No booking processing, OCR, or live integrations are invoked."""

import importlib
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class APISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.original_cwd = Path.cwd()
        # The application initializes a default app at import time. Keep both
        # its log and SQLite database outside the repository and all real data.
        repo_root = str(Path(__file__).resolve().parents[1])
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        cls.test_env = patch.dict(
            os.environ,
            {
                "APP_BASE_DIR": cls.temp_dir.name,
                "DATABASE_URL": "sqlite:///data/ci-checkin.db",
                "TELEGRAM_WEBHOOK_SECRET": "synthetic-webhook-secret",
                "TELEGRAM_BOT_TOKEN": "",
                "BEDS24_API_TOKEN": "",
                "BEDS24_REFRESH_TOKEN": "",
                "TTLOCK_CLIENT_ID": "",
                "TTLOCK_CLIENT_SECRET": "",
                "OPENAI_API_KEY": "",
                "AZURE_DOCUMENT_INTELLIGENCE_KEY": "",
                "GOOGLE_SHEET_WEBHOOK_URL": "",
            },
        )
        cls.test_env.start()
        os.chdir(cls.temp_dir.name)
        try:
            module = importlib.import_module("checkin_bot.app")
            cls.parser = staticmethod(module.parse_jotform_payload)
            cls.client = TestClient(module.create_app())
        finally:
            os.chdir(cls.original_cwd)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        logging.shutdown()
        cls.test_env.stop()
        cls.temp_dir.cleanup()

    def test_health_endpoints(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        healthz = self.client.get("/healthz")
        self.assertEqual(healthz.status_code, 200)
        self.assertEqual(healthz.json()["status"], "ok")
        self.assertIn("time_utc", healthz.json())

    def test_missing_booking_returns_not_found(self):
        self.assertEqual(self.client.get("/bookings/nonexistent-test-id").status_code, 404)

    def test_telegram_secret_rejected_when_invalid(self):
        response = self.client.post(
            "/webhooks/telegram",
            json={"message": {"text": "hello"}},
            headers={"x-telegram-bot-api-secret-token": "invalid-test-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_parser_accepts_synthetic_booking(self):
        booking = self.parser(
            {
                "booking": {
                    "external_booking_id": "synthetic-booking",
                    "lead_guest_name": "Sample Guest",
                    "guest_count": 1,
                },
                "guests": [{"guest_index": 1, "full_name": "Sample Guest"}],
                "submission_id": "synthetic-submission",
            }
        )
        self.assertEqual(booking.external_booking_id, "synthetic-booking")
        self.assertEqual(booking.jotform_submission_id, "synthetic-submission")
        self.assertEqual(booking.guests[0].full_name, "Sample Guest")

    def test_parser_maps_two_synthetic_jotform_guests(self):
        booking = self.parser(
            {
                "submissionID": "synthetic-submission-2",
                "q32_BOOKINGID": "synthetic-reservation-2",
                "q5_numGuest": "2",
                "q38_fullName38": "Sample First Guest",
                "q39_fullName39": "Sample Second Guest",
                "q20_birthDate": {"day": "2", "month": "3", "year": "2000"},
            }
        )
        self.assertEqual(booking.external_booking_id, "synthetic-reservation-2")
        self.assertEqual(booking.guest_count, 2)
        self.assertEqual(len(booking.guests), 2)
        self.assertEqual(booking.guests[0].full_name, "Sample First Guest")
        self.assertEqual(booking.guests[1].full_name, "Sample Second Guest")
        self.assertEqual(booking.guests[0].date_of_birth, "2000-03-02")


if __name__ == "__main__":
    unittest.main()
