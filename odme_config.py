from __future__ import annotations

APP_NAME = "ODME Angel — Options Decision & Monitoring Engine"

# Angel instrument master
ANGEL_INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Supported instruments. token/exchange_token values for spot/index are intentionally optional.
# For dashboard spot reference, app tries to infer from option chain/futures and falls back gracefully.
NSE_INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
MCX_SYMBOLS = [
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
    "GOLD", "GOLDM", "SILVER", "SILVERM", "SILVERMIC", "COPPER", "ZINC"
]
SUPPORTED_INSTRUMENTS = NSE_INDEX_SYMBOLS + MCX_SYMBOLS

# Relevant range settings around spot/future. Prevents useless far OTM max OI strikes.
RELEVANT_RANGE_PCT = {
    "NIFTY": 0.08,
    "BANKNIFTY": 0.09,
    "FINNIFTY": 0.09,
    "MIDCPNIFTY": 0.10,
    "CRUDEOIL": 0.12,
    "CRUDEOILM": 0.12,
    "NATURALGAS": 0.18,
    "NATGASMINI": 0.18,
    "GOLD": 0.08,
    "GOLDM": 0.08,
    "SILVER": 0.10,
    "SILVERM": 0.10,
    "SILVERMIC": 0.10,
    "COPPER": 0.10,
    "ZINC": 0.10,
}

DEFAULT_RELEVANT_RANGE_PCT = 0.10

# Angel MCX strike scaling is not uniform in appearance.
# Some lower-priced MCX commodities are stored as 100x in the master even when
# the raw strike is below the old generic 100000 threshold.
# Example: NATURALGAS 30000 in master means 300.00 strike.
# SILVER-family options may be stored as 100x in Angel master while futures LTP is already displayed.
MCX_STRIKE_SCALE_DIVISOR = {
    "NATURALGAS": 100.0,
    "NATGASMINI": 100.0,
    "COPPER": 100.0,
    "ZINC": 100.0,
    "CRUDEOIL": 100.0,
    "CRUDEOILM": 100.0,
    "GOLD": 100.0,
    "GOLDM": 100.0,
    # Angel MCX master currently stores SILVER-family option strikes as 100x.
    # Example: 27600000 in master = 276000 displayed strike.
    # Futures LTP is already returned in displayed price units, so option strikes
    # must be divided before futures-range validation and ODME analysis.
    "SILVER": 100.0,
    "SILVERM": 100.0,
    "SILVERMIC": 100.0,
}


# Hourly refresh while active. Streamlit only runs while the app is open.
REFRESH_INTERVAL_SECONDS = 3600

LOCAL_DATA_DIR = "data"
LOCAL_CONFIG_PATH = "config/angel_credentials.json"
GOOGLE_SHEET_DEFAULT_NAME = "ODME_Angel_Memory"

SHEET_TABS = {
    "initialized": "initialized_expiries",
    "snapshots": "snapshots",
    "chain": "chain_rows",
}

INITIALIZED_COLUMNS = [
    "key", "instrument", "exchange", "expiry", "initialized_at", "last_fetch_at",
    "option_count", "usable_oi_count", "status", "notes"
]

SNAPSHOT_COLUMNS = [
    "snapshot_id", "key", "ts", "instrument", "exchange", "expiry", "spot", "future",
    "source", "chain_rows", "usable_oi_count", "notes"
]

CHAIN_COLUMNS = [
    "snapshot_id", "key", "ts", "instrument", "exchange", "expiry", "symbol", "token",
    "option_type", "strike", "ltp", "oi", "volume", "open", "high", "low", "close",
    "bid", "ask", "feed_time", "trade_time"
]
