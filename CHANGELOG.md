# Changelog

Changes to the publicly maintained open-source repository are recorded here.

## [0.2.0] - 2026-10-21

Security and maintenance pre-release:

- Fail-closed shared-secret checks for booking/admin API routes, Jotform, and Telegram.
- Telegram chat validation, privacy-safe Jotform error files, safer diagnostic logging.
- HTTPS-only OCR download host allowlisting, redirect/size/pixel limits.
- SQLite foreign key enforcement on every connection and allowlisted guest update fields.
- Expanded synthetic regression tests, secure deployment guide, and local-only
  aggregate usage reporting for maintainers.
- **Breaking configuration:** existing deployments must set
  `ADMIN_API_KEY`, `JOTFORM_WEBHOOK_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
  and `TELEGRAM_CHAT_ID` before migrating; consult `docs/SECURE_DEPLOYMENT.md`.

## [0.1.0] - 2026-10-21

First documented open-source snapshot:

- MIT license and explicit contribution/security guidance.
- Synthetic offline smoke and persistence tests; Python CI on GitHub Actions.
- Corrected fallback parsing of structured Jotform birth dates (day/month/year).
- README covering installation, public API surface, testing, and deployment cautions.

**Scope:** source and maintenance infrastructure only. This release is not a guarantee of
production readiness. Live Jotform/Telegram/Beds24/TTLock/OCR/Sheets integrations require
private configuration and have not been end-to-end validated in public CI.
