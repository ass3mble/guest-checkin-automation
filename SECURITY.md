# Security policy

Guest Check-in Automation processes sensitive personal information (including identity
documents). **The public repository is an early open-source snapshot, not a security-audited,
production-ready deployment template.** Use it only behind appropriate network and access
controls until the limitations below have been addressed.

## Known limitations

- From v0.2 onward booking-read and processing endpoints require `ADMIN_API_KEY`.
  These use a shared secret, not a full user/role access-control system. Do not expose them
  without HTTPS, reverse-proxy controls, monitoring, and rate limiting.
- Jotform requires `JOTFORM_WEBHOOK_TOKEN`. A header is preferred; URL-query secrets
  are more easily leaked via access logs/browser history and must be redacted.
  This is not cryptographic payload signing; implement sender verification at the gateway
  where possible.
- Telegram requires `TELEGRAM_WEBHOOK_SECRET` and matches `TELEGRAM_CHAT_ID`.
  Chat membership alone does not restrict who may issue commands inside that chat.
- Document downloads use a hostname allowlist, HTTPS-only validation, redirect
  re-validation, and size limits. DNS rebinding and infrastructure-level controls
  still require gateway/égress restrictions, particularly with custom allowed hosts.
- Runtime SQLite records, OCR results, logs, and diagnostic webhook payloads can contain
  personally identifiable information. Protect the filesystem, use strict retention and
  deletion rules, and encrypt/limit access where required by law and your threat model.
- The repository's offline tests do not validate access controls, cryptography, external
  service permissions, or overall production security. New unit tests check the
  shared-secret and URL allowlist logic, not the security of live deployments.

## Responsible reporting

If you find a vulnerability, please use the repository's GitHub **Security → Report a
vulnerability** feature (private vulnerability reporting) if it is enabled. Otherwise,
contact the maintainer privately through a non-public channel before sharing details.
Do not post exploit steps, personal records, or credentials in public issues or pull requests.

Please include: affected component, potential impact, minimum steps using synthetic data,
and any suggested mitigation. Do not access or test accounts or data you do not own.

## Handling sensitive data

- Do not commit `.env`, local databases, logs, document uploads, webhook samples from real
  users, credential caches, or exported reports.
- Use synthetic data in tests and documentation.
- Rotate exposed credentials immediately, revoke compromised access, and review Git history.
- Terminate TLS at a trusted proxy and require authentication for protected routes.
- Existing installations MUST configure new secrets before upgrading to v0.2 or
  webhook/booking requests will return 503; see [migration instructions](docs/SECURE_DEPLOYMENT.md).
