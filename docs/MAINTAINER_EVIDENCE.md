# Maintainer evidence without publishing guest data

This project is maintained for real-world hospitality check-in workflows.
The public repository does **not** publish production booking records or
end-user adoption numbers. Maintainers should report only independently
verifiable or safely aggregated facts in grant applications.

## Evidence already publicly available

- Public code, MIT license, `CONTRIBUTING.md`, `SECURITY.md`.
- Public Git commit history and tagged pre-releases.
- GitHub Actions CI runs on Python 3.11 and Python 3.12.
- Offline tests using synthetic data, with public documentation of limitations.

## Optional local operational counts

Run on a **private trusted machine**, never in GitHub Actions:

```bash
python tools/usage_metrics.py --db data/checkin.db
```

The tool opens SQLite read-only and emits only aggregate totals
(bookings, number created in last 30 days, guest rows, and document rows).
No individual booking IDs, identity numbers, names, images or dates are printed.
Check that the data and aggregation can be disclosed under relevant contractual
or privacy requirements before using any numbers in an application.

## Good additional evidence

- An authentic screenshot of the functioning automation **with all sensitive
  customer data fully removed**.
- A reproducible synthetic demonstration, changelog, security remediation,
  and tests that pass in a fresh environment.
- Genuine externally reported issues, contributions, or downstream deployments.

Do not fabricate downloads, stars, customers, or commit activity.
