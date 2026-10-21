# Guest Check-in Automation

[![Python CI](https://github.com/ass3mble/guest-checkin-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/ass3mble/guest-checkin-automation/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Privacy-conscious backend automation for digital guest check-in workflows.
It receives form submissions, extracts identity-document data with OCR, routes
reviews to Telegram, and can synchronize approved stays with property-management
and smart-lock services.

## What it does

- Receives Jotform webhook submissions.
- Stores booking and guest records in a local SQLite database.
- Extracts passport and visa data through configurable OCR providers.
- Sends review cards to Telegram for approval or rejection.
- Updates Beds24 after an approval decision.
- Optionally mirrors detailed status to Google Sheets.
- Optionally generates time-limited TTLock door codes.

## Workflow

```text
Jotform → FastAPI webhook → SQLite → OCR → Telegram review
                                             ↓
                              Beds24 / TTLock / Google Sheets
```

## Requirements

- Python 3.11+
- A Telegram bot and review chat
- Credentials for the integrations you enable
- Optional: Azure Document Intelligence, OpenAI, PaddleOCR, Beds24, TTLock,
  or Google Apps Script

## Quick start

These steps start a **local development instance**, not a public production service.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-checkin-bot.txt
Copy-Item .env.example .env
```

Open `.env` and add only the credentials and URLs for your own services. The
real `.env` file is intentionally excluded from Git by `.gitignore`.
**Before enabling real webhooks**, configure `ADMIN_API_KEY`,
`JOTFORM_WEBHOOK_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` and `TELEGRAM_CHAT_ID`.
Booking/admin endpoints and webhooks now **fail closed** when their security
secrets are unset. For an existing deployment, read
[the v0.2 migration instructions](docs/SECURE_DEPLOYMENT.md) **before upgrading**.

Start the API:

```powershell
uvicorn checkin_bot.app:create_app --factory --host 127.0.0.1 --port 8000
```

Or use `run-bot.bat` on Windows. The local database is created under `data/`
when the application starts.

### Run tests

After installing `requirements-checkin-bot.txt`, run:

```powershell
python -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, synthetic guests, and API smoke checks. They do not
call live booking, OCR, messaging, or smart-lock services. The same suite runs in
[GitHub Actions](https://github.com/ass3mble/guest-checkin-automation/actions/workflows/ci.yml)
on Python 3.11 and 3.12.

## OCR providers

Set the provider order with:

```env
OCR_PROVIDER_ORDER=azure,paddle,openai
```

The service tries providers in the configured order. Azure and OpenAI require
API credentials; PaddleOCR runs locally through `tools/paddle_ocr_worker.py`.

## API endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Health check |
| POST | `/webhooks/jotform` | Receive a form submission |
| POST | `/webhooks/telegram` | Receive Telegram updates |
| GET | `/bookings/{booking_id}` | Read a booking |
| POST | `/bookings/{booking_id}/process-ocr` | Process stored documents |
| POST | `/bookings/{booking_id}/review/send` | Send a Telegram review |
| POST | `/bookings/{booking_id}/sheet/sync` | Sync to Google Sheets |
| POST | `/bookings/{booking_id}/beds24/refresh` | Refresh Beds24 data |
| POST | `/beds24/retry-unmatched` | Retry unmatched bookings |

All `/bookings/*` and `/beds24/retry-unmatched` routes require
`Authorization: Bearer <ADMIN_API_KEY>` or `X-API-Key: <ADMIN_API_KEY>`.
`/webhooks/jotform` requires `X-Checkin-Webhook-Token` (or `?token=` when a
custom header is unavailable). `/webhooks/telegram` requires Telegram's
`X-Telegram-Bot-Api-Secret-Token` and a matching `TELEGRAM_CHAT_ID`.
Unauthenticated `/health` and `/healthz` remain available.

## Telegram commands

- `/link <beds24-booking-id>` creates a configured check-in link.
- `/note-link <beds24-booking-id>` creates the link and saves it to Beds24.

## Project structure

```text
checkin_bot/                  Application and integration code
tools/paddle_ocr_worker.py    Isolated PaddleOCR worker
tools/google_sheet_webhook.gs Google Apps Script mirror
data/                         Runtime data (ignored by Git)
.env.example                  Safe configuration template
```

## Security and privacy

- **Do not expose the API directly to the internet.** This release includes required
  shared-secret checks for booking/admin APIs and both webhook endpoints, but it has
  not had an independent security audit. Use a trusted TLS proxy, rate limiting,
  network controls, and least-privilege account access.
- Enable `ADMIN_API_KEY`, `JOTFORM_WEBHOOK_TOKEN`,
  `TELEGRAM_WEBHOOK_SECRET`, and `TELEGRAM_CHAT_ID` before production migration.
- OCR document URLs must use HTTPS and an explicitly approved hostname
  (`OCR_ALLOWED_DOWNLOAD_HOSTS`); redirect destinations are checked again.
- Never commit `.env`, databases, logs, uploaded documents, or webhook payloads.
- Keep API keys, bot tokens, passwords, and webhook secrets in environment
  variables or a secrets manager.
- Use HTTPS and authenticated webhook endpoints in production.
- Restrict the Telegram review chat to authorized staff.
- Rotate credentials immediately if they are ever exposed.

See [SECURITY.md](SECURITY.md) for known limitations and vulnerability reporting.
Do not use real guest data in public bug reports or tests.

## Project maintenance

- [Contributing guidelines](CONTRIBUTING.md)
- [Changelog and release scope](CHANGELOG.md)
- [Security policy and limitations](SECURITY.md)
- [Secure deployment and migration](docs/SECURE_DEPLOYMENT.md)
- [Maintainer usage-evidence guide](docs/MAINTAINER_EVIDENCE.md)

The public repository is an early snapshot of a system under active development.
Automated tests do not cover live third-party integrations or attest that deployment is
secure. Contributions and reproducible bug reports with fake data are welcome.

## License

This project is released under the [MIT License](LICENSE).
Third-party libraries and services retain their own licenses and terms.
