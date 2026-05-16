"""Configuration for ODME Angel."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

APP_NAME = "ODME Angel — Options Decision & Monitoring Engine"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
MEMORY_DIR = DATA_DIR / "memory"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
CREDENTIALS_PATH = CONFIG_DIR / "angel_credentials.json"
CREDENTIALS_TEMPLATE_PATH = CONFIG_DIR / "angel_credentials_TEMPLATE.json"
INSTRUMENT_CACHE_PATH = CACHE_DIR / "angel_instruments.parquet"
INIT_REGISTRY_PATH = DATA_DIR / "initialized_expiries.json"
REFRESH_MINUTES = 60

NSE_INDEX_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
MCX_UNDERLYINGS = [
    "CRUDEOIL",
    "CRUDEOILM",
    "NATURALGAS",
    "NATGASMINI",
    "GOLD",
    "GOLDM",
    "SILVER",
    "SILVERM",
    "COPPER",
    "ZINC",
]
ALL_UNDERLYINGS = NSE_INDEX_UNDERLYINGS + MCX_UNDERLYINGS

# Default relevant range width around spot. App auto-tightens from observed strike spacing.
RELEVANT_RANGE_PCT = {
    "NIFTY": 0.08,
    "BANKNIFTY": 0.08,
    "FINNIFTY": 0.08,
    "MIDCPNIFTY": 0.08,
    "CRUDEOIL": 0.12,
    "CRUDEOILM": 0.12,
    "NATURALGAS": 0.18,
    "NATGASMINI": 0.18,
    "GOLD": 0.08,
    "GOLDM": 0.08,
    "SILVER": 0.10,
    "SILVERM": 0.10,
    "COPPER": 0.12,
    "ZINC": 0.12,
}

# Used only when spot discovery from Angel index token is unavailable.
FALLBACK_SPOT_FROM_CHAIN = True


@dataclass(frozen=True)
class InstrumentSpec:
    exchange: str
    option_exchange: str
    instrument_type: str


def instrument_spec(symbol: str) -> InstrumentSpec:
    symbol = symbol.upper()
    if symbol in NSE_INDEX_UNDERLYINGS:
        return InstrumentSpec(exchange="NSE", option_exchange="NFO", instrument_type="OPTIDX")
    if symbol in MCX_UNDERLYINGS:
        return InstrumentSpec(exchange="MCX", option_exchange="MCX", instrument_type="OPTFUT")
    raise ValueError(f"Unsupported underlying: {symbol}")
