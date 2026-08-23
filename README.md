# ODME Angel — Options Decision & Monitoring Engine

Angel SmartAPI-only Streamlit app for option-chain decisioning and monitoring. No NSE scraping.

## What this app does

- Page 1 asks only for current Angel TOTP.
- API key, Client ID and PIN are read from Streamlit Secrets for URL deployment, or from local `config/angel_credentials.json`.
- Loads Angel instrument master.
- Supports NSE index options and MCX commodity options.
- User selects instrument and expiry.
- User initializes expiry once; app fetches full option chain and stores memory.
- Every analysis uses cumulative saved memory for that instrument+expiry.
- Supports Google Sheets as persistent memory for Streamlit URL deployment.

## Repository files

Commit these files:

```text
app.py
angel_connector.py
data_store.py
odme_engine.py
odme_config.py
requirements.txt
README.md
config/angel_credentials_TEMPLATE.json
.streamlit/secrets_TEMPLATE.toml
.gitignore
data/.gitkeep
```

Do not commit real credentials or local data.

## Streamlit Cloud Secrets

In Streamlit Cloud, go to:

```text
App -> Settings -> Secrets
```

Paste:

```toml
ANGEL_API_KEY = "your_angel_api_key"
ANGEL_CLIENT_ID = "your_angel_client_id"
ANGEL_PIN = "your_angel_pin"

USE_GOOGLE_SHEETS = true
GOOGLE_SHEET_NAME = "ODME_Angel_Memory"

[gcp_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "your-private-key-id"
private_key = "-----BEGIN PRIVATE KEY-----\nPASTE_KEY_WITH_N_LINES\n-----END PRIVATE KEY-----\n"
client_email = "your-service-account@your-project-id.iam.gserviceaccount.com"
client_id = "your-client-id"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/your-service-account%40your-project-id.iam.gserviceaccount.com"
universe_domain = "googleapis.com"
```

## Google Sheet setup

Create a Google Sheet named:

```text
ODME_Angel_Memory
```

Share it with the service account email, for example:

```text
your-service-account@your-project-id.iam.gserviceaccount.com
```

Give Editor access.

The app will create these tabs automatically if missing:

1. `initialized_expiries`
2. `snapshots`
3. `chain_rows`

You do not need to manually create columns; the app creates them.

## Local run

Create this file from the template:

```text
config/angel_credentials.json
```

Then run:

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Important note

Streamlit Cloud file storage is not permanent. Use Google Sheets for persistent ODME memory.

## Automatic Angel TOTP test

For unattended Angel login, add `ANGEL_TOTP_SECRET` as a top-level Streamlit secret (before `[gcp_service_account]`). The login page includes **Automatic Angel login test**, which generates the current 6-digit TOTP internally and verifies a real SmartAPI login without displaying the seed or OTP. Manual TOTP login remains available.

## Unattended scheduled scans

The project now includes `scheduled_worker.py`, `scan_service.py`, `runtime_config.py` and `Dockerfile.worker` for unattended ODME scans. The dashboard stores a manual expiry and one or more 24-hour IST scan times for each instrument. The worker runs those exact expiry selections, never auto-rolls them, sends one consolidated Gmail summary, and skips saving unchanged closed-market data so weekend/holiday reads do not replace the prior trading-session anchor. See `SCHEDULED_WORKER.md`.
