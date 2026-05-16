from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from odme_config import (
    CHAIN_COLUMNS,
    GOOGLE_SHEET_DEFAULT_NAME,
    INITIALIZED_COLUMNS,
    LOCAL_DATA_DIR,
    SHEET_TABS,
    SNAPSHOT_COLUMNS,
)

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:  # pragma: no cover
    gspread = None
    Credentials = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_key(instrument: str, expiry: str) -> str:
    safe_expiry = str(expiry).replace(" ", "_").replace("/", "-")
    return f"{instrument.upper()}__{safe_expiry}"


def make_snapshot_id(key: str) -> str:
    return f"{key}__{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}__{uuid.uuid4().hex[:8]}"


class BaseStore:
    def ensure(self) -> None:
        raise NotImplementedError

    def list_initialized(self) -> pd.DataFrame:
        raise NotImplementedError

    def is_initialized(self, key: str) -> bool:
        df = self.list_initialized()
        return (not df.empty) and key in set(df.get("key", pd.Series(dtype=str)).astype(str))

    def upsert_initialized(self, row: Dict[str, Any]) -> None:
        raise NotImplementedError

    def append_snapshot(self, snapshot: Dict[str, Any], chain_df: pd.DataFrame) -> None:
        raise NotImplementedError

    def load_chain_memory(self, key: str) -> pd.DataFrame:
        raise NotImplementedError

    def load_snapshots(self, key: Optional[str] = None) -> pd.DataFrame:
        raise NotImplementedError


class LocalStore(BaseStore):
    def __init__(self, data_dir: str = LOCAL_DATA_DIR):
        self.root = Path(data_dir)
        self.init_path = self.root / "initialized_expiries.csv"
        self.snapshot_path = self.root / "snapshots.csv"
        self.chain_dir = self.root / "chains"

    def ensure(self) -> None:
        self.root.mkdir(exist_ok=True)
        self.chain_dir.mkdir(exist_ok=True)
        if not self.init_path.exists():
            pd.DataFrame(columns=INITIALIZED_COLUMNS).to_csv(self.init_path, index=False)
        if not self.snapshot_path.exists():
            pd.DataFrame(columns=SNAPSHOT_COLUMNS).to_csv(self.snapshot_path, index=False)

    def list_initialized(self) -> pd.DataFrame:
        self.ensure()
        return pd.read_csv(self.init_path, dtype=str).fillna("")

    def upsert_initialized(self, row: Dict[str, Any]) -> None:
        self.ensure()
        df = self.list_initialized()
        row = {c: row.get(c, "") for c in INITIALIZED_COLUMNS}
        if not df.empty and row["key"] in set(df["key"].astype(str)):
            mask = df["key"].astype(str).eq(row["key"])
            # Preserve original initialization time when updating last fetch.
            if not row.get("initialized_at"):
                row["initialized_at"] = df.loc[mask, "initialized_at"].iloc[0]
            df.loc[mask, INITIALIZED_COLUMNS] = [row[c] for c in INITIALIZED_COLUMNS]
        else:
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df.to_csv(self.init_path, index=False)

    def append_snapshot(self, snapshot: Dict[str, Any], chain_df: pd.DataFrame) -> None:
        self.ensure()
        snap = {c: snapshot.get(c, "") for c in SNAPSHOT_COLUMNS}
        snaps = pd.read_csv(self.snapshot_path, dtype=str).fillna("")
        snaps = pd.concat([snaps, pd.DataFrame([snap])], ignore_index=True)
        snaps.to_csv(self.snapshot_path, index=False)
        key_path = self.chain_dir / f"{snapshot['key']}.csv"
        chain = _normalize_chain_for_store(chain_df, snapshot)
        if key_path.exists():
            old = pd.read_csv(key_path)
            chain = pd.concat([old, chain], ignore_index=True)
        chain.to_csv(key_path, index=False)

    def load_chain_memory(self, key: str) -> pd.DataFrame:
        self.ensure()
        path = self.chain_dir / f"{key}.csv"
        if not path.exists():
            return pd.DataFrame(columns=CHAIN_COLUMNS)
        return pd.read_csv(path)

    def load_snapshots(self, key: Optional[str] = None) -> pd.DataFrame:
        self.ensure()
        df = pd.read_csv(self.snapshot_path, dtype=str).fillna("")
        if key:
            df = df[df["key"].astype(str).eq(key)]
        return df


class GoogleSheetStore(BaseStore):
    def __init__(self, sheet_name: str = GOOGLE_SHEET_DEFAULT_NAME):
        if gspread is None or Credentials is None:
            raise RuntimeError("Google Sheets packages missing. Run: pip install -r requirements.txt")
        self.sheet_name = sheet_name
        self.gc = self._client_from_streamlit_secrets()
        self.sheet = self._open_or_create_sheet(sheet_name)

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

    def ensure(self) -> None:
        self._ensure_tab(SHEET_TABS["initialized"], INITIALIZED_COLUMNS)
        self._ensure_tab(SHEET_TABS["snapshots"], SNAPSHOT_COLUMNS)
        self._ensure_tab(SHEET_TABS["chain"], CHAIN_COLUMNS)

    def _ensure_tab(self, title: str, columns: List[str]) -> None:
        try:
            ws = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=1000, cols=max(len(columns), 20))
        existing = ws.row_values(1)
        if existing != columns:
            ws.clear()
            ws.update("A1", [columns])

    def _records(self, tab: str) -> pd.DataFrame:
        self.ensure()
        ws = self.sheet.worksheet(tab)
        records = ws.get_all_records()
        return pd.DataFrame(records)

    def list_initialized(self) -> pd.DataFrame:
        df = self._records(SHEET_TABS["initialized"])
        if df.empty:
            return pd.DataFrame(columns=INITIALIZED_COLUMNS)
        return df.astype(str).fillna("")

    def upsert_initialized(self, row: Dict[str, Any]) -> None:
        self.ensure()
        ws = self.sheet.worksheet(SHEET_TABS["initialized"])
        df = self.list_initialized()
        row = {c: row.get(c, "") for c in INITIALIZED_COLUMNS}
        if df.empty or row["key"] not in set(df["key"].astype(str)):
            ws.append_row([row[c] for c in INITIALIZED_COLUMNS], value_input_option="USER_ENTERED")
            return
        mask = df["key"].astype(str).eq(row["key"])
        if not row.get("initialized_at"):
            row["initialized_at"] = df.loc[mask, "initialized_at"].iloc[0]
        idx = df.index[mask][0] + 2
        ws.update(f"A{idx}", [[row[c] for c in INITIALIZED_COLUMNS]])

    def append_snapshot(self, snapshot: Dict[str, Any], chain_df: pd.DataFrame) -> None:
        self.ensure()
        snap_ws = self.sheet.worksheet(SHEET_TABS["snapshots"])
        chain_ws = self.sheet.worksheet(SHEET_TABS["chain"])
        snap = {c: snapshot.get(c, "") for c in SNAPSHOT_COLUMNS}
        snap_ws.append_row([snap[c] for c in SNAPSHOT_COLUMNS], value_input_option="USER_ENTERED")
        chain = _normalize_chain_for_store(chain_df, snapshot)
        values = chain[CHAIN_COLUMNS].astype(object).where(pd.notna(chain[CHAIN_COLUMNS]), "").values.tolist()
        if values:
            chain_ws.append_rows(values, value_input_option="USER_ENTERED")

    def load_chain_memory(self, key: str) -> pd.DataFrame:
        df = self._records(SHEET_TABS["chain"])
        if df.empty:
            return pd.DataFrame(columns=CHAIN_COLUMNS)
        df = df[df["key"].astype(str).eq(str(key))].copy()
        return df

    def load_snapshots(self, key: Optional[str] = None) -> pd.DataFrame:
        df = self._records(SHEET_TABS["snapshots"])
        if df.empty:
            return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
        if key:
            df = df[df["key"].astype(str).eq(str(key))].copy()
        return df


def get_store() -> BaseStore:
    if GoogleSheetStore.enabled_from_secrets() and GoogleSheetStore.available_from_secrets():
        store = GoogleSheetStore(GoogleSheetStore.sheet_name_from_secrets())
    else:
        store = LocalStore()
    store.ensure()
    return store


def _normalize_chain_for_store(chain_df: pd.DataFrame, snapshot: Dict[str, Any]) -> pd.DataFrame:
    df = chain_df.copy()
    df["snapshot_id"] = snapshot["snapshot_id"]
    df["key"] = snapshot["key"]
    df["ts"] = snapshot["ts"]
    df["instrument"] = snapshot["instrument"]
    df["exchange"] = snapshot["exchange"]
    df["expiry"] = snapshot["expiry"]
    for col in CHAIN_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    # Avoid writing complex nested depth objects into Sheets.
    return df[CHAIN_COLUMNS].copy()
