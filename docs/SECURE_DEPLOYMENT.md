# Secure deployment and v0.2 migration

**Read before upgrading any working installation.** The public repository is only a
sanitized snapshot. Changes pushed to GitHub do **not** modify the original
installation or its `.env`. Do not migrate a live service without backups, its
operator's credentials, and a rollback plan.

## 1. Prepare without stopping the existing deployment

1. Export a private backup of the current application code, `.env`, and SQLite
   database. Keep copies outside the public Git repository.
2. Record the version/commit currently deployed and how webhooks reach it.
3. Generate independent long random values for `ADMIN_API_KEY` and
   `JOTFORM_WEBHOOK_TOKEN` (for example with Python
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
4. Add the new variables to the private `.env`:

```env
ADMIN_API_KEY=<GENERATE_AN_INDEPENDENT_SECRET>
JOTFORM_WEBHOOK_TOKEN=<GENERATE_ANOTHER_SECRET>
TELEGRAM_WEBHOOK_SECRET=<KEEP_OR_CONFIGURE_THE_EXISTING_TELEGRAM_SECRET>
TELEGRAM_CHAT_ID=<AUTHORIZED_CHAT_ID>
OCR_ALLOWED_DOWNLOAD_HOSTS=jotform.com,jotformusercontent.com,jotformpro.com
OCR_MAX_DOWNLOAD_MB=10
```

Use placeholders only in examples. **Never put the actual values in a public
commit, support ticket, browser screenshot, or GitHub Actions log.**

5. Review real document URLs already in your private data to identify **hostnames
   only**. Add additional trusted HTTPS providers to
   `OCR_ALLOWED_DOWNLOAD_HOSTS` when needed; IP addresses and insecure HTTP
   URLs are intentionally rejected.
6. Set the Telegram webhook through the official Telegram API with a matching
   `secret_token` and verify that `TELEGRAM_CHAT_ID` matches the review chat.

## 2. Configure Jotform secret validation

The service accepts `JOTFORM_WEBHOOK_TOKEN` in the
`X-Checkin-Webhook-Token` header. Prefer a trusted HTTPS proxy that adds this
header after authenticating the sender. When custom headers are unavailable,
the webhook URL can use `https://your-host/webhooks/jotform?token=<secret>`.
**This query-string method is less secure:** URLs may appear in proxy/server
access logs, monitoring dashboards, and browser histories. Suppress or redact
query strings in the entire ingress pipeline.

This is a shared-secret check, not a signed Jotform payload verification.
Protect the endpoint with provider/network restrictions, rate limits,
request-size limits, and TLS termination. Never trust arbitrary public sender
requests based only on an unverified `X-Forwarded-For` header.

## 3. Configure admin access

All booking-read and action endpoints now need an admin key:

```bash
curl -H "Authorization: Bearer <ADMIN_API_KEY>" \
  https://your-host/bookings/<SYNTHETIC_OR_KNOWN_BOOKING_ID>
```

Or provide `X-API-Key: <ADMIN_API_KEY>`. Neither secret goes in a URL.
Update local scripts/API consumers before changing the running server.

If a required secret is **unset**, the protected route returns HTTP **503**.
If the supplied secret is missing or invalid, it returns **401**.
Telegram calls from unauthorized chats return **403**. `/health` and
`/healthz` stay public to support health checks.

## 4. Validate in staging, then deploy deliberately

- Run `python -m unittest discover -s tests -v` with no production credentials.
- Run `python tools/check_security_config.py` **locally** with the intended
  private environment; it prints missing variable names, not secret values.
- Test good/bad Jotform webhook tokens and Telegram webhook secret/chat IDs
  against a staging instance without sending real guest documents.
- Verify your actual permitted document-hostnames and redirect behavior with
  **synthetic** image URLs.
- Confirm API consumers send admin credentials and webhook processing still
  produces the expected review and booking states.
- Test health endpoints and a controlled rollback before traffic cutover.
- Rate-limit endpoints, restrict egress, secure logs/files/backups, and enable
  full lifecycle deletion policies appropriate to local data protection laws.

## Rollback

Restore the previous tested code and matching private configuration. Do not
blindly roll back an application if its database schema has changed. This
release does not include a database migration. Restore the database backup
only if necessary and after preserving more recent legitimate guest check-ins.

## Explicit limits

The shared-secret model is **not** staff identity/role authorization. Telegram
chat membership does not limit which users in that chat may issue commands.
Potential leakage via external libraries/integration logs, DNS-rebinding risks
on custom permitted OCR domains, data retention, and end-to-end third-party
integration security require additional engineering and a production audit.
