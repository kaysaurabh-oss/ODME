from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from odme_config import GOOGLE_SHEET_DEFAULT_NAME, LOCAL_DATA_DIR

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:  # pragma: no cover
    gspread = None
    Credentials = None


ODME_TAB = "odme_snapshots"

ODME_COLUMNS = [
    "snapshot_id", "key", "ts", "instrument", "exchange", "expiry",
    "spot", "option_poc", "value_area_low", "value_area_high",
    "ce_wall", "pe_wall", "ce_wall_shift", "pe_wall_shift", "poc_shift", "range_shift",
    "bullish_score", "bearish_score", "range_score", "expansion_score", "odme_tilt",
    "safer_sell_ce", "active_ce_wall", "safer_sell_pe", "active_pe_wall",
    "hvn", "lvn", "key_strikes_json", "commentary", "source", "usable_oi_count", "notes"
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_key(instrument: str, expiry: str) -> str:
    safe_expiry = str(expiry).replace(" ", "_").replace("/", "-")
    return f"{instrument.upper()}__{safe_expiry}"


def make_snapshot_id(key: str) -> str:
    return f"{key}__{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}__{uuid.uuid4().hex[:8]}"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return ""


def _json_loads(value: Any, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        if value is None or value == "":
            return default
        return json.loads(str(value))
    except Exception:
        return default


def select_prior_day_anchor(history: pd.DataFrame, tz_name: str = "Asia/Kolkata") -> Dict[str, Any]:
    """Pick the latest saved snapshot strictly before today's local date as anchor.

    This is not limited to yesterday. If the previous trading session was Friday
    and today is Monday, Friday's latest saved snapshot is the anchor. If there
    are holidays or the app was not opened for several days, the most recent
    saved snapshot before today is used. Same-day snapshots are intentionally
    excluded so intraday commentary does not drift against the last refresh.
    """
    if history is None or history.empty or "ts" not in history.columns:
        return {}
    df = history.copy()
    df["_ts_utc"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["_ts_utc"])
    if df.empty:
        return {}
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    now_local_date = datetime.now(tz).date()
    df["_local_date"] = df["_ts_utc"].dt.tz_convert(tz).dt.date
    prior = df[df["_local_date"] < now_local_date].copy()
    if prior.empty:
        return {}
    prior = prior.sort_values("_ts_utc")
    return prior.iloc[-1].drop(labels=[c for c in ["_ts_utc", "_local_date"] if c in prior.columns]).to_dict()


class BaseStore:
    def ensure(self) -> None:
        raise NotImplementedError

    def append_odme_snapshot(self, result: Dict[str, Any], meta: Dict[str, Any]) -> None:
        raise NotImplementedError

    def load_latest_odme_snapshot(self, key: str) -> Dict[str, Any]:
        raise NotImplementedError

    def load_anchor_odme_snapshot(self, key: str, tz_name: str = "Asia/Kolkata") -> Dict[str, Any]:
        """Return the fixed prior-day closing anchor for intraday comparison.

        ODME live refreshes compare against the latest saved snapshot before
        today's local date, not against the previous same-day refresh. This keeps
        the previous-session positioning as the fixed anchor throughout the day.
        """
        hist = self.load_odme_history(key, limit=500)
        return select_prior_day_anchor(hist, tz_name=tz_name)

    def load_odme_history(self, key: Optional[str] = None, limit: int = 100) -> pd.DataFrame:
        raise NotImplementedError

    def list_initialized(self) -> pd.DataFrame:
        hist = self.load_odme_history(limit=500)
        if hist.empty:
            return pd.DataFrame(columns=["key", "instrument", "exchange", "expiry", "initialized_at", "last_fetch_at", "snapshots", "status", "notes"])
        hist = hist.astype(str).fillna("")
        rows = []
        for key, g in hist.groupby("key", sort=False):
            g = g.sort_values("ts")
            first = g.iloc[0]
            last = g.iloc[-1]
            rows.append({
                "key": key,
                "instrument": last.get("instrument", ""),
                "exchange": last.get("exchange", ""),
                "expiry": last.get("expiry", ""),
                "initialized_at": first.get("ts", ""),
                "last_fetch_at": last.get("ts", ""),
                "snapshots": len(g),
                "status": "ACTIVE" if _to_float(last.get("usable_oi_count")) > 0 else "NO_USABLE_OI",
                "notes": last.get("notes", ""),
            })
        return pd.DataFrame(rows)

    def is_initialized(self, key: str) -> bool:
        latest = self.load_latest_odme_snapshot(key)
        return bool(latest)

    # Backward-compatible no-op method name from old app.
    def upsert_initialized(self, row: Dict[str, Any]) -> None:
        return None


class LocalStore(BaseStore):
    def __init__(self, data_dir: str = LOCAL_DATA_DIR):
        self.root = Path(data_dir)
        self.path = self.root / "odme_snapshots.csv"

    def ensure(self) -> None:
        self.root.mkdir(exist_ok=True)
        if not self.path.exists():
            pd.DataFrame(columns=ODME_COLUMNS).to_csv(self.path, index=False)

    def append_odme_snapshot(self, result: Dict[str, Any], meta: Dict[str, Any]) -> None:
        self.ensure()
        df = pd.read_csv(self.path, dtype=str).fillna("")
        row = make_summary_row(result, meta)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df.to_csv(self.path, index=False)

    def load_odme_history(self, key: Optional[str] = None, limit: int = 100) -> pd.DataFrame:
        self.ensure()
        df = pd.read_csv(self.path, dtype=str).fillna("")
        if key:
            df = df[df["key"].astype(str).eq(str(key))]
        if not df.empty:
            df = df.sort_values("ts").tail(limit)
        return df

    def load_latest_odme_snapshot(self, key: str) -> Dict[str, Any]:
        df = self.load_odme_history(key, limit=1)
        if df.empty:
            return {}
        return df.iloc[-1].to_dict()


class GoogleSheetStore(BaseStore):
    def __init__(self, sheet_name: str = GOOGLE_SHEET_DEFAULT_NAME):
        if gspread is None or Credentials is None:
            raise RuntimeError("Google Sheets packages missing. Run: pip install -r requirements.txt")
        self.sheet_name = sheet_name
        self.gc = self._client_from_streamlit_secrets()
        self.sheet = self._open_or_create_sheet(sheet_name)
        self._ws_cache: Dict[str, Any] = {}
        self._ensured = False

    @staticmethod
    def available_from_secrets() -> bool:
        try:
            return bool(st.secrets.get("gcp_service_account"))
        except Exception:
            return False

    @staticmethod
    def enabled_from_secrets() -> bool:
        try:
            return bool(st.secrets.get("USE_GOOGLE_SHEETS", False))
        except Exception:
            return False

    @staticmethod
    def sheet_name_from_secrets() -> str:
        try:
            return str(st.secrets.get("GOOGLE_SHEET_NAME", GOOGLE_SHEET_DEFAULT_NAME))
        except Exception:
            return GOOGLE_SHEET_DEFAULT_NAME

    @staticmethod
    def _client_from_streamlit_secrets():
        sa_info = dict(st.secrets["gcp_service_account"])
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
        return gspread.authorize(creds)

    def _open_or_create_sheet(self, sheet_name: str):
        try:
            return self.gc.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            return self.gc.create(sheet_name)

    def _worksheet(self, title: str):
        if title in self._ws_cache:
            return self._ws_cache[title]
        try:
            ws = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=2000, cols=max(len(ODME_COLUMNS), 30))
            ws.update("A1", [ODME_COLUMNS])
        self._ws_cache[title] = ws
        return ws

    def ensure(self) -> None:
        if self._ensured:
            return
        ws = self._worksheet(ODME_TAB)
        header = ws.row_values(1)
        if header != ODME_COLUMNS:
            # Keep it simple and safe for this new light version.
            # If old heavy tabs exist, they are left untouched. Only this tab is managed.
            ws.clear()
            ws.update("A1", [ODME_COLUMNS])
        self._ensured = True

    def append_odme_snapshot(self, result: Dict[str, Any], meta: Dict[str, Any]) -> None:
        self.ensure()
        ws = self._worksheet(ODME_TAB)
        row = make_summary_row(result, meta)
        ws.append_row([row.get(c, "") for c in ODME_COLUMNS], value_input_option="USER_ENTERED")

    def load_odme_history(self, key: Optional[str] = None, limit: int = 100) -> pd.DataFrame:
        self.ensure()
        ws = self._worksheet(ODME_TAB)
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        if df.empty:
            return pd.DataFrame(columns=ODME_COLUMNS)
        for col in ODME_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[ODME_COLUMNS].astype(str).fillna("")
        if key:
            df = df[df["key"].astype(str).eq(str(key))]
        if not df.empty:
            df = df.sort_values("ts").tail(limit)
        return df

    def load_latest_odme_snapshot(self, key: str) -> Dict[str, Any]:
        df = self.load_odme_history(key, limit=1)
        if df.empty:
            return {}
        return df.iloc[-1].to_dict()


@st.cache_resource(show_spinner=False)
def get_store() -> BaseStore:
    if GoogleSheetStore.enabled_from_secrets() and GoogleSheetStore.available_from_secrets():
        store: BaseStore = GoogleSheetStore(GoogleSheetStore.sheet_name_from_secrets())
    else:
        store = LocalStore()
    store.ensure()
    return store


def make_summary_row(result: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    scores = result.get("scores", {}) or {}
    row = {
        "snapshot_id": meta.get("snapshot_id", make_snapshot_id(str(meta.get("key", "ODME")))),
        "key": meta.get("key", ""),
        "ts": meta.get("ts", utc_now_iso()),
        "instrument": meta.get("instrument", ""),
        "exchange": meta.get("exchange", ""),
        "expiry": meta.get("expiry", ""),
        "spot": result.get("spot", ""),
        "option_poc": result.get("poc", ""),
        "value_area_low": result.get("value_area_low", ""),
        "value_area_high": result.get("value_area_high", ""),
        "ce_wall": result.get("ce_wall", ""),
        "pe_wall": result.get("pe_wall", ""),
        "ce_wall_shift": result.get("ce_wall_move", ""),
        "pe_wall_shift": result.get("pe_wall_move", ""),
        "poc_shift": result.get("poc_move", ""),
        "range_shift": result.get("range_move", ""),
        "bullish_score": scores.get("Bullish", 0),
        "bearish_score": scores.get("Bearish", 0),
        "range_score": scores.get("Range", 0),
        "expansion_score": scores.get("Expansion", 0),
        "odme_tilt": result.get("tilt", ""),
        "safer_sell_ce": result.get("safer_sell_ce", ""),
        "active_ce_wall": result.get("ce_wall", ""),
        "safer_sell_pe": result.get("safer_sell_pe", ""),
        "active_pe_wall": result.get("pe_wall", ""),
        "hvn": _json_dumps(result.get("hvn", [])),
        "lvn": _json_dumps(result.get("lvn", [])),
        "key_strikes_json": _json_dumps(result.get("key_strikes", {})),
        "commentary": result.get("commentary", ""),
        "source": meta.get("source", "Angel SmartAPI FULL → ODME summary"),
        "usable_oi_count": meta.get("usable_oi_count", ""),
        "notes": meta.get("notes", ""),
    }
    return {c: row.get(c, "") for c in ODME_COLUMNS}


def parse_previous_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    if not row:
        return {}
    out = dict(row)
    out["spot"] = _to_float(out.get("spot"))
    out["poc"] = _to_float(out.get("option_poc"))
    out["ce_wall"] = _to_float(out.get("ce_wall"))
    out["pe_wall"] = _to_float(out.get("pe_wall"))
    out["safer_sell_ce"] = _to_float(out.get("safer_sell_ce"))
    out["safer_sell_pe"] = _to_float(out.get("safer_sell_pe"))
    out["value_area_low"] = _to_float(out.get("value_area_low"))
    out["value_area_high"] = _to_float(out.get("value_area_high"))
    out["scores"] = {
        "Bullish": _to_float(out.get("bullish_score")),
        "Bearish": _to_float(out.get("bearish_score")),
        "Range": _to_float(out.get("range_score")),
        "Expansion": _to_float(out.get("expansion_score")),
    }
    out["key_strikes"] = _json_loads(out.get("key_strikes_json"), {})
    out["tilt"] = out.get("odme_tilt", "")
    return out
