# ODME Angel — Options Decision & Monitoring Engine

Fresh Streamlit app using **Angel One SmartAPI only**. There is no NSE scraping in this app.

## What this app does

- Login screen asks only for the current Angel TOTP.
- API key, client ID and PIN are stored locally in `config/angel_credentials.json`.
- Loads Angel instrument master.
- Supports NSE index options and MCX options:
  - `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`
  - `CRUDEOIL`, `CRUDEOILM`, `NATURALGAS`, `NATGASMINI`, `GOLD`, `GOLDM`, `SILVER`, `SILVERM`, `COPPER`, `ZINC`
- User selects expiry from Angel master.
- User clicks **Initialize Expiry** once for a new instrument + expiry.
- The app saves full option-chain snapshots locally by instrument + expiry.
- On future launches, it appends fresh snapshots and analyzes cumulative memory, not only the latest chain.
- MCX active-expiry validation is included: if the selected expiry has no usable OI, the app tells the user to select another expiry.

## Files

```text
app.py
angel_connector.py
data_store.py
odme_engine.py
odme_config.py
requirements.txt
README.md
config/angel_credentials_TEMPLATE.json
```

Runtime-created local files:

```text
config/angel_credentials.json          # you create this locally; do not share it
data/cache/angel_instruments.parquet   # instrument master cache
data/initialized_expiries.json         # initialized expiry registry
data/memory/*.parquet                  # option-chain memory snapshots
```

## Setup

1. Create and activate a Python environment.

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # macOS/Linux
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Create credentials file.

Copy:

```text
config/angel_credentials_TEMPLATE.json
```

Rename the copy to:

```text
config/angel_credentials.json
```

Fill it like this:

```json
{
  "api_key": "YOUR_ANGEL_API_KEY",
  "client_id": "YOUR_ANGEL_CLIENT_ID",
  "pin": "YOUR_ANGEL_PIN"
}
```

Do not enter or share these credentials in chat.

4. Run the app.

```bash
streamlit run app.py
```

## Daily workflow

1. Open the app.
2. Enter only current Angel TOTP on Page 1.
3. Select instrument and expiry.
4. If not initialized, click **Initialize Expiry**.
5. Thereafter, the app uses local memory and appends fresh Angel snapshots roughly hourly while active.
6. If Streamlit is closed, nothing runs in the background. On next launch, the app fetches the latest snapshot and appends it.
7. Memory resets only when you initialize/reset a new expiry.

## ODME logic included

The engine uses cumulative memory and evaluates:

- Tradable Option POC using combined CE + PE OI inside relevant range around spot/future.
- Raw full-chain max OI is shown only as background, not used for strike decision.
- HVN / friction and LVN / vacuum strikes.
- CE wall and PE wall using a weighted score:
  - current OI
  - cumulative OI buildup from initialization
  - volume
  - persistence across snapshots
  - proximity to spot
- Wall migration:
  - CE wall higher/lower/stable
  - PE wall higher/lower/stable
  - POC higher/lower/stable
  - range narrowing/widening/stable
- Matrix logic on key strikes:
  - OI up + premium up = fresh buying / stress
  - OI up + premium down = writing / control
  - OI down + premium up = writer covering / failure risk
  - OI down + premium down = long liquidation / interest fading

Dashboard decision outputs:

- `BULLISH POSITIONING`
- `BEARISH POSITIONING`
- `RANGE-BOUND THETA`
- `EXPANSION / TRAP RISK`
- `MIXED / NO CLEAN EDGE`

## Important practical notes

- The app estimates spot/future from option-chain parity if direct underlying LTP is unavailable. This is acceptable for ODME positioning, but for live execution you may later add a direct underlying token fetch.
- Angel option-chain availability depends on Angel market data permission and active contracts.
- Some MCX expiries may legitimately show zero OI; use another active expiry in that case.
- Keep `config/angel_credentials.json` private.
