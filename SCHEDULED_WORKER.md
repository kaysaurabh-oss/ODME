# ODME unattended worker — GitHub Actions

ODME uses GitHub Actions only at the scan times saved in the Streamlit app. It does not poll every five minutes.

## Why

For a private GitHub repository, polling every five minutes would consume too many hosted-runner minutes. The app therefore synchronizes the union of enabled IST scan times into `.github/workflows/odme_scheduled.yml`.

## Streamlit secrets needed for schedule sync

- `GITHUB_REPO` = `owner/repository`
- `GITHUB_BRANCH` = normally `main`
- `GITHUB_SCHEDULE_TOKEN` = fine-grained PAT restricted to this repository with **Contents: Read and write** and **Workflows: Read and write**

## GitHub Actions repository secrets needed by the worker

- `ANGEL_API_KEY`
- `ANGEL_CLIENT_ID`
- `ANGEL_PIN`
- `ANGEL_TOTP_SECRET`
- `GMAIL_SENDER`
- `GMAIL_APP_PASSWORD`
- `ALERT_EMAILS` as a JSON array string, for example `["a@gmail.com","b@gmail.com"]`
- `GOOGLE_SHEET_NAME`
- `GCP_SERVICE_ACCOUNT_JSON` as the complete service-account JSON document

## Behaviour

- Runs every day, including weekends and public holidays, if that time is selected.
- Uses the exact manually saved expiry for each instrument.
- Does not auto-roll expiry.
- Expired history is deleted.
- Unchanged closed-market data is not saved, preserving the prior changed anchor.
- One consolidated email is sent for all instruments due in that scheduled run.
- GitHub schedule delays are tolerated by the worker; it uses the latest unprocessed slot in the recent grace window.
