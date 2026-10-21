"""Offline configuration readiness check; never prints secrets."""

from __future__ import annotations

import sys
from pathlib import Path

# Supports running as `python tools/check_security_config.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checkin_bot.config import load_settings  # noqa: E402


def main() -> int:
    try:
        settings = load_settings()
    except (ValueError, OSError) as exc:
        print(f"CONFIG ERROR: {type(exc).__name__}; check local .env syntax.")
        return 2

    required = {
        "ADMIN_API_KEY": settings.admin_api_key,
        "JOTFORM_WEBHOOK_TOKEN": settings.jotform_webhook_token,
        "TELEGRAM_WEBHOOK_SECRET": settings.telegram_webhook_secret,
        "TELEGRAM_CHAT_ID": settings.telegram_chat_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        for name in missing:
            print(f"MISSING: {name}")
        print("FAIL: Configure the above values in the private environment before migration.")
        return 1

    if not settings.ocr_allowed_download_hosts:
        print("MISSING: OCR_ALLOWED_DOWNLOAD_HOSTS")
        return 1

    print("PASS: Required access-control configuration is present.")
    print("NOTE: This does not verify credential strength, third-party access, or deployment security.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
