from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pandas as pd

from angel_connector import AngelConnector, AngelDataError
from data_store import (
    BaseStore,
    make_key,
    make_snapshot_id,
    make_summary_row,
    parse_previous_summary,
    utc_now_iso,
)
from odme_engine import analyze_odme


_STATE_FLOAT_FIELDS = [
    # Only anchor-independent market state belongs here. Scores/tilt/commentary are
    # intentionally excluded because they are derived versus the prior anchor and
    # can change even when the underlying closed-market data is identical.
    "spot", "option_poc", "value_area_low", "value_area_high",
    "ce_wall", "pe_wall", "safer_sell_ce", "safer_sell_pe",
]
_STATE_JSON_FIELDS = ["hvn", "lvn"]
_STATE_TEXT_FIELDS: list[str] = []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _canonical_json(value: Any) -> str:
    if value in [None, ""]:
        return ""
    try:
        obj = json.loads(value) if isinstance(value, str) else value
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(value).strip()


def market_state_changed(candidate_row: Dict[str, Any], latest_row: Dict[str, Any]) -> bool:
    """True only when the saved ODME market state materially changed.

    This deliberately ignores timestamp/commentary/anchor wording. On weekends,
    public holidays, or after market close, identical stale market data therefore
    does not create a new snapshot and cannot replace the prior trading-session anchor.
    """
    if not latest_row:
        return True

    for field in _STATE_FLOAT_FIELDS:
        a = _to_float(candidate_row.get(field))
        b = _to_float(latest_row.get(field))
        tol = max(1e-9, max(abs(a), abs(b)) * 1e-10)
        if abs(a - b) > tol:
            return True

    for field in _STATE_TEXT_FIELDS:
        if str(candidate_row.get(field, "")).strip() != str(latest_row.get(field, "")).strip():
            return True

    for field in _STATE_JSON_FIELDS:
        if _canonical_json(candidate_row.get(field)) != _canonical_json(latest_row.get(field)):
            return True

    # key_strikes_json can contain extra PRIOR-anchor levels, so comparing the whole
    # object would falsely flag a weekend/holiday as changed simply because the
    # comparison anchor changed. Compare only the CURRENT structural levels.
    try:
        cand_keys = json.loads(str(candidate_row.get("key_strikes_json") or "{}"))
    except Exception:
        cand_keys = {}
    try:
        latest_keys = json.loads(str(latest_row.get("key_strikes_json") or "{}"))
    except Exception:
        latest_keys = {}

    current_levels = [
        candidate_row.get("option_poc"), candidate_row.get("value_area_low"), candidate_row.get("value_area_high"),
        candidate_row.get("ce_wall"), candidate_row.get("pe_wall"),
        candidate_row.get("safer_sell_ce"), candidate_row.get("safer_sell_pe"),
    ]
    for level in current_levels:
        try:
            key = str(int(round(float(level))))
        except Exception:
            continue
        if _canonical_json(cand_keys.get(key, {})) != _canonical_json(latest_keys.get(key, {})):
            return True

    return False


def run_odme_scan(
    store: BaseStore,
    angel: AngelConnector,
    master: pd.DataFrame,
    instrument: str,
    expiry: str,
    save_only_if_changed: bool = True,
) -> Dict[str, Any]:
    """Run one ODME scan using the exact same Angel + ODME engine as the dashboard."""
    instrument = str(instrument).upper().strip()
    expiry = str(expiry).strip()
    key = make_key(instrument, expiry)

    angel.ensure_session_ready()

    previous_raw = store.load_anchor_odme_snapshot(key)
    previous = parse_previous_summary(previous_raw)
    latest_raw = store.load_latest_odme_snapshot(key)

    if isinstance(previous_raw, dict) and previous_raw.get("ts"):
        anchor_note = f"anchor=latest saved changed snapshot before today ({previous_raw.get('ts')})"
    else:
        anchor_note = "anchor=no saved snapshot before today; observation mode until a prior changed session exists"

    chain, info = angel.fetch_option_chain_snapshot(master, instrument, expiry)
    future_ltp = float(info.get("future_ltp") or 0)
    if future_ltp <= 0:
        raise AngelDataError("Angel future LTP was not available. ODME analysis blocked; no spot assumption used.")

    usable = int((pd.to_numeric(chain.get("oi", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum())
    result = analyze_odme(chain, instrument, future_ltp, previous_summary=previous)
    result["_previous_summary"] = previous
    result["anchor_snapshot_ts"] = previous_raw.get("ts", "") if isinstance(previous_raw, dict) else ""
    result["future_ltp"] = future_ltp
    result["future_symbol"] = info.get("future_symbol", "")
    result["future_token"] = info.get("future_token", "")
    result["future_expiry"] = info.get("future_expiry", "")
    result["future_feed_time"] = info.get("future_feed_time", "")
    result["future_trade_time"] = info.get("future_trade_time", "")
    result["future_mapping_reason"] = info.get("future_mapping_reason", "")
    result["option_expiry_used_for_mapping"] = info.get("option_expiry_used_for_mapping", "")

    snapshot_id = make_snapshot_id(key)
    ts = utc_now_iso()
    result["ts"] = ts
    status_note = "OK" if usable > 0 else "Selected expiry has no usable OI."
    future_note = (
        f"future={info.get('future_symbol', '')}; future_ltp={future_ltp:,.2f}; "
        f"future_expiry={info.get('future_expiry', '')}; future_token={info.get('future_token', '')}"
    )
    meta = {
        "snapshot_id": snapshot_id,
        "key": key,
        "ts": ts,
        "instrument": instrument,
        "exchange": info.get("exchange", ""),
        "expiry": expiry,
        "source": "Angel SmartAPI FULL options + verified Angel futures LTP → ODME compact summary",
        "usable_oi_count": usable,
        "notes": f"{status_note}; {anchor_note}; {future_note}; contracts={len(chain)}; unfetched={info.get('unfetched_count', 0)}",
    }

    candidate_row = make_summary_row(result, meta)
    changed = market_state_changed(candidate_row, latest_raw)
    saved = (not save_only_if_changed) or changed
    if saved:
        store.append_odme_snapshot(result, meta)

    return {
        "result": result,
        "meta": meta,
        "usable": usable,
        "contracts": len(chain),
        "future_ltp": future_ltp,
        "changed": changed,
        "saved": saved,
        "anchor": previous_raw or {},
        "latest_before": latest_raw or {},
    }
