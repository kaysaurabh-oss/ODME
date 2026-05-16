from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

from odme_config import ANGEL_INSTRUMENT_MASTER_URL, LOCAL_CONFIG_PATH, MCX_SYMBOLS, NSE_INDEX_SYMBOLS

try:
    from SmartApi import SmartConnect
except Exception:  # pragma: no cover
    SmartConnect = None


@dataclass
class AngelCredentials:
    api_key: str
    client_id: str
    pin: str


def _read_streamlit_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def load_angel_credentials() -> AngelCredentials:
    """Load credentials from Streamlit secrets first, then local config file.

    URL deployment: use Streamlit Cloud secrets.
    Local deployment: create config/angel_credentials.json from template.
    """
    api_key = _read_streamlit_secret("ANGEL_API_KEY")
    client_id = _read_streamlit_secret("ANGEL_CLIENT_ID")
    pin = _read_streamlit_secret("ANGEL_PIN")

    if api_key and client_id and pin:
        return AngelCredentials(str(api_key), str(client_id), str(pin))

    path = Path(LOCAL_CONFIG_PATH)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return AngelCredentials(
            api_key=str(data.get("api_key", "")).strip(),
            client_id=str(data.get("client_id", "")).strip(),
            pin=str(data.get("pin", "")).strip(),
        )

    raise RuntimeError(
        "Angel credentials not found. For Streamlit Cloud, add ANGEL_API_KEY, "
        "ANGEL_CLIENT_ID and ANGEL_PIN in Secrets. For local, create config/angel_credentials.json."
    )


class AngelConnector:
    def __init__(self, credentials: AngelCredentials):
        self.credentials = credentials
        self.obj: Optional[Any] = None
        self.jwt_token: Optional[str] = None
        self.feed_token: Optional[str] = None

    def login(self, totp: str) -> Dict[str, Any]:
        if SmartConnect is None:
            raise RuntimeError("SmartApi package not installed. Run: pip install -r requirements.txt")
        totp = str(totp).strip()
        if not totp:
            raise ValueError("Enter current Angel TOTP.")
        self.obj = SmartConnect(api_key=self.credentials.api_key)
        data = self.obj.generateSession(self.credentials.client_id, self.credentials.pin, totp)
        if not data or not data.get("status"):
            raise RuntimeError(f"Angel login failed: {data}")
        payload = data.get("data", {})
        self.jwt_token = payload.get("jwtToken")
        try:
            self.feed_token = self.obj.getfeedToken()
        except Exception:
            self.feed_token = payload.get("feedToken")
        return data

    @staticmethod
    @st.cache_data(ttl=6 * 3600, show_spinner=False)
    def load_instrument_master() -> pd.DataFrame:
        r = requests.get(ANGEL_INSTRUMENT_MASTER_URL, timeout=30)
        r.raise_for_status()
        raw = r.json()
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        for col in ["strike", "lotsize", "tick_size"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ["expiry", "exch_seg", "instrumenttype", "symbol", "name", "token"]:
            if col in df.columns:
                df[col] = df[col].astype(str)
        return df

    @staticmethod
    def option_exchange_for_instrument(instrument: str) -> str:
        return "MCX" if instrument in MCX_SYMBOLS else "NFO"

    @staticmethod
    def normalize_strike(row: pd.Series) -> float:
        strike = float(row.get("strike", 0) or 0)
        exch = str(row.get("exch_seg", ""))
        # Angel NFO index option strikes are generally 100x. MCX often can also be scaled.
        # Keep practical normalization: divide by 100 when strike is clearly too large.
        if strike >= 100000:
            return strike / 100.0
        if exch == "NFO" and strike >= 10000 * 100:
            return strike / 100.0
        return strike

    @classmethod
    def get_option_rows(cls, master: pd.DataFrame, instrument: str) -> pd.DataFrame:
        if master.empty:
            return master
        exch = cls.option_exchange_for_instrument(instrument)
        df = master.copy()
        # symbol/name matching differs between exchanges; use both.
        name_match = df.get("name", pd.Series(dtype=str)).astype(str).str.upper().eq(instrument.upper())
        symbol_match = df.get("symbol", pd.Series(dtype=str)).astype(str).str.upper().str.startswith(instrument.upper())
        exch_match = df.get("exch_seg", pd.Series(dtype=str)).astype(str).str.upper().eq(exch)
        opt_type_match = df.get("instrumenttype", pd.Series(dtype=str)).astype(str).str.upper().str.contains("OPT", na=False)
        df = df[exch_match & opt_type_match & (name_match | symbol_match)].copy()
        if df.empty:
            return df
        df["strike_norm"] = df.apply(cls.normalize_strike, axis=1)
        df["option_type"] = df["symbol"].str.upper().str.extract(r"(CE|PE)$", expand=False)
        df = df[df["option_type"].isin(["CE", "PE"])]
        return df.sort_values(["expiry", "strike_norm", "option_type", "symbol"])

    @classmethod
    def get_expiries(cls, master: pd.DataFrame, instrument: str) -> List[str]:
        rows = cls.get_option_rows(master, instrument)
        if rows.empty or "expiry" not in rows.columns:
            return []
        return sorted(rows["expiry"].dropna().astype(str).unique().tolist())

    @staticmethod
    def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def get_market_data_full(self, exchange: str, tokens: List[str]) -> Dict[str, Any]:
        if self.obj is None:
            raise RuntimeError("Not logged in to Angel.")
        all_fetched: List[Dict[str, Any]] = []
        all_unfetched: List[Dict[str, Any]] = []
        # Angel market data accepts a token map. Keep chunks small for reliability.
        for chunk in self._chunks([str(t) for t in tokens], 45):
            params = {exchange: chunk}
            response = self.obj.getMarketData("FULL", params)
            if not response or not response.get("status"):
                raise RuntimeError(f"Angel market data failed for {exchange}: {response}")
            data = response.get("data", {}) or {}
            all_fetched.extend(data.get("fetched", []) or [])
            all_unfetched.extend(data.get("unfetched", []) or [])
            time.sleep(0.15)
        return {"fetched": all_fetched, "unfetched": all_unfetched}

    def fetch_option_chain_snapshot(self, master: pd.DataFrame, instrument: str, expiry: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        option_rows = self.get_option_rows(master, instrument)
        option_rows = option_rows[option_rows["expiry"].astype(str).eq(str(expiry))].copy()
        if option_rows.empty:
            raise RuntimeError(f"No option contracts found for {instrument} expiry {expiry}.")
        exchange = self.option_exchange_for_instrument(instrument)
        tokens = option_rows["token"].astype(str).dropna().unique().tolist()
        md = self.get_market_data_full(exchange, tokens)
        fetched = pd.DataFrame(md.get("fetched", []))
        if fetched.empty:
            raise RuntimeError("Angel returned no fetched contracts for this expiry.")

        # SmartAPI field names can vary in case.
        rename_map = {
            "symbolToken": "token", "exchange": "exchange", "tradingSymbol": "symbol",
            "ltp": "ltp", "open": "open", "high": "high", "low": "low", "close": "close",
            "tradeVolume": "volume", "opnInterest": "oi", "openInterest": "oi",
            "exchFeedTime": "feed_time", "exchTradeTime": "trade_time",
        }
        fetched = fetched.rename(columns={k: v for k, v in rename_map.items() if k in fetched.columns})
        fetched["token"] = fetched["token"].astype(str)

        meta_cols = ["token", "symbol", "strike_norm", "option_type", "expiry", "exch_seg", "name"]
        meta = option_rows[[c for c in meta_cols if c in option_rows.columns]].copy()
        meta = meta.rename(columns={"strike_norm": "strike", "exch_seg": "exchange"})
        out = meta.merge(fetched, on="token", how="left", suffixes=("", "_md"))
        if "symbol_md" in out.columns:
            out["symbol"] = out["symbol"].fillna(out["symbol_md"])
        for col in ["ltp", "oi", "volume", "open", "high", "low", "close"]:
            if col not in out.columns:
                out[col] = 0
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
        out["bid"] = out.apply(_extract_best_bid, axis=1)
        out["ask"] = out.apply(_extract_best_ask, axis=1)
        out["instrument"] = instrument
        out["expiry"] = str(expiry)
        out["exchange"] = exchange
        usable = int((out["oi"] > 0).sum())
        info = {
            "exchange": exchange,
            "contracts": len(out),
            "usable_oi_count": usable,
            "unfetched_count": len(md.get("unfetched", [])),
        }
        return out, info


def _extract_best_bid(row: pd.Series) -> float:
    depth = row.get("depth", None)
    try:
        buy = depth.get("buy", []) if isinstance(depth, dict) else []
        if buy:
            return float(buy[0].get("price", 0) or 0)
    except Exception:
        return 0.0
    return 0.0


def _extract_best_ask(row: pd.Series) -> float:
    depth = row.get("depth", None)
    try:
        sell = depth.get("sell", []) if isinstance(depth, dict) else []
        if sell:
            return float(sell[0].get("price", 0) or 0)
    except Exception:
        return 0.0
    return 0.0
