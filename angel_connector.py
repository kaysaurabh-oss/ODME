from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

from odme_config import ANGEL_INSTRUMENT_MASTER_URL, LOCAL_CONFIG_PATH, MCX_SYMBOLS, MCX_STRIKE_SCALE_DIVISOR

try:
    from SmartApi import SmartConnect
except Exception:  # pragma: no cover
    SmartConnect = None


@dataclass
class AngelCredentials:
    api_key: str
    client_id: str
    pin: str


class AngelDataError(RuntimeError):
    """Raised when Angel data is missing, inconsistent, or unsafe to use."""


class AngelSessionError(RuntimeError):
    """Raised when Angel login/session is unavailable or expired."""


def _read_streamlit_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def load_angel_credentials() -> AngelCredentials:
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


# Strict roots prevent accidental mixing: SILVERM != SILVERMIC, GOLD != GOLDM, CRUDEOIL != CRUDEOILM.
INSTRUMENT_ALIASES = {
    "SILVERM": ["SILVERM"],
    "SILVERMIC": ["SILVERMIC"],
    "SILVER": ["SILVER"],
    "GOLDM": ["GOLDM"],
    "GOLD": ["GOLD"],
    "CRUDEOILM": ["CRUDEOILM"],
    "CRUDEOIL": ["CRUDEOIL"],
    "NATGASMINI": ["NATGASMINI"],
    "NATURALGAS": ["NATURALGAS"],
}


def _aliases_for(instrument: str) -> List[str]:
    root = str(instrument).upper().strip()
    return INSTRUMENT_ALIASES.get(root, [root])


def _strict_symbol_root_match(symbol_series: pd.Series, roots: List[str]) -> pd.Series:
    symbols = symbol_series.astype(str).str.upper().str.strip()
    mask = pd.Series(False, index=symbols.index)
    for root in roots:
        root = root.upper().strip()
        starts = symbols.str.startswith(root, na=False)
        next_char = symbols.str.slice(len(root), len(root) + 1)
        boundary = next_char.eq("") | ~next_char.str.match(r"[A-Z]", na=False)
        mask = mask | (starts & boundary)
    return mask


def _parse_expiry_date(value: Any) -> pd.Timestamp:
    s = str(value or "").strip().upper()
    if not s:
        return pd.NaT
    # Angel typically uses 29MAY2025, but keep fallbacks.
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return pd.Timestamp(datetime.strptime(s, fmt))
        except Exception:
            pass
    return pd.to_datetime(s, errors="coerce")


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return default
        return float(x)
    except Exception:
        return default


class AngelConnector:
    def __init__(self, credentials: AngelCredentials):
        self.credentials = credentials
        self.obj: Optional[Any] = None
        self.jwt_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.login_time_utc: Optional[str] = None
        self.last_session_error: str = ""

    def login(self, totp: str) -> Dict[str, Any]:
        if SmartConnect is None:
            raise RuntimeError("SmartApi package not installed. Run: pip install -r requirements.txt")
        totp = str(totp).strip()
        if not totp:
            raise ValueError("Enter current Angel TOTP.")
        self.obj = SmartConnect(api_key=self.credentials.api_key)
        data = self.obj.generateSession(self.credentials.client_id, self.credentials.pin, totp)
        if not data or not data.get("status"):
            raise AngelSessionError(f"Angel login failed: {data}")
        payload = data.get("data", {}) or {}
        self.jwt_token = payload.get("jwtToken")
        self.refresh_token = payload.get("refreshToken")
        try:
            self.feed_token = self.obj.getfeedToken()
        except Exception:
            self.feed_token = payload.get("feedToken")
        self.login_time_utc = datetime.now(timezone.utc).isoformat()
        self.last_session_error = ""
        return data

    def ensure_session_ready(self) -> None:
        if self.obj is None or not self.jwt_token:
            raise AngelSessionError(
                "Angel session is not active. Streamlit may have restarted or the browser session was cleared. "
                "Please login once again with current TOTP."
            )

    def session_label(self) -> str:
        if not self.login_time_utc:
            return "Angel session active"
        return f"Angel session active since {self.login_time_utc[:19].replace('T', ' ')} UTC"

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
        df["expiry_dt"] = df["expiry"].apply(_parse_expiry_date) if "expiry" in df.columns else pd.NaT
        return df

    @staticmethod
    def option_exchange_for_instrument(instrument: str) -> str:
        return "MCX" if instrument in MCX_SYMBOLS else "NFO"

    @staticmethod
    def normalize_strike(row: pd.Series) -> float:
        """Normalize Angel master strike to the actual displayed strike.

        Angel uses 100x strike storage for many contracts, but the old generic
        rule (divide only when strike >= 100000) misses lower-priced MCX
        contracts such as NATURALGAS, NATGASMINI, COPPER and ZINC.

        This function therefore uses an instrument-specific MCX scale first.
        It keeps SILVER/SILVERM/SILVERMIC unscaled because those contracts are
        quoted at large absolute strike levels.
        """
        strike = _safe_float(row.get("strike", 0))
        if strike <= 0:
            return 0.0

        exch = str(row.get("exch_seg", "")).upper().strip()
        name = str(row.get("name", "")).upper().strip()

        if exch == "MCX" and name in MCX_STRIKE_SCALE_DIVISOR:
            divisor = _safe_float(MCX_STRIKE_SCALE_DIVISOR.get(name, 1.0), 1.0)
            if divisor and divisor != 1.0:
                return strike / divisor
            return strike

        # NSE FO index/equity options are generally 100x in Angel master.
        if exch == "NFO":
            return strike / 100.0 if strike >= 100000 else strike

        # Conservative fallback for very large raw strikes.
        if strike >= 100000:
            return strike / 100.0
        return strike

    @classmethod
    def _root_mask(cls, df: pd.DataFrame, instrument: str) -> pd.Series:
        roots = _aliases_for(instrument)
        name_col = df.get("name", pd.Series("", index=df.index)).astype(str).str.upper().str.strip()
        symbol_col = df.get("symbol", pd.Series("", index=df.index)).astype(str).str.upper().str.strip()
        name_match = name_col.isin([r.upper() for r in roots])
        symbol_match = _strict_symbol_root_match(symbol_col, roots)
        return name_match | symbol_match

    @classmethod
    def get_option_rows(cls, master: pd.DataFrame, instrument: str) -> pd.DataFrame:
        if master.empty:
            return master
        exch = cls.option_exchange_for_instrument(instrument)
        df = master.copy()
        exch_match = df.get("exch_seg", pd.Series("", index=df.index)).astype(str).str.upper().eq(exch)
        opt_type_match = df.get("instrumenttype", pd.Series("", index=df.index)).astype(str).str.upper().str.contains("OPT", na=False)
        df = df[exch_match & opt_type_match & cls._root_mask(df, instrument)].copy()
        if df.empty:
            return df
        df["strike_norm"] = df.apply(cls.normalize_strike, axis=1)
        df["option_type"] = df["symbol"].astype(str).str.upper().str.extract(r"(CE|PE)$", expand=False)
        df = df[df["option_type"].isin(["CE", "PE"])]
        return df.sort_values(["expiry_dt", "expiry", "strike_norm", "option_type", "symbol"], na_position="last")

    @classmethod
    def get_future_rows(cls, master: pd.DataFrame, instrument: str) -> pd.DataFrame:
        """Return only true futures rows for the instrument.

        Important Angel/MCX quirk:
        Commodity options can have instrumenttype values such as OPTFUT. A loose
        contains("FUT") check therefore pulls option rows like
        SILVERM19JUN26300000CE into the futures list. That creates false
        futures LTPs from option premiums.

        Futures selection must therefore satisfy all of these:
        - same exchange/root
        - instrument type contains FUT
        - instrument type does NOT contain OPT
        - symbol does NOT end with CE/PE
        """
        if master.empty:
            return master
        exch = cls.option_exchange_for_instrument(instrument)
        df = master.copy()
        exch_match = df.get("exch_seg", pd.Series("", index=df.index)).astype(str).str.upper().eq(exch)

        itype = df.get("instrumenttype", pd.Series("", index=df.index)).astype(str).str.upper().str.strip()
        symbol = df.get("symbol", pd.Series("", index=df.index)).astype(str).str.upper().str.strip()

        # True futures include FUTIDX/FUTSTK/FUTCOM etc.
        # Exclude OPTIDX/OPTSTK/OPTFUT/OPTCOM and any CE/PE symbols.
        fut_type_match = itype.str.contains("FUT", na=False) & ~itype.str.contains("OPT", na=False)
        not_option_symbol = ~symbol.str.endswith(("CE", "PE"), na=False)

        df = df[exch_match & fut_type_match & not_option_symbol & cls._root_mask(df, instrument)].copy()
        if df.empty:
            return df
        return df.sort_values(["expiry_dt", "expiry", "symbol"], na_position="last")

    @classmethod
    def get_expiries(cls, master: pd.DataFrame, instrument: str) -> List[str]:
        rows = cls.get_option_rows(master, instrument)
        if rows.empty or "expiry" not in rows.columns:
            return []
        # Keep chronological order where possible.
        temp = rows[["expiry", "expiry_dt"]].drop_duplicates().copy()
        temp = temp.sort_values(["expiry_dt", "expiry"], na_position="last")
        return temp["expiry"].dropna().astype(str).unique().tolist()

    @staticmethod
    def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def get_market_data_full(self, exchange: str, tokens: List[str]) -> Dict[str, Any]:
        self.ensure_session_ready()
        all_fetched: List[Dict[str, Any]] = []
        all_unfetched: List[Dict[str, Any]] = []
        for chunk in self._chunks([str(t) for t in tokens if str(t).strip()], 45):
            params = {exchange: chunk}
            try:
                response = self.obj.getMarketData("FULL", params)
            except Exception as exc:
                raise AngelSessionError(f"Angel market-data request failed. Session may be expired or network/API failed: {exc}") from exc
            if not response or not response.get("status"):
                msg = str(response)
                if re.search(r"token|session|jwt|invalid|expired|login", msg, re.I):
                    raise AngelSessionError(f"Angel session/API rejected market-data request. Login again with fresh TOTP. Details: {msg}")
                raise AngelDataError(f"Angel market data failed for {exchange}: {msg}")
            data = response.get("data", {}) or {}
            all_fetched.extend(data.get("fetched", []) or [])
            all_unfetched.extend(data.get("unfetched", []) or [])
            time.sleep(0.15)
        return {"fetched": all_fetched, "unfetched": all_unfetched}

    @staticmethod
    def _normalize_market_df(records: List[Dict[str, Any]]) -> pd.DataFrame:
        fetched = pd.DataFrame(records)
        if fetched.empty:
            return fetched
        rename_map = {
            "symbolToken": "token", "exchange": "exchange", "tradingSymbol": "symbol",
            "ltp": "ltp", "open": "open", "high": "high", "low": "low", "close": "close",
            "tradeVolume": "volume", "opnInterest": "oi", "openInterest": "oi",
            "exchFeedTime": "feed_time", "exchTradeTime": "trade_time",
        }
        fetched = fetched.rename(columns={k: v for k, v in rename_map.items() if k in fetched.columns})
        if "token" in fetched.columns:
            fetched["token"] = fetched["token"].astype(str)
        for col in ["ltp", "oi", "volume", "open", "high", "low", "close"]:
            if col in fetched.columns:
                fetched[col] = pd.to_numeric(fetched[col], errors="coerce").fillna(0)
        return fetched

    def fetch_future_ltp(
        self,
        master: pd.DataFrame,
        instrument: str,
        option_rows: Optional[pd.DataFrame] = None,
        option_expiry: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch verified related futures LTP for the selected option expiry.

        For index weeklies and MCX commodity options, option expiry frequently does not
        match the futures expiry. The correct futures proxy is the nearest active futures
        contract whose expiry is on/after the selected option expiry. If that is not
        available, we fall back to the nearest active futures contract and clearly expose
        the mapping in the verification panel.
        """
        futures = self.get_future_rows(master, instrument)
        if futures.empty:
            raise AngelDataError(
                f"No futures contract found in Angel master for {instrument}. ODME blocked because related futures LTP is required."
            )

        today = pd.Timestamp(datetime.now().date())
        futures = futures[(futures["expiry_dt"].isna()) | (futures["expiry_dt"] >= today)].copy()
        if futures.empty:
            raise AngelDataError(f"Only expired futures found for {instrument}. ODME blocked.")

        option_expiry_dt = _parse_expiry_date(option_expiry) if option_expiry else pd.NaT
        exchange = self.option_exchange_for_instrument(instrument)
        is_mcx = exchange == "MCX"

        # Build a full same-root futures candidate set. Do NOT take only the first future,
        # because MCX options often expire before the referenced futures contract.
        # Example: SILVERM option may expire on 26-May/19-Jun while the liquid futures
        # reference is 30-Jun. Same-expiry futures, if present, are often zero/irrelevant.
        futures["_expiry_rank"] = 9_999_999
        futures["_selection_reason"] = "nearest active futures"
        futures["_is_related_to_option_expiry"] = False

        if not pd.isna(option_expiry_dt):
            diff_days = (futures["expiry_dt"] - option_expiry_dt).dt.days
            if is_mcx:
                # For MCX commodity options, prefer the nearest futures expiry STRICTLY AFTER
                # the option expiry. This avoids trying to fetch a same-date option expiry as
                # the futures reference when the actual tradable futures month is later.
                related_mask = diff_days.gt(0)
                related_label = "nearest same-root MCX futures expiry after selected option expiry"
            else:
                # For NSE index options, monthly futures can be on/after weekly option expiry.
                related_mask = diff_days.ge(0)
                related_label = "nearest futures expiry on/after selected option expiry"

            if related_mask.any():
                futures.loc[related_mask, "_expiry_rank"] = diff_days[related_mask].fillna(999999).astype(int)
                futures.loc[related_mask, "_selection_reason"] = related_label
                futures.loc[related_mask, "_is_related_to_option_expiry"] = True
                candidates = futures.copy()
            else:
                # Explicit fallback. We still fetch all active same-root futures and later pick
                # a non-zero quote, but the reason will clearly show no after-expiry future existed.
                diff_today = (futures["expiry_dt"] - today).dt.days
                futures["_expiry_rank"] = diff_today.fillna(999999).astype(int)
                futures["_selection_reason"] = "fallback: no same-root futures expiry after selected option expiry; using nearest quoted active future"
                candidates = futures.copy()
        else:
            diff_today = (futures["expiry_dt"] - today).dt.days
            futures["_expiry_rank"] = diff_today.fillna(999999).astype(int)
            futures["_selection_reason"] = "nearest active futures; option expiry date could not be parsed"
            candidates = futures.copy()

        candidates = candidates.sort_values(["_is_related_to_option_expiry", "_expiry_rank", "expiry_dt", "expiry"], ascending=[False, True, True, True], na_position="last")
        tokens = candidates["token"].astype(str).dropna().unique().tolist()
        if not tokens:
            raise AngelDataError(f"No usable futures tokens found for {instrument}. ODME blocked.")

        # Fetch every same-root active future candidate. Candidate count is normally very small;
        # this prevents zero-LTP same-expiry candidates from blocking the later liquid future.
        md = self.get_market_data_full(exchange, tokens)
        fetched = self._normalize_market_df(md.get("fetched", []))
        if fetched.empty:
            preview = candidates[[c for c in ["symbol", "expiry", "token"] if c in candidates.columns]].head(10).to_dict("records")
            raise AngelDataError(
                f"Angel returned no futures quote for {instrument}. ODME blocked; cannot verify related futures LTP. "
                f"Candidate futures checked: {preview}"
            )

        meta_cols = [c for c in ["token", "symbol", "expiry", "expiry_dt", "exch_seg", "name", "_expiry_rank", "_selection_reason", "_is_related_to_option_expiry"] if c in candidates.columns]
        meta = candidates[meta_cols].copy()
        meta["token"] = meta["token"].astype(str)
        fut = meta.merge(fetched, on="token", how="left", suffixes=("", "_md"))
        if "symbol_md" in fut.columns:
            fut["symbol"] = fut["symbol"].fillna(fut["symbol_md"])
        fut["ltp"] = pd.to_numeric(fut.get("ltp", 0), errors="coerce").fillna(0)
        fut["volume"] = pd.to_numeric(fut.get("volume", 0), errors="coerce").fillna(0)

        active = fut[fut["ltp"] > 0].copy()
        if active.empty:
            preview_cols = [c for c in ["symbol", "expiry", "token", "ltp", "volume", "_selection_reason"] if c in fut.columns]
            preview = fut[preview_cols].head(12).to_dict("records")
            raise AngelDataError(
                f"Related same-root futures quotes for {instrument} have zero/missing LTP. ODME blocked; "
                f"no assumption made. Candidate futures checked: {preview}"
            )

        # Primary selection: active related future after/on option expiry as per exchange rule.
        related_active = active[active["_is_related_to_option_expiry"].fillna(False)].copy()
        if not related_active.empty:
            active_pick = related_active.sort_values(["_expiry_rank", "expiry_dt", "volume"], ascending=[True, True, False], na_position="last")
        else:
            # Fallback: highest-quality active same-root future. This is only used when no after-expiry
            # related future has a quote; the reason is exposed to the UI verification panel.
            active["_selection_reason"] = active["_selection_reason"].astype(str) + " | fallback picked quoted same-root future"
            active_pick = active.sort_values(["_expiry_rank", "expiry_dt", "volume"], ascending=[True, True, False], na_position="last")

        row = active_pick.iloc[0].to_dict()
        ltp = _safe_float(row.get("ltp"))

        # Dynamic validation against selected option-chain strikes. This prevents nonsense like NIFTY 70k.
        if option_rows is not None and not option_rows.empty and "strike_norm" in option_rows.columns:
            strikes = pd.to_numeric(option_rows["strike_norm"], errors="coerce").dropna()
            strikes = strikes[strikes > 0]
            if not strikes.empty:
                mn, mx = float(strikes.min()), float(strikes.max())
                buffer = max((mx - mn) * 0.05, ltp * 0.01)
                if not (mn - buffer <= ltp <= mx + buffer):
                    raise AngelDataError(
                        f"Unsafe related futures LTP for {instrument}: {ltp:,.2f} from {row.get('symbol')} "
                        f"is outside selected option-chain strike range {mn:,.0f}–{mx:,.0f}. "
                        f"ODME blocked instead of assuming wrong spot. Futures mapping reason: {row.get('_selection_reason', '')}."
                    )

        return {
            "future_ltp": ltp,
            "future_symbol": str(row.get("symbol", "")),
            "future_token": str(row.get("token", "")),
            "future_expiry": str(row.get("expiry", "")),
            "future_exchange": exchange,
            "future_feed_time": str(row.get("feed_time", "")),
            "future_trade_time": str(row.get("trade_time", "")),
            "future_mapping_reason": str(row.get("_selection_reason", "")),
            "option_expiry_used_for_mapping": str(option_expiry or ""),
        }

    def fetch_option_chain_snapshot(self, master: pd.DataFrame, instrument: str, expiry: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        self.ensure_session_ready()
        option_rows = self.get_option_rows(master, instrument)
        option_rows = option_rows[option_rows["expiry"].astype(str).eq(str(expiry))].copy()
        if option_rows.empty:
            raise AngelDataError(f"No option contracts found for {instrument} expiry {expiry}.")
        exchange = self.option_exchange_for_instrument(instrument)

        future_info = self.fetch_future_ltp(master, instrument, option_rows=option_rows, option_expiry=expiry)
        tokens = option_rows["token"].astype(str).dropna().unique().tolist()
        md = self.get_market_data_full(exchange, tokens)
        fetched = self._normalize_market_df(md.get("fetched", []))
        if fetched.empty:
            raise AngelDataError("Angel returned no fetched option contracts for this expiry.")

        meta_cols = ["token", "symbol", "strike_norm", "option_type", "expiry", "exch_seg", "name"]
        meta = option_rows[[c for c in meta_cols if c in option_rows.columns]].copy()
        meta = meta.rename(columns={"strike_norm": "strike", "exch_seg": "exchange"})
        meta["token"] = meta["token"].astype(str)
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
        if usable <= 0:
            # Do not block saving entirely, but clearly warn. Some MCX expiries may be inactive.
            pass
        info = {
            "exchange": exchange,
            "contracts": len(out),
            "usable_oi_count": usable,
            "unfetched_count": len(md.get("unfetched", [])),
            **future_info,
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
