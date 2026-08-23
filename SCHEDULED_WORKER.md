# ODME unattended scheduled worker

`scheduled_worker.py` is the non-Streamlit runner. It is intended to be invoked by an external scheduler (recommended: one Google Cloud Scheduler trigger every 5 minutes calling a Cloud Run Job).

At each invocation it:

1. Reads persistent instrument settings from the ODME Google Sheet.
2. Finds manually selected instrument/expiry pairs whose saved IST scan time is due.
3. Logs into Angel automatically using `ANGEL_TOTP_SECRET`.
4. Runs the same `analyze_odme()` engine as the dashboard.
5. Saves a compact snapshot only when the anchor-independent market state actually changed.
6. Sends one consolidated Gmail summary for all due instruments with email alerts enabled.
7. Marks that scan slot complete to prevent duplicate alerts.

There is no weekday or market-hours restriction. Scan slots cover 00:00–23:55 IST in five-minute increments.

## Closed-market / weekend / holiday rule

If Angel returns the same market state as the latest saved snapshot, the worker does not save another snapshot. Therefore an unchanged Saturday, Sunday, overnight period, or public holiday does not replace the prior trading-session anchor. Monday will still compare against the latest genuinely changed prior-session snapshot.

## Environment variables for the worker

The worker can use environment variables instead of Streamlit Secrets:

- `ANGEL_API_KEY`
- `ANGEL_CLIENT_ID`
- `ANGEL_PIN`
- `ANGEL_TOTP_SECRET`
- `USE_GOOGLE_SHEETS=true`
- `GOOGLE_SHEET_NAME`
- `GCP_SERVICE_ACCOUNT_JSON` (the complete service-account JSON as one environment secret)
- `GMAIL_SENDER`
- `GMAIL_APP_PASSWORD`
- `ALERT_EMAILS` (JSON list or comma-separated addresses)

Do not commit real secrets to GitHub.
