from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from odme_config import GOOGLE_SHEET_DEFAULT_NAME, LOCAL_DATA_DIR, SUPPORTED_INSTRUMENTS
from runtime_config import get_bool, get_gcp_service_account_info, get_secret

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:  # pragma: no cover
    gspread = None
    Credentials = None


ODME_TAB = "odme_snapshots"
INSTRUMENT_TAB = "instrument_settings"

INSTRUMENT_COLUMNS = [
    "instrument", "active", "selected_expiry", "scan_enabled", "email_alert", "scan_times",
    "last_run_slot", "added_at", "updated_at"
]

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


def _column_letter(n: int) -> str:
    out = ""
    n = max(int(n), 1)
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _anchor_state_signature(row: Dict[str, Any]) -> str:
    """Anchor-independent fingerprint used to ignore duplicate closed-market rows."""
    base = {
        "spot": _to_float(row.get("spot")),
        "poc": _to_float(row.get("option_poc")),
        "val": _to_float(row.get("value_area_low")),
        "vah": _to_float(row.get("value_area_high")),
        "ce_wall": _to_float(row.get("ce_wall")),
        "pe_wall": _to_float(row.get("pe_wall")),
        "safer_ce": _to_float(row.get("safer_sell_ce")),
        "safer_pe": _to_float(row.get("safer_sell_pe")),
        "hvn": _json_loads(row.get("hvn"), []),
        "lvn": _json_loads(row.get("lvn"), []),
    }
    keys = _json_loads(row.get("key_strikes_json"), {})
    selected = {}
    for level in [base["poc"], base["val"], base["vah"], base["ce_wall"], base["pe_wall"], base["safer_ce"], base["safer_pe"]]:
        if not level:
            continue
        k = str(int(round(float(level))))
        if isinstance(keys, dict) and k in keys:
            selected[k] = keys.get(k)
    base["key_levels"] = selected
    return _json_dumps(base)


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

    # Collapse duplicate closed-market observations across calendar dates. This
    # keeps Friday as Monday's anchor when Saturday/Sunday (or a public holiday)
    # merely repeated the same market state.
    chosen: Dict[str, Any] = {}
    last_sig = None
    for _, r in prior.iterrows():
        clean = r.drop(labels=[c for c in ["_ts_utc", "_local_date"] if c in r.index]).to_dict()
        sig = _anchor_state_signature(clean)
        if sig != last_sig:
            chosen = clean
            last_sig = sig
    return chosen


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _expiry_date(value: Any) -> Optional[datetime.date]:
    s = str(value or "").strip().upper()
    if not s:
        return None
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    parsed = pd.to_datetime(s, errors="coerce")
    if pd.isna(parsed):
        return None
    try:
        return parsed.date()
    except Exception:
        return None


def _seed_default_settings(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        df = pd.DataFrame(columns=INSTRUMENT_COLUMNS)
    for col in INSTRUMENT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    existing = set(df["instrument"].astype(str).str.upper().str.strip()) if not df.empty else set()
    now = utc_now_iso()
    rows = []
    for instrument in SUPPORTED_INSTRUMENTS:
        if instrument not in existing:
            rows.append({
                "instrument": instrument,
                "active": "TRUE",
                "selected_expiry": "",
                "scan_enabled": "FALSE",
                "email_alert": "FALSE",
                "scan_times": "",
                "last_run_slot": "",
                "added_at": now,
                "updated_at": now,
            })
    if rows:
        df = pd.concat([df[INSTRUMENT_COLUMNS], pd.DataFrame(rows)], ignore_index=True)
    return df[INSTRUMENT_COLUMNS].astype(str).fillna("")



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

    def list_instrument_settings(self, active_only: bool = False) -> pd.DataFrame:
        raise NotImplementedError

    def upsert_instrument_setting(
        self,
        instrument: str,
        active: Optional[bool] = None,
        selected_expiry: Optional[str] = None,
        scan_enabled: Optional[bool] = None,
        email_alert: Optional[bool] = None,
        scan_times: Optional[str] = None,
        last_run_slot: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    def deactivate_instrument(self, instrument: str) -> None:
        self.upsert_instrument_setting(instrument, active=False, scan_enabled=False, email_alert=False)

    def cleanup_expired_data(self, tz_name: str = "Asia/Kolkata") -> Dict[str, int]:
        raise NotImplementedError

    # Backward-compatible no-op method name from old app.
    def upsert_initialized(self, row: Dict[str, Any]) -> None:
        return None


class LocalStore(BaseStore):
    def __init__(self, data_dir: str = LOCAL_DATA_DIR):
        self.root = Path(data_dir)
        self.path = self.root / "odme_snapshots.csv"
        self.settings_path = self.root / "instrument_settings.csv"

    def ensure(self) -> None:
        self.root.mkdir(exist_ok=True)
        if not self.path.exists():
            pd.DataFrame(columns=ODME_COLUMNS).to_csv(self.path, index=False)
        if self.settings_path.exists():
            settings = pd.read_csv(self.settings_path, dtype=str).fillna("")
        else:
            settings = pd.DataFrame(columns=INSTRUMENT_COLUMNS)
        seeded = _seed_default_settings(settings)
        seeded.to_csv(self.settings_path, index=False)

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

    def list_instrument_settings(self, active_only: bool = False) -> pd.DataFrame:
        self.ensure()
        df = pd.read_csv(self.settings_path, dtype=str).fillna("")
        for col in INSTRUMENT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[INSTRUMENT_COLUMNS]
        if active_only:
            df = df[df["active"].apply(_as_bool)]
        return df.reset_index(drop=True)

    def upsert_instrument_setting(
        self,
        instrument: str,
        active: Optional[bool] = None,
        selected_expiry: Optional[str] = None,
        scan_enabled: Optional[bool] = None,
        email_alert: Optional[bool] = None,
        scan_times: Optional[str] = None,
        last_run_slot: Optional[str] = None,
    ) -> None:
        self.ensure()
        instrument = str(instrument).upper().strip()
        if not instrument:
            raise ValueError("Instrument cannot be blank.")
        df = pd.read_csv(self.settings_path, dtype=str).fillna("")
        for col in INSTRUMENT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        mask = df["instrument"].astype(str).str.upper().eq(instrument)
        now = utc_now_iso()
        if mask.any():
            idx = df.index[mask][-1]
            if active is not None:
                df.at[idx, "active"] = "TRUE" if active else "FALSE"
            if selected_expiry is not None:
                df.at[idx, "selected_expiry"] = str(selected_expiry)
            if scan_enabled is not None:
                df.at[idx, "scan_enabled"] = "TRUE" if scan_enabled else "FALSE"
            if email_alert is not None:
                df.at[idx, "email_alert"] = "TRUE" if email_alert else "FALSE"
            if scan_times is not None:
                df.at[idx, "scan_times"] = str(scan_times)
            if last_run_slot is not None:
                df.at[idx, "last_run_slot"] = str(last_run_slot)
            df.at[idx, "updated_at"] = now
            if not str(df.at[idx, "added_at"]).strip():
                df.at[idx, "added_at"] = now
        else:
            row = {
                "instrument": instrument,
                "active": "TRUE" if active is not False else "FALSE",
                "selected_expiry": str(selected_expiry or ""),
                "scan_enabled": "TRUE" if scan_enabled else "FALSE",
                "email_alert": "TRUE" if email_alert else "FALSE",
                "scan_times": str(scan_times or ""),
                "last_run_slot": str(last_run_slot or ""),
                "added_at": now,
                "updated_at": now,
            }
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df[INSTRUMENT_COLUMNS].to_csv(self.settings_path, index=False)

    def cleanup_expired_data(self, tz_name: str = "Asia/Kolkata") -> Dict[str, int]:
        self.ensure()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Kolkata")
        today = datetime.now(tz).date()

        snapshots = pd.read_csv(self.path, dtype=str).fillna("")
        before = len(snapshots)
        if not snapshots.empty and "expiry" in snapshots.columns:
            keep = snapshots["expiry"].apply(lambda x: (_expiry_date(x) is None) or (_expiry_date(x) >= today))
            snapshots = snapshots[keep].copy()
        deleted = before - len(snapshots)
        snapshots.to_csv(self.path, index=False)

        settings = pd.read_csv(self.settings_path, dtype=str).fillna("")
        cleared = 0
        for idx, row in settings.iterrows():
            exp = _expiry_date(row.get("selected_expiry", ""))
            if exp is not None and exp < today:
                settings.at[idx, "selected_expiry"] = ""
                settings.at[idx, "scan_enabled"] = "FALSE"
                settings.at[idx, "email_alert"] = "FALSE"
                settings.at[idx, "last_run_slot"] = ""
                settings.at[idx, "updated_at"] = utc_now_iso()
                cleared += 1
        settings[INSTRUMENT_COLUMNS].to_csv(self.settings_path, index=False)
        return {"deleted_snapshots": deleted, "cleared_scan_settings": cleared}


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
        return bool(get_gcp_service_account_info())

    @staticmethod
    def enabled_from_secrets() -> bool:
        return get_bool("USE_GOOGLE_SHEETS", False)

    @staticmethod
    def sheet_name_from_secrets() -> str:
        return str(get_secret("GOOGLE_SHEET_NAME", GOOGLE_SHEET_DEFAULT_NAME))

    @staticmethod
    def _client_from_streamlit_secrets():
        sa_info = get_gcp_service_account_info()
        if not sa_info:
            raise RuntimeError("Google service-account credentials are not configured.")
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

    def _worksheet(self, title: str, columns: Optional[List[str]] = None):
        if title in self._ws_cache:
            return self._ws_cache[title]
        try:
            ws = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            cols = columns or ODME_COLUMNS
            ws = self.sheet.add_worksheet(title=title, rows=2000, cols=max(len(cols), 30))
            ws.update("A1", [cols])
        self._ws_cache[title] = ws
        return ws

    def ensure(self) -> None:
        if self._ensured:
            return
        ws = self._worksheet(ODME_TAB, ODME_COLUMNS)
        header = ws.row_values(1)
        if header != ODME_COLUMNS:
            # If old heavy tabs exist, they are left untouched. Only this compact tab is managed.
            ws.clear()
            ws.update("A1", [ODME_COLUMNS])

        sws = self._worksheet(INSTRUMENT_TAB, INSTRUMENT_COLUMNS)
        sheader = sws.row_values(1)
        # Preserve existing instrument settings when the schema gains a new column
        # (for example scan_times). Never wipe the user's saved dropdown/expiry setup.
        records = sws.get_all_records() if sheader else []
        settings = pd.DataFrame(records)
        seeded = _seed_default_settings(settings)
        needs_rewrite = sheader != INSTRUMENT_COLUMNS or len(seeded) != len(settings)
        if needs_rewrite:
            sws.clear()
            sws.update("A1", [INSTRUMENT_COLUMNS])
            if not seeded.empty:
                sws.update("A2", seeded[INSTRUMENT_COLUMNS].astype(str).fillna("").values.tolist())
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


    def list_instrument_settings(self, active_only: bool = False) -> pd.DataFrame:
        self.ensure()
        ws = self._worksheet(INSTRUMENT_TAB, INSTRUMENT_COLUMNS)
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        if df.empty:
            df = pd.DataFrame(columns=INSTRUMENT_COLUMNS)
        for col in INSTRUMENT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[INSTRUMENT_COLUMNS].astype(str).fillna("")
        if active_only:
            df = df[df["active"].apply(_as_bool)]
        return df.reset_index(drop=True)

    def upsert_instrument_setting(
        self,
        instrument: str,
        active: Optional[bool] = None,
        selected_expiry: Optional[str] = None,
        scan_enabled: Optional[bool] = None,
        email_alert: Optional[bool] = None,
        scan_times: Optional[str] = None,
        last_run_slot: Optional[str] = None,
    ) -> None:
        self.ensure()
        instrument = str(instrument).upper().strip()
        if not instrument:
            raise ValueError("Instrument cannot be blank.")
        ws = self._worksheet(INSTRUMENT_TAB, INSTRUMENT_COLUMNS)
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        if df.empty:
            df = pd.DataFrame(columns=INSTRUMENT_COLUMNS)
        for col in INSTRUMENT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        mask = df["instrument"].astype(str).str.upper().eq(instrument) if not df.empty else pd.Series(dtype=bool)
        now = utc_now_iso()
        if not df.empty and mask.any():
            idx = df.index[mask][-1]
            row = {c: str(df.at[idx, c]) for c in INSTRUMENT_COLUMNS}
            row["instrument"] = instrument
            if active is not None:
                row["active"] = "TRUE" if active else "FALSE"
            if selected_expiry is not None:
                row["selected_expiry"] = str(selected_expiry)
            if scan_enabled is not None:
                row["scan_enabled"] = "TRUE" if scan_enabled else "FALSE"
            if email_alert is not None:
                row["email_alert"] = "TRUE" if email_alert else "FALSE"
            if scan_times is not None:
                row["scan_times"] = str(scan_times)
            if last_run_slot is not None:
                row["last_run_slot"] = str(last_run_slot)
            row["updated_at"] = now
            if not row.get("added_at", "").strip():
                row["added_at"] = now
            sheet_row = int(idx) + 2
            # Update the full current schema width (not a hard-coded A:H range).
            end_col = _column_letter(len(INSTRUMENT_COLUMNS))
            ws.update(f"A{sheet_row}:{end_col}{sheet_row}", [[row.get(c, "") for c in INSTRUMENT_COLUMNS]])
        else:
            row = {
                "instrument": instrument,
                "active": "TRUE" if active is not False else "FALSE",
                "selected_expiry": str(selected_expiry or ""),
                "scan_enabled": "TRUE" if scan_enabled else "FALSE",
                "email_alert": "TRUE" if email_alert else "FALSE",
                "scan_times": str(scan_times or ""),
                "last_run_slot": str(last_run_slot or ""),
                "added_at": now,
                "updated_at": now,
            }
            ws.append_row([row.get(c, "") for c in INSTRUMENT_COLUMNS], value_input_option="USER_ENTERED")

    def cleanup_expired_data(self, tz_name: str = "Asia/Kolkata") -> Dict[str, int]:
        self.ensure()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Kolkata")
        today = datetime.now(tz).date()

        ws = self._worksheet(ODME_TAB, ODME_COLUMNS)
        records = ws.get_all_records()
        snapshots = pd.DataFrame(records)
        if snapshots.empty:
            snapshots = pd.DataFrame(columns=ODME_COLUMNS)
        for col in ODME_COLUMNS:
            if col not in snapshots.columns:
                snapshots[col] = ""
        before = len(snapshots)
        if not snapshots.empty:
            keep = snapshots["expiry"].apply(lambda x: (_expiry_date(x) is None) or (_expiry_date(x) >= today))
            snapshots = snapshots[keep].copy()
        deleted = before - len(snapshots)
        if deleted:
            ws.clear()
            ws.update("A1", [ODME_COLUMNS])
            if not snapshots.empty:
                ws.update("A2", snapshots[ODME_COLUMNS].astype(str).fillna("").values.tolist())

        sws = self._worksheet(INSTRUMENT_TAB, INSTRUMENT_COLUMNS)
        srecords = sws.get_all_records()
        settings = pd.DataFrame(srecords)
        if settings.empty:
            settings = pd.DataFrame(columns=INSTRUMENT_COLUMNS)
        for col in INSTRUMENT_COLUMNS:
            if col not in settings.columns:
                settings[col] = ""
        cleared = 0
        for idx, row in settings.iterrows():
            exp = _expiry_date(row.get("selected_expiry", ""))
            if exp is not None and exp < today:
                settings.at[idx, "selected_expiry"] = ""
                settings.at[idx, "scan_enabled"] = "FALSE"
                settings.at[idx, "email_alert"] = "FALSE"
                settings.at[idx, "last_run_slot"] = ""
                settings.at[idx, "updated_at"] = utc_now_iso()
                cleared += 1
        if cleared:
            sws.clear()
            sws.update("A1", [INSTRUMENT_COLUMNS])
            if not settings.empty:
                sws.update("A2", settings[INSTRUMENT_COLUMNS].astype(str).fillna("").values.tolist())
        return {"deleted_snapshots": deleted, "cleared_scan_settings": cleared}


def create_store() -> BaseStore:
    """Create a store for Streamlit or an unattended worker."""
    if GoogleSheetStore.enabled_from_secrets() and GoogleSheetStore.available_from_secrets():
        store: BaseStore = GoogleSheetStore(GoogleSheetStore.sheet_name_from_secrets())
    else:
        store = LocalStore()
    store.ensure()
    return store


@st.cache_resource(show_spinner=False)
def get_store() -> BaseStore:
    return create_store()


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
