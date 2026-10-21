"""Schema and configuration tests that do not contact external services."""

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from checkin_bot.config import _bool_env, _int_tuple_env, parse_json_env
from checkin_bot.schemas import BookingInput, GuestInput


class SchemaTests(unittest.TestCase):
    def test_valid_synthetic_booking(self):
        booking = BookingInput.model_validate(
            {
                "external_booking_id": "synthetic-001",
                "guest_count": 1,
                "guests": [{"guest_index": 1, "full_name": "Sample Guest"}],
            }
        )
        self.assertEqual(booking.guests[0].full_name, "Sample Guest")

    def test_guest_index_and_guest_count_bounds(self):
        with self.assertRaises(ValidationError):
            GuestInput(guest_index=0)
        with self.assertRaises(ValidationError):
            GuestInput(guest_index=5)
        with self.assertRaises(ValidationError):
            BookingInput(external_booking_id="synthetic", guest_count=0, guests=[])
        with self.assertRaises(ValidationError):
            BookingInput(external_booking_id="synthetic", guest_count=5, guests=[])

    def test_boolean_environment_parser(self):
        with patch.dict(os.environ, {"SYNTHETIC_BOOL": "YES"}):
            self.assertTrue(_bool_env("SYNTHETIC_BOOL", False))
        with patch.dict(os.environ, {"SYNTHETIC_BOOL": "off"}):
            self.assertFalse(_bool_env("SYNTHETIC_BOOL", True))

    def test_integer_list_and_json_environment_parser(self):
        with patch.dict(
            os.environ,
            {"SYNTHETIC_IDS": "12, 34", "SYNTHETIC_JSON": '{"demo": [1, 2]}'},
        ):
            self.assertEqual(_int_tuple_env("SYNTHETIC_IDS"), (12, 34))
            self.assertEqual(parse_json_env("SYNTHETIC_JSON"), {"demo": [1, 2]})


if __name__ == "__main__":
    unittest.main()
