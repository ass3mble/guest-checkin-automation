# Security policy

Guest Check-in Automation processes sensitive personal information (including identity
documents). **The public repository is an early open-source snapshot, not a security-audited,
production-ready deployment template.** Use it only behind appropriate network and access
controls until the limitations below have been addressed.

## Known limitations

- Booking-read and administrative/processing endpoints do not currently enforce a complete
  application-level authentication and authorization scheme. Do **not** expose them publicly.
- The Jotform intake webhook does not yet enforce an end-to-end sender signature/secret check.
  Place it behind a trusted gateway and configure appropriate verification.
- Telegram webhook authentication is conditional on setting `TELEGRAM_WEBHOOK_SECRET`.
  Set a non-empty random secret and configure the same token in Telegram.
- Runtime SQLite records, OCR results, logs, and diagnostic webhook payloads can contain
  personally identifiable information. Protect the filesystem, use strict retention and
  deletion rules, and encrypt/limit access where required by law and your threat model.
- The repository's offline tests do not validate access controls, cryptography, external
  service permissions, or overall production security.

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
