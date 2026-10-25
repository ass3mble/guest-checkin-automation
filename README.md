# Guest Check-in Automation

[![Python CI](https://github.com/ass3mble/guest-checkin-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/ass3mble/guest-checkin-automation/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A privacy-conscious Python service for collecting guest details, reviewing identity documents, and coordinating check-in tasks.**

Guest check-in often means copying information between forms, messages, booking systems, and door-lock services. This project brings those steps into one review workflow. Staff stay in control of approval decisions, while the service handles the repetitive work around them.

It is a real project under active development, shared here as an early public source release. It is not a finished, independently audited product.

## What it can help with

| Step | How it works |
| --- | --- |
| Collect guest details | Receive Jotform submissions through a protected webhook. |
| Record a booking | Store guests, booking references, and processing status in SQLite. |
| Read documents | Extract passport or visa fields with a configurable OCR provider. |
| Review information | Send a Telegram review message so staff can approve or reject. |
| Update reservations | Send approved changes to Beds24 when configured. |
| Share progress | Optionally mirror status to Google Sheets. |
| Prepare access | Optionally generate time-limited TTLock door codes. |

The integrations are optional and require their own credentials. A feature listed here does not mean it is enabled in every deployment.

## How the workflow fits together

```text
Guest submits Jotform
        |
        v
Protected webhook -> SQLite -> OCR document extraction
                                  |
                                  v
                          Telegram staff review
                                  |
                      Approved or rejected
                                  |
                     Optional integrations
                         /      |      \
                     Beds24  TTLock  Google Sheets
```

**Why keep a review step?** Identity documents can be blurry, incomplete, or incorrectly read by OCR. A staff member should check the extracted information before it is used in a booking or access workflow.

## Main integrations

- **Jotform:** Receives submitted check-in information.
- **Telegram:** Delivers review requests and receives staff decisions.
- **Beds24:** Connects approved guest information with booking records.
- **TTLock:** Can issue temporary access codes when enabled.
- **Google Sheets:** Provides an optional operational status view.
- **Azure Document Intelligence, OpenAI, PaddleOCR:** Configurable document-reading options. PaddleOCR can run locally.

## Requirements

- Python 3.11 or newer.
- A Telegram bot and a private review chat.
- Credentials for whichever external services you choose to enable.
- Permission to process the guest documents and booking data involved.

## Run locally

These commands start a **local development instance**, not a production-ready internet service.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-checkin-bot.txt
Copy-Item .env.example .env
```

Edit `.env` with your own credentials and endpoints. Never commit the real file.

For protected booking operations and webhooks, configure at least:

```text
ADMIN_API_KEY
JOTFORM_WEBHOOK_TOKEN
TELEGRAM_WEBHOOK_SECRET
TELEGRAM_CHAT_ID
```

Without these values, protected endpoints are designed to refuse requests. Existing deployments should read [the v0.2 migration guide](docs/SECURE_DEPLOYMENT.md) before upgrading.

Start the service:

```powershell
uvicorn checkin_bot.app:create_app --factory --host 127.0.0.1 --port 8000
```

On Windows, `run-bot.bat` is another local launch option. The service creates its runtime SQLite database under `data/`.

## Document-reading options

Set the provider order in `.env`:

```env
OCR_PROVIDER_ORDER=azure,paddle,openai
```

The service tries providers in the order you configure. Azure and OpenAI require credentials. PaddleOCR uses the local worker in `tools/paddle_ocr_worker.py`.

For remote document downloads, allow only trusted HTTPS hosts using `OCR_ALLOWED_DOWNLOAD_HOSTS`. Redirect destinations are checked too.

## API routes

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Basic health check |
| POST | `/webhooks/jotform` | Receive check-in submissions |
| POST | `/webhooks/telegram` | Process Telegram updates |
| GET | `/bookings/{booking_id}` | Read a booking |
| POST | `/bookings/{booking_id}/process-ocr` | Read stored documents |
| POST | `/bookings/{booking_id}/review/send` | Send a staff review request |
| POST | `/bookings/{booking_id}/sheet/sync` | Update the optional status sheet |
| POST | `/bookings/{booking_id}/beds24/refresh` | Refresh reservation information |
| POST | `/beds24/retry-unmatched` | Retry unmatched bookings |

Booking routes and the retry route require `Authorization: Bearer <ADMIN_API_KEY>` or `X-API-Key: <ADMIN_API_KEY>`.

The Jotform webhook checks `X-Checkin-Webhook-Token` (or the configured `?token=` fallback). The Telegram webhook checks its secret token and the expected `TELEGRAM_CHAT_ID`. The `/health` and `/healthz` routes do not require authentication.

Telegram commands include `/link <beds24-booking-id>` and `/note-link <beds24-booking-id>` for creating and optionally recording configured check-in links.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use temporary databases and synthetic guest information. GitHub Actions also runs them on Python 3.11 and 3.12. These checks do **not** contact live booking, document-processing, messaging, or door-lock services.

## Security matters

Guest identity documents are sensitive. If you deploy this service:

1. Use HTTPS through a trusted reverse proxy. Do not put the development server directly on the public internet.
2. Configure webhook secrets, API authentication, rate limiting, and network access controls.
3. Restrict Telegram review access to authorized staff.
4. Keep credentials in environment variables or a secret manager.
5. Never publish guest documents, database files, logs, or webhook payloads.
6. Rotate compromised credentials immediately.

This repository has not had an independent security assessment. Read [SECURITY.md](SECURITY.md) and [secure deployment guidance](docs/SECURE_DEPLOYMENT.md) before using real guest data.

## Project files and contribution

- `checkin_bot/`: API, workflow, database, and integrations.
- `tools/paddle_ocr_worker.py`: Isolated local OCR worker.
- `tools/google_sheet_webhook.gs`: Optional spreadsheet integration.
- `.env.example`: Configuration template with no real secrets.
- `tests/`: Unit tests and API checks.

For changes and contributions, see [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the [maintainer guide](docs/MAINTAINER_EVIDENCE.md). Use fictional guest data in issues and examples.

## License

Original project code is available under the [MIT License](LICENSE). The external services and libraries mentioned here have their own licenses and terms.
