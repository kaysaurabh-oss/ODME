"""Local storage layer for ODME Angel snapshots and expiry registry."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from odme_config import DATA_DIR, MEMORY_DIR, INIT_REGISTRY_PATH


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_key(symbol: str, expiry: str) -> str:
    raw = f"{symbol}_{expiry}".upper()
    return re.sub(r"[^A-Z0-9_\-]+", "_", raw)


def memory_path(symbol: str, expiry: str) -> Path:
    ensure_dirs()
    return MEMORY_DIR / f"{safe_key(symbol, expiry)}.parquet"


def load_registry() -> Dict[str, Any]:
    ensure_dirs()
    if not INIT_REGISTRY_PATH.exists():
        return {"items": {}}
    try:
        return json.loads(INIT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}


def save_registry(registry: Dict[str, Any]) -> None:
    ensure_dirs()
    INIT_REGISTRY_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def is_initialized(symbol: str, expiry: str) -> bool:
    key = safe_key(symbol, expiry)
    return key in load_registry().get("items", {}) and memory_path(symbol, expiry).exists()


def register_initialized(symbol: str, expiry: str, meta: Optional[Dict[str, Any]] = None) -> None:
    registry = load_registry()
    registry.setdefault("items", {})[safe_key(symbol, expiry)] = {
        "symbol": symbol.upper(),
        "expiry": expiry,
        "initialized_at_utc": utc_now_iso(),
        **(meta or {}),
    }
    save_registry(registry)


def list_initialized() -> List[Dict[str, Any]]:
    items = list(load_registry().get("items", {}).values())
    return sorted(items, key=lambda x: (x.get("symbol", ""), x.get("expiry", "")))


def append_snapshot(symbol: str, expiry: str, chain: pd.DataFrame, spot: float, source: str = "angel") -> int:
    """Append a full-chain snapshot. Returns rows appended."""
    ensure_dirs()
    if chain is None or chain.empty:
        return 0
    snapshot_id = utc_now_iso()
    df = chain.copy()
    df["symbol"] = symbol.upper()
    df["expiry"] = str(expiry)
    df["snapshot_ts"] = snapshot_id
    df["spot"] = float(spot) if spot is not None else float("nan")
    df["source"] = source

    p = memory_path(symbol, expiry)
    if p.exists():
        old = pd.read_parquet(p)
        combined = pd.concat([old, df], ignore_index=True)
        # prevent accidental exact duplicate snapshot rows if user double-clicks refresh
        combined = combined.drop_duplicates(subset=["snapshot_ts", "tradingsymbol", "strike", "option_type"], keep="last")
    else:
        combined = df
    combined.to_parquet(p, index=False)
    return len(df)


def load_memory(symbol: str, expiry: str) -> pd.DataFrame:
    p = memory_path(symbol, expiry)
    if not p.exists():
        return pd.DataFrame()
    return pd.read_parquet(p)


def last_snapshot_time(symbol: str, expiry: str) -> Optional[pd.Timestamp]:
    df = load_memory(symbol, expiry)
    if df.empty or "snapshot_ts" not in df.columns:
        return None
    return pd.to_datetime(df["snapshot_ts"], errors="coerce", utc=True).max()


def should_refresh(symbol: str, expiry: str, refresh_minutes: int) -> bool:
    ts = last_snapshot_time(symbol, expiry)
    if ts is None or pd.isna(ts):
        return True
    age_min = (pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 60
    return age_min >= refresh_minutes


def reset_memory(symbol: str, expiry: str) -> None:
    p = memory_path(symbol, expiry)
    if p.exists():
        p.unlink()
    registry = load_registry()
    registry.get("items", {}).pop(safe_key(symbol, expiry), None)
    save_registry(registry)
