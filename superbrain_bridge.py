from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from angel_connector import AngelConnector, load_angel_credentials
from data_store import BaseStore
from scan_service import run_odme_scan


def _norm(value: Any) -> str:
    return str(value or "").upper().strip()


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _safe_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    clean = df.copy().fillna("")
    return clean.to_dict(orient="records")


def _open_trade_records(store: BaseStore, instrument: str) -> List[Dict[str, Any]]:
    try:
        trades = store.list_superbrain_trades(instrument=instrument, open_only=True)
    except Exception:
        return []
    return _safe_records(trades)


def build_instrument_map(store: BaseStore) -> pd.DataFrame:
    """Build the public SuperBrain instrument list from live Sheet data only.

    Instruments come from TV_TEST_CURRENT. ODME is attached only by exact
    normalized instrument equality against odme_snapshots / instrument_settings.
    No alias table or invented mapping is used.
    """
    tv = store.load_tv_current()
    if tv is None or tv.empty or "instrument" not in tv.columns:
        return pd.DataFrame(
            columns=[
                "instrument", "tv_sources", "tv_tfs", "odme_snapshot_match",
                "odme_scan_enabled", "selected_expiry", "mode_hint",
            ]
        )

    tv = tv.copy().astype(str).fillna("")
    tv["_instrument"] = tv["instrument"].map(_norm)
    tv = tv[tv["_instrument"].ne("")]

    try:
        odme = store.load_odme_history(limit=100000)
    except Exception:
        odme = pd.DataFrame()
    odme_instruments = set()
    if odme is not None and not odme.empty and "instrument" in odme.columns:
        odme_instruments = set(odme["instrument"].astype(str).map(_norm))

    try:
        settings = store.list_instrument_settings(active_only=False)
    except Exception:
        settings = pd.DataFrame()
    settings_by_instrument: Dict[str, Dict[str, Any]] = {}
    if settings is not None and not settings.empty and "instrument" in settings.columns:
        for _, row in settings.iterrows():
            settings_by_instrument[_norm(row.get("instrument"))] = row.to_dict()

    rows: List[Dict[str, Any]] = []
    for instrument, group in tv.groupby("_instrument", sort=True):
        setting = settings_by_instrument.get(instrument, {})
        selected_expiry = str(setting.get("selected_expiry", "") or "").strip()
        scan_enabled = (
            _as_bool(setting.get("active", True))
            and _as_bool(setting.get("scan_enabled", False))
            and bool(selected_expiry)
        )
        sources = sorted({str(x).strip() for x in group.get("source", pd.Series(dtype=str)) if str(x).strip()})
        tfs = sorted({str(x).strip() for x in group.get("tf", pd.Series(dtype=str)) if str(x).strip()})
        odme_match = instrument in odme_instruments
        rows.append(
            {
                "instrument": instrument,
                "tv_sources": ", ".join(sources),
                "tv_tfs": ", ".join(tfs),
                "odme_snapshot_match": odme_match,
                "odme_scan_enabled": scan_enabled,
                "selected_expiry": selected_expiry,
                "mode_hint": "TV + ODME" if scan_enabled else "TV only",
            }
        )
    return pd.DataFrame(rows).sort_values("instrument").reset_index(drop=True)


def _persist_scan_memory(
    store: BaseStore,
    instrument: str,
    mode: str,
    mapping: Dict[str, Any],
    tv_rows: pd.DataFrame,
    latest_odme: Dict[str, Any],
    odme_live: bool,
    odme_error: str,
    open_trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist current SuperBrain evidence without creating any trade opinion.

    One STATE row exists per instrument. The row carries the current exact input
    snapshot plus the immediately previous input snapshot so the future reasoning
    engine can compare what changed across scans. TRADE rows are maintained
    independently by SuperBrain itself; broker positions are never consulted.
    """
    previous = store.load_superbrain_state(instrument) or {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    scan_id = f"SBSCAN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    tv_records = _safe_records(tv_rows)

    evidence = {
        "scan_id": scan_id,
        "scanned_at": now,
        "instrument": instrument,
        "mode": mode,
        "mapping": mapping,
        "tv_rows": tv_records,
        "latest_odme": latest_odme or {},
        "odme_live": bool(odme_live),
        "odme_error": odme_error or "",
        "open_trade_ids": [str(x.get("trade_id", "")) for x in open_trades if str(x.get("trade_id", "")).strip()],
    }

    fingerprint_payload = {
        "instrument": instrument,
        "mode": mode,
        "tv_rows": tv_records,
        "latest_odme": latest_odme or {},
    }
    fingerprint = hashlib.sha256(_json_dumps(fingerprint_payload).encode("utf-8")).hexdigest()

    state_row = {
        "status": "ACTIVE",
        "scan_id": scan_id,
        "previous_scan_id": str(previous.get("scan_id", "") or ""),
        "input_fingerprint": fingerprint,
        "mode": mode,
        # Intentionally blank until the actual reasoning engine is added.
        "action": "",
        "thesis": "",
        "market_state_json": _json_dumps(evidence),
        "previous_state_json": str(previous.get("market_state_json", "") or ""),
        "metadata_json": _json_dumps({"open_trade_count": len(open_trades)}),
        "updated_at": now,
    }
    prior_row = store.upsert_superbrain_state(instrument, state_row)

    return {
        "scan_id": scan_id,
        "previous_scan_id": str(previous.get("scan_id", "") or ""),
        "had_previous_state": bool(previous),
        "prior_state": prior_row or previous,
        "input_fingerprint": fingerprint,
    }


def prepare_superbrain_scan(store: BaseStore, instrument: str) -> Dict[str, Any]:
    """Phase-2 orchestration packet for Ask SuperBrain.

    This still does not invent trade decisions. It refreshes the exact market
    inputs, loads SuperBrain-owned open trade memory, and persists the current
    evidence so the next scan has a durable previous state to compare against.
    """
    instrument = _norm(instrument)
    mapping = build_instrument_map(store)
    matched = mapping[mapping["instrument"].eq(instrument)] if not mapping.empty else pd.DataFrame()
    if matched.empty:
        raise ValueError(f"{instrument} is not present in TV_TEST_CURRENT.")
    map_row = matched.iloc[0].to_dict()

    # Read any SuperBrain-owned exposure before refreshing inputs. Future
    # reasoning uses this to manage its own prior decisions, not broker positions.
    open_trades = _open_trade_records(store, instrument)

    tv_rows = store.load_tv_current(instrument)
    odme_outcome: Dict[str, Any] = {}
    odme_error = ""
    odme_live = False

    if bool(map_row.get("odme_scan_enabled")):
        expiry = str(map_row.get("selected_expiry", "") or "").strip()
        try:
            angel = AngelConnector(load_angel_credentials())
            angel.login_automatic()
            master = angel.load_instrument_master()
            odme_outcome = run_odme_scan(
                store,
                angel,
                master,
                instrument,
                expiry,
                save_only_if_changed=True,
            )
            odme_live = True
        except Exception as exc:
            # SuperBrain remains usable in TV-only mode if ODME refresh fails.
            odme_error = f"{type(exc).__name__}: {exc}"

    latest_odme = store.load_latest_odme_for_instrument(instrument)
    sources = []
    if tv_rows is not None and not tv_rows.empty and "source" in tv_rows.columns:
        sources = sorted({str(x).strip() for x in tv_rows["source"] if str(x).strip()})

    mode = "TV + ODME" if odme_live else "TV only"
    memory = _persist_scan_memory(
        store=store,
        instrument=instrument,
        mode=mode,
        mapping=map_row,
        tv_rows=tv_rows,
        latest_odme=latest_odme,
        odme_live=odme_live,
        odme_error=odme_error,
        open_trades=open_trades,
    )

    return {
        "instrument": instrument,
        "mode": mode,
        "tv_rows": tv_rows,
        "tv_sources": sources,
        "mapping": map_row,
        "odme_live": odme_live,
        "odme_outcome": odme_outcome,
        "latest_odme": latest_odme,
        "odme_error": odme_error,
        "open_trades": open_trades,
        "memory": memory,
    }
