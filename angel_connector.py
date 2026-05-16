"""Angel One SmartAPI connector for ODME Angel.

No NSE scraping is used. Market data and instruments are taken from Angel only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

from odme_config import (
    CACHE_DIR,
    CREDENTIALS_PATH,
    INSTRUMENT_CACHE_PATH,
    instrument_spec,
    ALL_UNDERLYINGS,
)

INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


class AngelConfigError(RuntimeError):
    pass


class AngelConnector:
    def __init__(self) -> None:
        self.credentials = self._load_credentials()
        self.obj = None
        self.jwt_token = None
        self.refresh_token = None
        self.feed_token = None

    @staticmethod
    def _load_credentials() -> Dict[str, str]:
        if not CREDENTIALS_PATH.exists():
            raise AngelConfigError(
                "Missing config/angel_credentials.json. Copy config/angel_credentials_TEMPLATE.json, "
                "rename it to angel_credentials.json, and fill API key, client ID and PIN."
            )
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        required = ["api_key", "client_id", "pin"]
        missing = [k for k in required if not str(data.get(k, "")).strip() or "PASTE_" in str(data.get(k, ""))]
        if missing:
            raise AngelConfigError(f"Credential file is incomplete. Missing/placeholder: {', '.join(missing)}")
        return {k: str(data[k]).strip() for k in required}

    def login(self, totp: str) -> Dict[str, Any]:
        try:
            from SmartApi.smartConnect import SmartConnect
        except Exception as exc:
            raise RuntimeError("SmartAPI package not available. Run: pip install -r requirements.txt") from exc

        totp = str(totp or "").strip()
        if not totp:
            raise ValueError("TOTP is required.")
        self.obj = SmartConnect(api_key=self.credentials["api_key"])
        session = self.obj.generateSession(self.credentials["client_id"], self.credentials["pin"], totp)
        if not session or not session.get("status"):
            raise RuntimeError(f"Angel login failed: {session}")
        data = session.get("data") or {}
        self.jwt_token = data.get("jwtToken")
        self.refresh_token = data.get("refreshToken")
        try:
            self.feed_token = self.obj.getfeedToken()
        except Exception:
            self.feed_token = data.get("feedToken")
        return {"client_id": self.credentials["client_id"], "feed_token": self.feed_token, "raw": session}

    @staticmethod
    def load_instrument_master(force_refresh: bool = False) -> pd.DataFrame:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if INSTRUMENT_CACHE_PATH.exists() and not force_refresh:
            try:
                return pd.read_parquet(INSTRUMENT_CACHE_PATH)
            except Exception:
                pass
        r = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        if df.empty:
            raise RuntimeError("Angel instrument master returned no rows.")
        for col in ["strike", "lotsize", "tick_size"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "expiry" in df.columns:
            df["expiry"] = df["expiry"].astype(str).str.upper().str.strip()
        if "exch_seg" in df.columns:
            df["exch_seg"] = df["exch_seg"].astype(str).str.upper().str.strip()
        if "instrumenttype" in df.columns:
            df["instrumenttype"] = df["instrumenttype"].astype(str).str.upper().str.strip()
        if "name" in df.columns:
            df["name"] = df["name"].astype(str).str.upper().str.strip()
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
        if "token" in df.columns:
            df["token"] = df["token"].astype(str)
        df.to_parquet(INSTRUMENT_CACHE_PATH, index=False)
        return df

    @staticmethod
    def option_rows(master: pd.DataFrame, underlying: str) -> pd.DataFrame:
        spec = instrument_spec(underlying)
        u = underlying.upper()
        df = master.copy()
        mask = df["exch_seg"].eq(spec.option_exchange)
        if "instrumenttype" in df.columns:
            mask &= df["instrumenttype"].eq(spec.instrument_type)
        # Angel master usually uses name for underlying and symbol/tradingsymbol for contract.
        name_match = df.get("name", pd.Series("", index=df.index)).astype(str).str.upper().eq(u)
        sym_match = df.get("symbol", pd.Series("", index=df.index)).astype(str).str.upper().str.startswith(u)
        ts_match = df.get("tradingsymbol", pd.Series("", index=df.index)).astype(str).str.upper().str.startswith(u)
        mask &= (name_match | sym_match | ts_match)
        out = df.loc[mask].copy()
        if out.empty:
            return out
        # Angel stores many index strikes scaled by 100; commodities may vary, normalize when obvious.
        out["strike_raw"] = pd.to_numeric(out["strike"], errors="coerce")
        median_strike = out["strike_raw"].dropna().median()
        if pd.notna(median_strike) and median_strike > 100000:
            out["strike_norm"] = out["strike_raw"] / 100.0
        else:
            out["strike_norm"] = out["strike_raw"]
        out["option_type"] = out["symbol"].astype(str).str.extract(r"(CE|PE)$", expand=False)
        if out["option_type"].isna().all() and "tradingsymbol" in out.columns:
            out["option_type"] = out["tradingsymbol"].astype(str).str.extract(r"(CE|PE)$", expand=False)
        out = out[out["option_type"].isin(["CE", "PE"])]
        return out

    @staticmethod
    def expiries(master: pd.DataFrame, underlying: str) -> List[str]:
        rows = AngelConnector.option_rows(master, underlying)
        if rows.empty:
            return []
        temp = rows[["expiry"]].dropna().drop_duplicates().copy()
        temp["dt"] = pd.to_datetime(temp["expiry"], format="%d%b%Y", errors="coerce")
        temp = temp.sort_values(["dt", "expiry"], na_position="last")
        return temp["expiry"].astype(str).tolist()

    @staticmethod
    def contracts_for_expiry(master: pd.DataFrame, underlying: str, expiry: str) -> pd.DataFrame:
        rows = AngelConnector.option_rows(master, underlying)
        return rows[rows["expiry"].astype(str).str.upper().eq(str(expiry).upper())].copy()

    def _market_data_full(self, exchange_tokens: Dict[str, List[str]]) -> Dict[str, Any]:
        if self.obj is None:
            raise RuntimeError("Login first.")
        try:
            res = self.obj.getMarketData("FULL", exchange_tokens)
        except TypeError:
            # Compatibility with some SmartAPI versions.
            res = self.obj.getMarketData(mode="FULL", exchangeTokens=exchange_tokens)
        if not res or not res.get("status"):
            raise RuntimeError(f"Angel market data FULL failed: {res}")
        return res

    def fetch_option_chain(self, master: pd.DataFrame, underlying: str, expiry: str) -> Tuple[pd.DataFrame, float, Dict[str, Any]]:
        contracts = self.contracts_for_expiry(master, underlying, expiry)
        if contracts.empty:
            raise RuntimeError(f"No Angel contracts found for {underlying} {expiry}.")
        spec = instrument_spec(underlying)
        tokens = contracts["token"].astype(str).dropna().unique().tolist()
        rows: List[Dict[str, Any]] = []
        # Angel market data accepts batches; keep conservative size.
        token_to_contract = contracts.set_index(contracts["token"].astype(str)).to_dict("index")
        for i in range(0, len(tokens), 50):
            batch = tokens[i : i + 50]
            res = self._market_data_full({spec.option_exchange: batch})
            fetched = ((res.get("data") or {}).get("fetched") or [])
            for item in fetched:
                token = str(item.get("symbolToken") or item.get("token") or "")
                c = token_to_contract.get(token, {})
                oi = item.get("opnInterest", item.get("openInterest", item.get("oi", 0)))
                ltp = item.get("ltp", item.get("lastTradedPrice", 0))
                vol = item.get("tradeVolume", item.get("volume", 0))
                rows.append(
                    {
                        "token": token,
                        "tradingsymbol": c.get("symbol") or c.get("tradingsymbol") or item.get("tradingSymbol"),
                        "name": c.get("name", underlying),
                        "exchange": spec.option_exchange,
                        "expiry": c.get("expiry", expiry),
                        "strike": float(c.get("strike_norm")) if pd.notna(c.get("strike_norm")) else None,
                        "strike_raw": c.get("strike_raw"),
                        "option_type": c.get("option_type"),
                        "ltp": _to_float(ltp),
                        "open": _to_float(item.get("open", 0)),
                        "high": _to_float(item.get("high", 0)),
                        "low": _to_float(item.get("low", 0)),
                        "close": _to_float(item.get("close", 0)),
                        "volume": _to_float(vol),
                        "oi": _to_float(oi),
                        "bid_qty": _depth_qty(item, "buy"),
                        "ask_qty": _depth_qty(item, "sell"),
                        "feed_time": item.get("feedTime") or item.get("exchFeedTime"),
                        "trade_time": item.get("lastTradeTime") or item.get("exchTradeTime"),
                    }
                )
        chain = pd.DataFrame(rows)
        if chain.empty:
            raise RuntimeError("Angel returned no option-chain rows.")
        chain = chain.dropna(subset=["strike", "option_type"])
        spot = self._estimate_spot_from_chain(chain)
        meta = {"contracts_requested": len(tokens), "contracts_fetched": len(chain), "non_zero_oi": int((chain["oi"] > 0).sum())}
        return chain, spot, meta

    @staticmethod
    def _estimate_spot_from_chain(chain: pd.DataFrame) -> float:
        df = chain.copy()
        # PCR parity proxy: spot near strike where absolute CE/PE LTP gap is smallest.
        piv = df.pivot_table(index="strike", columns="option_type", values="ltp", aggfunc="last")
        if {"CE", "PE"}.issubset(piv.columns):
            temp = piv.dropna(subset=["CE", "PE"]).copy()
            temp = temp[(temp["CE"] > 0) & (temp["PE"] > 0)]
            if not temp.empty:
                return float((temp["CE"] - temp["PE"]).abs().idxmin())
        # fallback: OI-weighted strike center
        valid = df[df["oi"] > 0]
        if not valid.empty:
            return float((valid["strike"] * valid["oi"]).sum() / valid["oi"].sum())
        return float(df["strike"].median())


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _depth_qty(item: Dict[str, Any], side: str) -> float:
    # SmartAPI depth keys vary slightly. Sum first five depth quantities when present.
    depth = item.get("depth") or item.get("marketDepth") or {}
    arr = depth.get(side) or depth.get(side.capitalize()) or []
    total = 0.0
    for row in arr[:5] if isinstance(arr, list) else []:
        total += _to_float(row.get("quantity") or row.get("qty") or 0)
    return total
