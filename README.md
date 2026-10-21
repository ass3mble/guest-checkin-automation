# Guest Check-in Automation

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

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-checkin-bot.txt
Copy-Item .env.example .env
```

Open `.env` and add only the credentials and URLs for your own services. The
real `.env` file is intentionally excluded from Git by `.gitignore`.

Start the API:

```powershell
uvicorn checkin_bot.app:create_app --factory --host 127.0.0.1 --port 8000
```

Or use `run-bot.bat` on Windows. The local database is created under `data/`
when the application starts.

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

- Never commit `.env`, databases, logs, uploaded documents, or webhook payloads.
- Keep API keys, bot tokens, passwords, and webhook secrets in environment
  variables or a secrets manager.
- Use HTTPS and authenticated webhook endpoints in production.
- Restrict the Telegram review chat to authorized staff.
- Rotate credentials immediately if they are ever exposed.

## License

This project is released under the [MIT License](LICENSE).
Third-party libraries and services retain their own licenses and terms.
