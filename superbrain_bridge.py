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

SUPERBRAIN_BRIDGE_VERSION = "SB2.2_TV_ONLY_MEMORY_FIX"


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


def _latest_tv_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Return one decision-relevant current row per TradingView source.

    TV_TEST_CURRENT intentionally retains rows from old test timeframes. Persistent
    SuperBrain memory should not duplicate the whole 297-column sheet. We keep the
    freshest row for each source and only the fields needed to compare market state
    across scans. Detailed live truth remains in TV_TEST_CURRENT and is reread on
    every scan.
    """
    if df is None or df.empty:
        return []
    work = df.copy().fillna("")
    if "source" not in work.columns:
        return []

    common = ["updated_at", "source", "tf", "bar_time", "close", "schema_version"]
    source_fields = {
        "AURORA": [
            "aurora_state", "aurora_prev", "aurora_change", "eta_up_level",
            "eta_up_regime", "eta_up_locked_remaining_bars", "eta_down_level",
            "eta_down_regime", "eta_down_locked_remaining_bars", "eta_current_atr",
        ],
        "STRUCTURE": [
            "structure_trend", "struct_high", "struct_low", "last_clean_price",
            "last_clean_time", "fork_valid", "fork_slope", "fork_slope_flip",
            "fork_position", "fork_reclaimed_2sd", "fork_active_kind", "fork_median",
            "fork_upper_1sd", "fork_upper_2sd", "fork_lower_1sd", "fork_lower_2sd",
        ],
        "LIQUIDITY": [
            "liq_buy_level", "liq_buy_count", "liq_buy_distance", "liq_sell_level",
            "liq_sell_count", "liq_sell_distance", "liq_pending_count",
            "liq_history_count", "liq_last_event", "liq_event_level",
            "liq_event_count", "liq_event_reclaimed", "liq_event1_type",
            "liq_event1_side", "liq_event1_level", "liq_event1_confirm_time",
            "liq_event2_type", "liq_event2_side", "liq_event2_level",
            "liq_event2_confirm_time", "liq_event3_type", "liq_event3_side",
            "liq_event3_level", "liq_event3_confirm_time",
        ],
        "EDGE": [
            "macro", "macro_tf", "macro_score", "battlefield_paired",
            "defender_tf", "defender_side", "defender_low", "defender_high",
            "defender_state", "challenger_tf", "challenger_side", "challenger_low",
            "challenger_high", "challenger_state", "exec_tf", "exec_of",
            "exec_of_score", "exec_move_quality", "fp_valid", "fp_side", "fp_low",
            "fp_high", "fp_strength", "fp_strength_score", "fp_status", "fp_hold",
            "fp_interacting", "fp_context_state", "fp_event_type", "fp_event_score",
            "fp_distance_atr", "edge_tracking_state", "edge_required_exec_tf",
            "edge_needs_alert_change", "edge_tracking_reason", "raw_json",
        ],
    }

    out: List[Dict[str, Any]] = []
    for source, group in work.groupby(work["source"].astype(str).str.upper().str.strip(), sort=True):
        group = group.copy()
        # Prefer ACTIVE EDGE authority when present, then the greatest bar_time.
        if source == "EDGE" and "edge_tracking_state" in group.columns:
            active = group[group["edge_tracking_state"].astype(str).str.upper().eq("ACTIVE")]
            if not active.empty:
                group = active
        if "bar_time" in group.columns:
            order = pd.to_numeric(group["bar_time"], errors="coerce").fillna(-1)
            row = group.loc[order.idxmax()]
        else:
            row = group.iloc[-1]

        keys = common + source_fields.get(source, [])
        compact: Dict[str, Any] = {}
        for key in keys:
            if key not in row.index:
                continue
            value = row.get(key, "")
            if value is None or str(value) == "":
                continue
            if key == "raw_json":
                try:
                    value = json.loads(str(value))
                except Exception:
                    # Keep a bounded raw fallback; malformed raw JSON should not
                    # make persistence fail.
                    value = str(value)[:12000]
            compact[key] = value
        out.append(compact)
    return out


def _compact_odme(row: Dict[str, Any]) -> Dict[str, Any]:
    """Keep prior ODME anchors needed for change comparison, not the full chain blob."""
    if not row:
        return {}
    fields = [
        "snapshot_id", "ts", "instrument", "exchange", "expiry", "spot",
        "option_poc", "value_area_low", "value_area_high", "ce_wall", "pe_wall",
        "ce_wall_shift", "pe_wall_shift", "poc_shift", "range_shift",
        "bullish_score", "bearish_score", "range_score", "expansion_score",
        "odme_tilt", "safer_sell_ce", "active_ce_wall", "safer_sell_pe",
        "active_pe_wall", "usable_oi_count", "source",
    ]
    return {k: row.get(k) for k in fields if row.get(k) not in (None, "")}


def _load_json_object(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


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
    # Persistent memory stores a compact comparison state only. Detailed live
    # truth remains in TV_TEST_CURRENT / odme_snapshots and is reread each scan.
    tv_compact = _latest_tv_rows(tv_rows)
    odme_compact = _compact_odme(latest_odme or {})
    mapping_compact = {
        "instrument": mapping.get("instrument", instrument),
        "odme_scan_enabled": bool(mapping.get("odme_scan_enabled")),
        "selected_expiry": mapping.get("selected_expiry", ""),
        "mode_hint": mapping.get("mode_hint", ""),
    }

    evidence = {
        "scan_id": scan_id,
        "scanned_at": now,
        "instrument": instrument,
        "mode": mode,
        "mapping": mapping_compact,
        "tv": tv_compact,
        "odme": odme_compact,
        "odme_live": bool(odme_live),
        "odme_error": odme_error or "",
        "open_trade_ids": [str(x.get("trade_id", "")) for x in open_trades if str(x.get("trade_id", "")).strip()],
    }

    # Fingerprint the exact inputs in memory, but do not write that huge payload
    # into a Sheet cell. This preserves accurate change detection without storage
    # bloat.
    fingerprint_payload = {
        "instrument": instrument,
        "mode": mode,
        "tv_rows": _safe_records(tv_rows),
        "latest_odme": latest_odme or {},
    }
    fingerprint = hashlib.sha256(_json_dumps(fingerprint_payload).encode("utf-8")).hexdigest()

    previous_compact = _load_json_object(previous.get("market_state_json", ""))
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
        "previous_state_json": _json_dumps(previous_compact) if previous_compact else "",
        "metadata_json": _json_dumps({
            "open_trade_count": len(open_trades),
            "memory_schema": "SBMEM2_COMPACT",
            "bridge_version": SUPERBRAIN_BRIDGE_VERSION,
            "tv_source_count": len(tv_compact),
        }),
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
    """Phase-2 orchestration packet for Ask SuperBrain with explicit stage diagnostics.

    No trade decisions are invented here. BTCUSD and any other instrument without
    ODME remain on the TV-only path. Each failure reports the exact orchestration
    stage so Sheet write issues cannot be misdiagnosed as ODME problems.
    """
    instrument = _norm(instrument)
    stage = "START"
    try:
        stage = "BUILD_INSTRUMENT_MAP"
        mapping = build_instrument_map(store)
        matched = mapping[mapping["instrument"].eq(instrument)] if not mapping.empty else pd.DataFrame()
        if matched.empty:
            raise ValueError(f"{instrument} is not present in TV_TEST_CURRENT.")
        map_row = matched.iloc[0].to_dict()

        stage = "LOAD_OPEN_SUPERBRAIN_TRADES"
        open_trades = _open_trade_records(store, instrument)

        stage = "LOAD_TV_CURRENT"
        tv_rows = store.load_tv_current(instrument)
        odme_outcome: Dict[str, Any] = {}
        odme_error = ""
        odme_live = False

        # Exact-match ODME is optional. TV-only instruments never call Angel/ODME.
        if bool(map_row.get("odme_scan_enabled")):
            stage = "OPTIONAL_ODME_REFRESH"
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
                odme_error = f"{type(exc).__name__}: {exc}"

        stage = "LOAD_MATCHED_ODME_HISTORY"
        latest_odme = store.load_latest_odme_for_instrument(instrument)
        sources = []
        if tv_rows is not None and not tv_rows.empty and "source" in tv_rows.columns:
            sources = sorted({str(x).strip() for x in tv_rows["source"] if str(x).strip()})

        mode = "TV + ODME" if odme_live else "TV only"
        stage = "WRITE_SUPERBRAIN_MEMORY"
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
            "superbrain_build": SUPERBRAIN_BRIDGE_VERSION,
        }
    except Exception as exc:
        raise RuntimeError(
            f"{SUPERBRAIN_BRIDGE_VERSION} [{stage}] {type(exc).__name__}: {exc}"
        ) from exc

