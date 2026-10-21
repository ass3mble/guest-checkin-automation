"""Security contract tests, without network or production secrets."""

import asyncio
import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from PIL import Image

from checkin_bot.config import load_settings
from checkin_bot.integrations import OCRProvider, TelegramClient
from checkin_bot.security import bearer_token, require_token, validated_download_url
from tools.usage_metrics import summary
from checkin_bot.db import Database


class SecretTests(unittest.TestCase):
    def test_unconfigured_and_wrong_tokens_fail_closed(self):
        for expected, supplied, status in (
            ("", "", 503),
            ("known", None, 401),
            ("known", "incorrect", 401),
        ):
            with self.subTest(status=status), self.assertRaises(HTTPException) as exc:
                require_token(expected, supplied, purpose="Test")
            self.assertEqual(exc.exception.status_code, status)
        require_token("known", "known", purpose="Test")

    def test_bearer_parser(self):
        self.assertEqual(bearer_token("Bearer abc"), "abc")
        self.assertEqual(bearer_token("bearer abc"), "abc")
        for value in (None, "", "Basic abc", "Bearer", "Bearer abc ", "Bearer "):
            self.assertIsNone(bearer_token(value))

    def test_document_url_allowlist(self):
        allowlist = ("jotform.com", "jotformusercontent.com")
        for url in (
            "http://www.jotform.com/uploads/demo",
            "https://127.0.0.1/secret",
            "https://localhost/private",
            "https://jotform.com.evil.invalid/upload",
            "https://evil.invalid/?next=jotform.com",
            "https://user:pass@jotform.com/upload",
            "https://jotform.com:8443/upload",
            "file:///etc/passwd",
            "https://[::1]/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validated_download_url(url, allowlist)
        self.assertEqual(
            validated_download_url("https://www.jotform.com/uploads/synthetic.png", allowlist),
            "https://www.jotform.com/uploads/synthetic.png",
        )

    def test_missing_settings_for_protected_routes_stay_unconfigured(self):
        with tempfile.TemporaryDirectory() as location, patch.dict(
            os.environ,
            {"APP_BASE_DIR": location, "ADMIN_API_KEY": "", "JOTFORM_WEBHOOK_TOKEN": ""},
        ):
            settings = load_settings()
            self.assertEqual(settings.admin_api_key, "")
            self.assertEqual(settings.jotform_webhook_token, "")

    def test_debug_error_file_does_not_include_customer_payload(self):
        with tempfile.TemporaryDirectory() as location:
            # Importing app.py initializes a default FastAPI app. Keep any
            # incidental logs and SQLite files inside a private temp folder.
            from checkin_bot.app import _write_webhook_error
            payload = {"passport_secret": "PII-EXAMPLE-DO-NOT-LOG", "full_name": "Secret Name"}
            error_id = _write_webhook_error(Path(location), payload, ValueError("Sensitive value"))
            dump = (Path(location) / "data" / "webhook-errors" / (error_id + ".json")).read_text("utf-8")
            self.assertNotIn("PII-EXAMPLE", dump)
            self.assertNotIn("Secret Name", dump)
            self.assertNotIn("Sensitive value", dump)
            self.assertIn("ValueError", dump)

    def test_aggregate_usage_metrics_never_prints_booking_identifiers(self):
        with tempfile.TemporaryDirectory() as location, patch.dict(
            os.environ,
            {"APP_BASE_DIR": location, "DATABASE_URL": "sqlite:///data/metrics.db"},
        ):
            db = Database(load_settings())
            db.init()
            db.upsert_booking(
                external_booking_id="sensitive-demo-identifier",
                jotform_submission_id=None,
                lead_guest_name="Private Synthetic Guest",
                guest_count=1,
                status="pending",
                source_payload={},
            )
            counts = summary(db.path)
            self.assertEqual(counts["bookings_total"], 1)
            self.assertEqual(counts["bookings_last_30_days"], 1)
            self.assertEqual(counts["guest_rows_total"], 0)


class OCRDownloadTests(unittest.TestCase):
    def test_data_url_normalization_is_offline(self):
        with tempfile.TemporaryDirectory() as location, patch.dict(os.environ, {"APP_BASE_DIR": location}):
            ocr = OCRProvider(load_settings())
            image = Image.new("RGB", (5, 5), "white")
            data = io.BytesIO()
            image.save(data, "PNG")
            payload = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode("ascii")
            normalized = asyncio.run(ocr._build_image_base64(payload))
            self.assertTrue(base64.b64decode(normalized).startswith(b"\xff\xd8\xff"))

    def test_private_network_image_url_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as location, patch.dict(os.environ, {"APP_BASE_DIR": location}):
            ocr = OCRProvider(load_settings())
            with self.assertRaises(ValueError):
                asyncio.run(ocr._build_image_base64("https://127.0.0.1/admin"))
            telegram = TelegramClient(load_settings())
            with self.assertRaises(ValueError):
                asyncio.run(telegram._load_image_payload(None, "https://127.0.0.1/admin", "passport"))

    def test_oversized_data_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as location, patch.dict(
            os.environ, {"APP_BASE_DIR": location, "OCR_MAX_DOWNLOAD_MB": "1"}
        ):
            ocr = OCRProvider(load_settings())
            with self.assertRaises(ValueError):
                asyncio.run(ocr._build_image_base64("data:image/png;base64," + "A" * 2_000_000))

    def test_redirect_to_non_allowlisted_host_is_blocked(self):
        with tempfile.TemporaryDirectory() as location, patch.dict(os.environ, {"APP_BASE_DIR": location}):
            ocr = OCRProvider(load_settings())
            recorded_urls = []

            def handler(request):
                recorded_urls.append(str(request.url))
                return httpx.Response(302, headers={"location": "https://127.0.0.1/private-data"})

            original_client = httpx.AsyncClient
            mock_transport = httpx.MockTransport(handler)

            def make_client(*args, **kwargs):
                return original_client(*args, transport=mock_transport, **kwargs)

            with patch("checkin_bot.integrations.httpx.AsyncClient", side_effect=make_client):
                with self.assertRaises(ValueError):
                    asyncio.run(ocr._build_image_base64("https://www.jotform.com/uploads/synthetic"))
            self.assertEqual(recorded_urls, ["https://www.jotform.com/uploads/synthetic"])


if __name__ == "__main__":
    unittest.main()
