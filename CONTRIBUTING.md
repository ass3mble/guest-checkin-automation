# Contributing

Thanks for helping maintain Guest Check-in Automation.

## What to work on

- Improve correctness and test coverage of booking intake, review and integration logic.
- Add documentation and safe examples for different property-management workflows.
- Improve privacy, authentication, observability and error recovery.
- Fix reproducible defects with minimal changes to live workflow behavior.

Before writing a substantial feature, please open an issue describing the proposed workflow
and how it will be tested. Do not submit real guest records or credentials.

## Local development

Use Python 3.11 or newer:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-checkin-bot.txt
python -m unittest discover -s tests -v
```

All tests must use synthetic booking data, temporary SQLite databases, and fake service
responses. Never connect tests to production Telegram, Beds24, TTLock, OCR, or Google Sheets
accounts. Integration tests that require secrets should not run on public pull requests.

## Pull requests

1. Fork this repository and create a topic branch.
2. Keep changes focused, document any behavior or configuration changes, and add regression
   tests where reasonable.
3. Verify the offline test suite locally and ensure CI is green.
4. Do not include any guest PII, document images, real booking IDs, passwords, API keys,
   tokens, production URLs with embedded credentials, database files, or diagnostic payloads.
5. Explain any backwards-compatibility or privacy implications in the PR description.

By contributing, you agree your contributions may be distributed under the project's MIT
License. You must have the right to submit all code, documentation, and assets you contribute.

For a vulnerability, **do not open a public issue**. See [SECURITY.md](SECURITY.md).
