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
from superbrain_reasoner import analyze_market, REASONER_VERSION

SUPERBRAIN_BRIDGE_VERSION = "SB3.6_FINAL_CONTEXT_LIQUIDITY_WATCH"


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
            "eta_up_model_bars", "eta_up_model_distance", "eta_up_model_atr",
            "eta_up_speed_price_per_bar", "eta_up_speed_atr_per_bar",
            "eta_down_model_bars", "eta_down_model_distance", "eta_down_model_atr",
            "eta_down_speed_price_per_bar", "eta_down_speed_atr_per_bar",
        ],
        "STRUCTURE": [
            "structure_trend", "struct_high", "struct_low", "last_clean_price",
            "last_clean_time", "fork_valid", "fork_slope", "fork_slope_flip",
            "fork_position", "fork_reclaimed_2sd", "fork_active_kind", "fork_median",
            "fork_upper_1sd", "fork_upper_2sd", "fork_lower_1sd", "fork_lower_2sd",
            "fork_reclaim_side",
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
            "edge_needs_alert_change", "edge_tracking_reason",
            "fp_d_valid", "fp_d_zone_id", "fp_d_low", "fp_d_high",
            "fp_d_strength_score", "fp_d_strength", "fp_d_hold", "fp_d_status",
            "fp_d_context_state", "fp_d_interacting", "fp_d_waiting_confirmation",
            "fp_d_breach_pending", "fp_d_event_type", "fp_d_event_score", "fp_d_distance_atr",
            "fp_s_valid", "fp_s_zone_id", "fp_s_low", "fp_s_high",
            "fp_s_strength_score", "fp_s_strength", "fp_s_hold", "fp_s_status",
            "fp_s_context_state", "fp_s_interacting", "fp_s_waiting_confirmation",
            "fp_s_breach_pending", "fp_s_event_type", "fp_s_event_score", "fp_s_distance_atr",
            "raw_json",
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
        "active_pe_wall", "usable_oi_count", "source", "commentary",
    ]
    return {k: row.get(k) for k in fields if row.get(k) not in (None, "")}


def _compact_live_odme(result: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    if not result:
        return {}
    scores = result.get("scores", {}) or {}
    out: Dict[str, Any] = {
        "snapshot_id": meta.get("snapshot_id", ""),
        "ts": result.get("ts") or meta.get("ts", ""),
        "instrument": meta.get("instrument", ""),
        "exchange": meta.get("exchange", ""),
        "expiry": meta.get("expiry", ""),
        "spot": result.get("spot") or result.get("future_ltp", ""),
        "option_poc": result.get("poc", ""),
        "value_area_low": result.get("value_area_low", ""),
        "value_area_high": result.get("value_area_high", ""),
        "ce_wall": result.get("ce_wall", ""),
        "pe_wall": result.get("pe_wall", ""),
        "active_ce_wall": result.get("ce_wall", ""),
        "active_pe_wall": result.get("pe_wall", ""),
        "safer_sell_ce": result.get("safer_sell_ce", ""),
        "safer_sell_pe": result.get("safer_sell_pe", ""),
        "ce_wall_shift": result.get("ce_wall_move", ""),
        "pe_wall_shift": result.get("pe_wall_move", ""),
        "poc_shift": result.get("poc_move", ""),
        "range_shift": result.get("range_move", ""),
        "bullish_score": scores.get("Bullish", 0),
        "bearish_score": scores.get("Bearish", 0),
        "range_score": scores.get("Range", 0),
        "expansion_score": scores.get("Expansion", 0),
        "odme_tilt": result.get("tilt", ""),
        "commentary": result.get("commentary", ""),
        "premium_alert": result.get("premium_alert", ""),
        "final_action": result.get("final_action", ""),
        "hero_action": result.get("hero_action", ""),
        "ce_action": result.get("ce_action", ""),
        "pe_action": result.get("pe_action", ""),
        "path_risk": result.get("path_risk", {}) or {},
        "usable_oi_count": meta.get("usable_oi_count", ""),
        "source": meta.get("source", ""),
    }
    keys = result.get("key_strikes", {}) or {}
    premiums: Dict[str, Any] = {}
    for name in ("ce_wall", "pe_wall", "safer_sell_ce", "safer_sell_pe"):
        level = result.get(name)
        try:
            k = str(int(round(float(level))))
        except Exception:
            continue
        row = keys.get(k, {}) if isinstance(keys, dict) else {}
        if row:
            premiums[k] = {"strike": row.get("strike", level), "ce_ltp": row.get("ce_ltp", 0), "pe_ltp": row.get("pe_ltp", 0)}
    if premiums:
        out["key_premiums"] = premiums
    return {k: v for k, v in out.items() if v not in (None, "")}


def _apply_trade_plan(store: BaseStore, instrument: str, mode: str, scan_id: str, analysis: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan = analysis.get("trade_plan", {}) or {}
    kind = str(plan.get("kind", "") or "").upper()
    if kind not in {"NEW", "MANAGE"}:
        return {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if kind == "NEW":
        trade = {
            "instrument": instrument,
            "status": "ACTIVE",
            "strategy_type": plan.get("strategy_type", ""),
            "direction": plan.get("direction", ""),
            "scan_id": scan_id,
            "mode": mode,
            "action": "ENTER",
            "thesis": plan.get("reason", ""),
            "entry_reference": plan.get("entry_reference", ""),
            "target": plan.get("target", ""),
            "invalidation": plan.get("invalidation", ""),
            "expected_eta": plan.get("expected_eta", ""),
            "risk": plan.get("risk", ""),
            "reward": plan.get("reward", ""),
            "rr": plan.get("rr", ""),
            "legs_json": _json_dumps(plan.get("legs", []) or []),
            "market_state_json": _json_dumps({"entry_scan_id": scan_id, "entry_price": plan.get("entry_reference", ""), "entry_quality": plan.get("entry_quality", ""), "posture": analysis.get("posture", "")}),
            "metadata_json": _json_dumps({"entry_quality": plan.get("entry_quality", ""), "late": bool(plan.get("late")), "pullback_low": plan.get("pullback_low", ""), "pullback_high": plan.get("pullback_high", ""), "reasoner_version": analysis.get("reasoner_version", "")}),
            "created_at": now,
            "updated_at": now,
        }
        saved = store.upsert_superbrain_trade(trade)
    else:
        trade_id = str(plan.get("trade_id", "") or "")
        existing = next((dict(x) for x in open_trades if str(x.get("trade_id", "")) == trade_id), {})
        if not existing:
            return {}
        trade = dict(existing)
        action = str(plan.get("action", "HOLD") or "HOLD").upper()
        status = str(plan.get("status", "ACTIVE") or "ACTIVE").upper()
        trade.update({"instrument": instrument, "trade_id": trade_id, "status": status, "scan_id": scan_id, "mode": mode, "action": action, "thesis": plan.get("reason", trade.get("thesis", "")), "updated_at": now})
        if action == "EXIT":
            trade["close_reason"] = plan.get("reason", "")
        saved = store.upsert_superbrain_trade(trade)
    analysis["recorded_exposure"] = {"trade_id": saved.get("trade_id", ""), "status": saved.get("status", ""), "strategy_type": saved.get("strategy_type", ""), "direction": saved.get("direction", ""), "action": saved.get("action", "")}
    plan["trade_id"] = saved.get("trade_id", "")
    return saved


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
    live_odme_result: Dict[str, Any],
    live_odme_meta: Dict[str, Any],
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
    odme_compact = _compact_live_odme(live_odme_result or {}, live_odme_meta or {}) if odme_live and live_odme_result else _compact_odme(latest_odme or {})
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

    previous_compact = _load_json_object(previous.get("market_state_json", ""))
    analysis = analyze_market(evidence, previous_compact, open_trades=open_trades)
    saved_trade = _apply_trade_plan(store, instrument, mode, scan_id, analysis, open_trades)
    active_ids = [str(x.get("trade_id", "")) for x in open_trades if str(x.get("trade_id", "")).strip()]
    if saved_trade:
        tid = str(saved_trade.get("trade_id", "") or "")
        terminal = {"CLOSED", "EXIT", "TARGET", "INVALIDATED", "EXPIRED", "CANCELLED"}
        if str(saved_trade.get("status", "")).upper() in terminal:
            active_ids = [x for x in active_ids if x != tid]
        elif tid and tid not in active_ids:
            active_ids.append(tid)
    evidence["open_trade_ids"] = active_ids
    evidence["analysis"] = analysis

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

    state_row = {
        "status": "ACTIVE",
        "scan_id": scan_id,
        "previous_scan_id": str(previous.get("scan_id", "") or ""),
        "input_fingerprint": fingerprint,
        "mode": mode,
        "action": str(analysis.get("posture", "") or ""),
        "thesis": " ".join(analysis.get("lines", [])[:3]),
        "market_state_json": _json_dumps(evidence),
        "previous_state_json": _json_dumps(previous_compact) if previous_compact else "",
        "metadata_json": _json_dumps({
            "open_trade_count": len(evidence.get("open_trade_ids", [])),
            "memory_schema": "SBMEM2_COMPACT",
            "bridge_version": SUPERBRAIN_BRIDGE_VERSION,
            "reasoner_version": REASONER_VERSION,
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
        "analysis": analysis,
    }


def prepare_superbrain_scan(store: BaseStore, instrument: str) -> Dict[str, Any]:
    """Ask SuperBrain orchestration packet with explicit stage diagnostics.

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
            live_odme_result=(odme_outcome.get("result", {}) if odme_outcome else {}),
            live_odme_meta=(odme_outcome.get("meta", {}) if odme_outcome else {}),
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
            "analysis": memory.get("analysis", {}),
            "superbrain_build": SUPERBRAIN_BRIDGE_VERSION,
        }
    except Exception as exc:
        raise RuntimeError(
            f"{SUPERBRAIN_BRIDGE_VERSION} [{stage}] {type(exc).__name__}: {exc}"
        ) from exc


# ============================================================================
# SB3.7 campaign / expression management overrides
# ============================================================================
SUPERBRAIN_BRIDGE_VERSION = "SB3.7_CAMPAIGN_EXPRESSION_MANAGER"

_compact_live_odme_sb36 = _compact_live_odme
_apply_trade_plan_sb36 = _apply_trade_plan


def _load_json_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [dict(x) for x in value if isinstance(x, dict)]
    try:
        parsed = json.loads(str(value or ""))
        return [dict(x) for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []
    except Exception:
        return []


def _compact_live_odme(result: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """SB3.7: retain the exact nearest live option strike for defined-risk expression.

    This does not change ODME or its Sheet schema.  It only carries a tiny live
    ATM snapshot into the SuperBrain evidence packet when ODME already returned
    the option-chain key_strikes map.
    """
    out = dict(_compact_live_odme_sb36(result, meta) or {})
    try:
        spot = float(result.get("spot") or result.get("future_ltp"))
    except Exception:
        spot = None
    keys = result.get("key_strikes", {}) or {}
    if spot is not None and isinstance(keys, dict) and keys:
        candidates: List[Dict[str, Any]] = []
        for raw_key, raw_row in keys.items():
            row = raw_row if isinstance(raw_row, dict) else {}
            try:
                strike = float(row.get("strike", raw_key))
            except Exception:
                continue
            candidates.append({"strike": strike, "row": row})
        if candidates:
            nearest = min(candidates, key=lambda x: abs(x["strike"] - spot))
            row = nearest["row"]
            out["atm_strike"] = nearest["strike"]
            for src, dst in (("ce_ltp", "atm_ce_ltp"), ("pe_ltp", "atm_pe_ltp")):
                try:
                    value = float(row.get(src, 0) or 0)
                except Exception:
                    value = 0.0
                if value > 0:
                    out[dst] = value
    return out


def _campaign_leg_label(leg: Dict[str, Any]) -> str:
    side = str(leg.get("side", "") or "").upper()
    option = str(leg.get("option", "") or "").upper()
    kind = str(leg.get("instrument_type", "") or "").upper()
    strike = leg.get("strike", "")
    if option in {"CE", "PE"}:
        try:
            strike_text = str(int(round(float(strike))))
        except Exception:
            strike_text = str(strike or "ATM")
        return f"{side} {strike_text} {option}".strip()
    if kind == "FUTURES" or str(leg.get("contract", "") or "").upper() == "FUTURES":
        return f"{side} FUTURES".strip()
    return f"{side} {kind}".strip()


def _normalise_campaign_legs(trade: Dict[str, Any], now: str) -> List[Dict[str, Any]]:
    trade_id = str(trade.get("trade_id", "") or "")
    legs = _load_json_list(trade.get("legs_json"))
    strategy = str(trade.get("strategy_type", "") or "").upper()
    direction = str(trade.get("direction", "") or "").upper()
    if not legs and strategy in {"FUTURES_LONG", "FUTURES_SHORT"}:
        legs = [{
            "side": "BUY" if strategy == "FUTURES_LONG" else "SELL",
            "instrument_type": "FUTURES",
            "contract": "FUTURES",
            "role": "PRIMARY",
            "status": "ACTIVE",
            "entry_reference": trade.get("entry_reference", ""),
        }]
    out: List[Dict[str, Any]] = []
    for idx, raw in enumerate(legs):
        leg = dict(raw)
        if not leg.get("leg_id"):
            suffix = hashlib.sha1(f"{trade_id}|{idx}|{_campaign_leg_label(leg)}".encode("utf-8")).hexdigest()[:8].upper()
            leg["leg_id"] = f"LEG-{suffix}"
        leg.setdefault("status", "ACTIVE")
        leg.setdefault("role", "PRIMARY" if idx == 0 else "INCOME")
        if not leg.get("instrument_type"):
            leg["instrument_type"] = "OPTION" if str(leg.get("option", "") or "").upper() in {"CE", "PE"} else "FUTURES"
        leg.setdefault("opened_at", str(trade.get("created_at", "") or now))
        leg.setdefault("entry_reference", trade.get("entry_reference", ""))
        if direction:
            leg.setdefault("campaign_direction", direction)
        out.append(leg)
    return out


def _decorate_new_leg(raw: Dict[str, Any], now: str, entry_reference: Any, role: str = "PRIMARY") -> Dict[str, Any]:
    leg = dict(raw or {})
    leg.setdefault("leg_id", f"LEG-{uuid.uuid4().hex[:10].upper()}")
    leg.setdefault("status", "ACTIVE")
    leg.setdefault("role", role)
    if not leg.get("instrument_type"):
        leg["instrument_type"] = "OPTION" if str(leg.get("option", "") or "").upper() in {"CE", "PE"} else "FUTURES"
    leg.setdefault("opened_at", now)
    leg.setdefault("entry_reference", entry_reference)
    return leg


def _campaign_event(event_type: str, now: str, scan_id: str, reason: str, before: List[Dict[str, Any]], after: List[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    before_active = [_campaign_leg_label(x) for x in before if str(x.get("status", "ACTIVE")).upper() == "ACTIVE"]
    after_active = [_campaign_leg_label(x) for x in after if str(x.get("status", "ACTIVE")).upper() == "ACTIVE"]
    event = {
        "ts": now,
        "scan_id": scan_id,
        "event": event_type,
        "reason": reason,
        "before": before_active,
        "after": after_active,
        "closed_legs": [x for x in before_active if x not in after_active],
        "opened_legs": [x for x in after_active if x not in before_active],
    }
    if extra:
        event.update(extra)
    return event


def _apply_trade_plan(store: BaseStore, instrument: str, mode: str, scan_id: str, analysis: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """SB3.7 campaign persistence.

    One TRADE row is the campaign header.  Multiple coordinated expressions are
    kept as legs in legs_json.  metadata_json contains a bounded campaign event
    ledger.  No broker execution is performed here.
    """
    plan = analysis.get("trade_plan", {}) or {}
    kind = str(plan.get("kind", "") or "").upper()
    if kind not in {"NEW", "MANAGE"}:
        return {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if kind == "NEW":
        trade_id = str(plan.get("trade_id", "") or f"SB-{uuid.uuid4().hex[:12].upper()}")
        strategy = str(plan.get("strategy_type", "") or "").upper()
        raw_legs = [dict(x) for x in (plan.get("legs", []) or []) if isinstance(x, dict)]
        if not raw_legs and strategy in {"FUTURES_LONG", "FUTURES_SHORT"}:
            raw_legs = [{"side": "BUY" if strategy == "FUTURES_LONG" else "SELL", "instrument_type": "FUTURES", "contract": "FUTURES"}]
        legs = [_decorate_new_leg(x, now, plan.get("entry_reference", ""), "PRIMARY" if i == 0 else "INCOME") for i, x in enumerate(raw_legs)]
        event = _campaign_event("OPEN_CAMPAIGN", now, scan_id, str(plan.get("reason", "") or ""), [], legs, {"expression": strategy})
        metadata = {
            "campaign_schema": "SB_CAMPAIGN1",
            "campaign_thesis": plan.get("reason", ""),
            "current_expression": strategy,
            "entry_quality": plan.get("entry_quality", ""),
            "late": bool(plan.get("late")),
            "pullback_low": plan.get("pullback_low", ""),
            "pullback_high": plan.get("pullback_high", ""),
            "reasoner_version": analysis.get("reasoner_version", ""),
            "last_management_reason": plan.get("reason", ""),
            "campaign_events": [event],
        }
        trade = {
            "instrument": instrument,
            "trade_id": trade_id,
            "status": "ACTIVE",
            "strategy_type": strategy,
            "direction": plan.get("direction", ""),
            "scan_id": scan_id,
            "mode": mode,
            "action": "ENTER",
            "thesis": plan.get("reason", ""),
            "entry_reference": plan.get("entry_reference", ""),
            "target": plan.get("target", ""),
            "invalidation": plan.get("invalidation", ""),
            "expected_eta": plan.get("expected_eta", ""),
            "risk": plan.get("risk", ""),
            "reward": plan.get("reward", ""),
            "rr": plan.get("rr", ""),
            "legs_json": _json_dumps(legs),
            "market_state_json": _json_dumps({"entry_scan_id": scan_id, "entry_price": plan.get("entry_reference", ""), "entry_quality": plan.get("entry_quality", ""), "posture": analysis.get("posture", "")}),
            "metadata_json": _json_dumps(metadata),
            "created_at": now,
            "updated_at": now,
        }
        saved = store.upsert_superbrain_trade(trade)
    else:
        trade_id = str(plan.get("trade_id", "") or "")
        existing = next((dict(x) for x in open_trades if str(x.get("trade_id", "")) == trade_id), {})
        if not existing:
            return {}
        trade = dict(existing)
        metadata = _load_json_object(trade.get("metadata_json", ""))
        events = metadata.get("campaign_events") if isinstance(metadata.get("campaign_events"), list) else []
        before = _normalise_campaign_legs(trade, now)
        legs = [dict(x) for x in before]
        operation = str(plan.get("campaign_operation", "") or "").upper()
        action = str(plan.get("action", "HOLD") or "HOLD").upper()
        status = str(plan.get("status", "ACTIVE") or "ACTIVE").upper()
        reason = str(plan.get("reason", "") or "")

        close_ids = {str(x) for x in (plan.get("close_leg_ids", []) or []) if str(x)}
        close_options = {str(x).upper() for x in (plan.get("close_option_sides", []) or []) if str(x)}
        if action == "EXIT" and not operation:
            operation = "EXIT_CAMPAIGN"
        if operation == "EXIT_CAMPAIGN":
            close_ids.update(str(x.get("leg_id", "")) for x in legs if str(x.get("status", "ACTIVE")).upper() == "ACTIVE")
        if operation == "ROTATE" and not close_ids:
            close_ids.update(str(x.get("leg_id", "")) for x in legs if str(x.get("status", "ACTIVE")).upper() == "ACTIVE" and str(x.get("role", "PRIMARY")).upper() in {"PRIMARY", "EXPRESSION"})

        for leg in legs:
            active = str(leg.get("status", "ACTIVE")).upper() == "ACTIVE"
            option = str(leg.get("option", "") or "").upper()
            if active and (str(leg.get("leg_id", "")) in close_ids or (option and option in close_options)):
                leg["status"] = "CLOSED"
                leg["closed_at"] = now
                leg["close_reason"] = reason

        new_raw = plan.get("new_legs", []) or []
        role = "ADD" if operation == "ADD_LEG" else "PRIMARY"
        for raw in new_raw:
            if isinstance(raw, dict):
                legs.append(_decorate_new_leg(raw, now, plan.get("entry_reference", analysis.get("price", "")), role))

        next_strategy = str(plan.get("next_strategy_type", "") or plan.get("strategy_type", "") or trade.get("strategy_type", "")).upper()
        next_direction = str(plan.get("direction", "") or trade.get("direction", "")).upper()
        if operation in {"ROTATE", "TRANSFORM", "ADD_LEG", "CLOSE_LEG"} and next_strategy:
            trade["strategy_type"] = next_strategy
        if next_direction:
            trade["direction"] = next_direction

        active_after = [x for x in legs if str(x.get("status", "ACTIVE")).upper() == "ACTIVE"]
        if not active_after and operation == "EXIT_CAMPAIGN":
            status = str(plan.get("status", "CLOSED") or "CLOSED").upper()
        elif active_after:
            status = "ACTIVE"

        event_type = operation or ({"HOLD": "HOLD_CAMPAIGN", "ADD": "ADD_EXPOSURE", "REDUCE": "REDUCE_EXPOSURE", "EXIT": "EXIT_CAMPAIGN"}.get(action, action or "MANAGE"))
        material_event = event_type not in {"HOLD_CAMPAIGN", "ADOPT"} or metadata.get("campaign_schema") != "SB_CAMPAIGN1"
        if material_event or event_type == "ADOPT":
            events.append(_campaign_event(event_type, now, scan_id, reason, before, legs, {
                "from_expression": str(existing.get("strategy_type", "") or "").upper(),
                "to_expression": next_strategy,
            }))
            events = events[-40:]

        # Bound leg history so long-lived campaigns stay below the existing
        # per-cell persistence guard. The event ledger preserves the chronology.
        active_kept = [x for x in legs if str(x.get("status", "ACTIVE")).upper() == "ACTIVE"]
        closed_kept = [x for x in legs if str(x.get("status", "ACTIVE")).upper() == "CLOSED"][-20:]
        legs = active_kept + closed_kept

        metadata.update({
            "campaign_schema": "SB_CAMPAIGN1",
            "campaign_thesis": metadata.get("campaign_thesis") or existing.get("thesis", ""),
            "current_expression": next_strategy,
            "last_management_reason": reason,
            "last_campaign_operation": event_type,
            "reasoner_version": analysis.get("reasoner_version", ""),
            "campaign_events": events,
        })
        trade.update({
            "instrument": instrument,
            "trade_id": trade_id,
            "status": status,
            "scan_id": scan_id,
            "mode": mode,
            "action": action,
            "updated_at": now,
            "legs_json": _json_dumps(legs),
            "metadata_json": _json_dumps(metadata),
        })
        # Preserve the campaign thesis.  Management reasons live in metadata/event ledger.
        if not str(trade.get("thesis", "") or "").strip():
            trade["thesis"] = metadata.get("campaign_thesis", "")
        if action == "EXIT" or operation == "EXIT_CAMPAIGN":
            trade["close_reason"] = reason
        saved = store.upsert_superbrain_trade(trade)

    saved_legs = _load_json_list(saved.get("legs_json", ""))
    analysis["recorded_exposure"] = {
        "trade_id": saved.get("trade_id", ""),
        "status": saved.get("status", ""),
        "strategy_type": saved.get("strategy_type", ""),
        "direction": saved.get("direction", ""),
        "action": saved.get("action", ""),
        "campaign_operation": plan.get("campaign_operation", ""),
        "active_legs": [_campaign_leg_label(x) for x in saved_legs if str(x.get("status", "ACTIVE")).upper() == "ACTIVE"],
    }
    plan["trade_id"] = saved.get("trade_id", "")
    return saved
