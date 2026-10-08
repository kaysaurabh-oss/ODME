from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import uuid
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, List

import pandas as pd
import streamlit as st

from angel_connector import AngelConnector, AngelDataError, AngelSessionError, load_angel_credentials
from data_store import get_store, make_key, make_snapshot_id, parse_previous_summary, utc_now_iso
from odme_config import APP_NAME, REFRESH_INTERVAL_SECONDS, SUPPORTED_INSTRUMENTS
from odme_engine import analyze_odme, reconstruct_saved_result
from scan_service import run_odme_scan
from superbrain_bridge import (
    build_instrument_map,
    prepare_superbrain_scan,
    record_superbrain_trade_taken,
)

st.set_page_config(page_title="ODME Angel", layout="wide")


@st.cache_resource(show_spinner=False)
def _angel_session_cache() -> Dict[str, Any]:
    """Process-level cache to survive Streamlit websocket/session resets while app process is alive.

    This does not survive Streamlit Cloud sleep/restart, but it reduces repeated TOTP prompts
    during normal reruns or temporary browser reconnects.
    """
    return {}


# =============================================================================
# Session / login
# =============================================================================


def init_session() -> None:
    defaults = {
        "logged_in": False,
        "angel": None,
        "master": None,
        "last_refresh_by_key": {},
        "last_result_by_key": {},
        "login_error": "",
        "angel_login_at": "",
        "public_superbrain_last": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)

    # Restore cached Angel session after a Streamlit websocket reset, if the
    # Python process is still alive. If Streamlit Cloud slept/restarted, cache
    # will be empty and a fresh TOTP login is unavoidable.
    if not st.session_state.get("logged_in"):
        cache = _angel_session_cache()
        if cache.get("angel") is not None and cache.get("master") is not None:
            st.session_state.logged_in = True
            st.session_state.angel = cache.get("angel")
            st.session_state.master = cache.get("master")
            st.session_state.angel_login_at = cache.get("angel_login_at", "")


def login_page() -> None:
    inject_css()
    st.title(APP_NAME)
    st.caption("ODME + TradingView market intelligence terminal")

    render_public_superbrain()

    st.markdown("---")
    st.subheader("Angel login")
    st.info("Login is needed only for ODME setup, manual controls and history management.")

    with st.form("login_form"):
        totp = st.text_input("Current Angel TOTP", type="password", max_chars=8)
        submitted = st.form_submit_button("Login")

    if submitted:
        try:
            creds = load_angel_credentials()
            angel = AngelConnector(creds)
            angel.login(totp)
            master = angel.load_instrument_master()
            st.session_state.logged_in = True
            st.session_state.angel = angel
            st.session_state.master = master
            st.session_state.angel_login_at = angel.login_time_utc or ""
            cache = _angel_session_cache()
            cache["angel"] = angel
            cache["master"] = master
            cache["angel_login_at"] = angel.login_time_utc or ""
            st.success("Angel login successful. Instrument master loaded. Login will be reused while the Streamlit process remains active.")
            st.rerun()
        except Exception as exc:
            st.session_state.login_error = str(exc)
            st.error(str(exc))


def _sb_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        x = json.loads(str(value or ""))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _sb_list_json(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    try:
        x = json.loads(str(value or ""))
        return [y for y in x if isinstance(y, dict)] if isinstance(x, list) else []
    except Exception:
        return []


def _sb_fmt_time(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            n = float(value)
            if n > 1e12:
                dt = datetime.fromtimestamp(n / 1000.0, tz=timezone.utc)
            elif n > 1e9:
                dt = datetime.fromtimestamp(n, tz=timezone.utc)
            else:
                return str(value)
        else:
            txt = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Asia/Singapore")).strftime("%d %b %H:%M")
    except Exception:
        return str(value)


def _sb_fmt_level(value: Any) -> str:
    try:
        n = float(value)
        if abs(n) >= 1000:
            return f"{n:,.0f}" if n.is_integer() else f"{n:,.2f}"
        return f"{n:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value or "—")


def _sb_tv_close_dt(source: str, row: Dict[str, Any]) -> Optional[datetime]:
    """Normalize the four TradingView feeds to the confirmed candle-close time.

    EDGE/AURORA carry the confirmed close timestamp. STRUCTURE/LIQUIDITY carry
    Pine `time` (bar open), so add the source timeframe before comparing/displaying.
    """
    raw = row.get("bar_time") or row.get("updated_at") or ""
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, (int, float)) or str(raw).strip().isdigit():
            n = float(raw)
            dt = datetime.fromtimestamp(n / 1000.0 if n > 1e12 else n, tz=timezone.utc)
        else:
            txt = str(raw).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        if str(source or "").upper() in {"STRUCTURE", "LIQUIDITY"} and row.get("bar_time") not in (None, ""):
            try:
                mins = int(float(row.get("tf")))
            except Exception:
                mins = 0
            if mins > 0:
                dt = dt + timedelta(minutes=mins)
        return dt
    except Exception:
        return None


def _sb_tv_freshness(packet: Dict[str, Any]) -> List[str]:
    tv = packet.get("tv_rows")
    if tv is None or not isinstance(tv, pd.DataFrame) or tv.empty or "source" not in tv.columns:
        return []

    feeds = []
    for source in ["EDGE", "AURORA", "STRUCTURE", "LIQUIDITY"]:
        g = tv[tv["source"].astype(str).str.upper().eq(source)]
        if g.empty:
            continue
        r = g.iloc[-1].to_dict()
        dt = _sb_tv_close_dt(source, r)
        tf = str(r.get("tf", "") or "")
        feeds.append((source, dt, tf))

    if not feeds:
        return []

    # When the synchronized sources refer to the same confirmed candle, show one
    # authoritative TV timestamp instead of four visually different raw times.
    # Only expose per-feed timestamps when a real normalized mismatch exists.
    complete = len(feeds) == 4 and all(x[1] is not None for x in feeds)
    same_tf = len({x[2] for x in feeds if x[2]}) <= 1
    if complete and same_tf:
        dts = [x[1] for x in feeds if x[1] is not None]
        spread = (max(dts) - min(dts)).total_seconds()
        if spread <= 60:
            dt = max(dts)
            tf = next((x[2] for x in feeds if x[2]), "")
            stamp = dt.astimezone(ZoneInfo("Asia/Singapore")).strftime("%d %b %H:%M")
            return [f"TV data {stamp}" + (f" · {tf}m" if tf else "")]

    rows = []
    for source, dt, tf in feeds:
        stamp = dt.astimezone(ZoneInfo("Asia/Singapore")).strftime("%d %b %H:%M") if dt else "—"
        rows.append(f"{source} {stamp}" + (f" · {tf}m" if tf else ""))
    return ["TV mismatch — " + " | ".join(rows)]


def _sb_has_value(value: Any) -> bool:
    """True only for display-safe, non-empty ODME values.

    Live ODME results can include pandas Series/DataFrames (for example strike
    tables). Comparing those objects to []/{} asks pandas for a boolean truth
    value and raises the ambiguous-truth ValueError. The clean terminal only
    needs scalar/JSON-like summary values, so pandas table objects are skipped.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (pd.Series, pd.DataFrame)):
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool):
            return not missing
    except Exception:
        pass
    return True


def _sb_odme_data(packet: Dict[str, Any]) -> Dict[str, Any]:
    live = ((packet.get("odme_outcome") or {}).get("result") or {}) if packet.get("odme_live") else {}
    latest = packet.get("latest_odme") or {}
    evidence_odme = packet.get("memory", {}).get("analysis", {}).get("odme") if isinstance(packet.get("memory"), dict) else {}
    data = dict(latest)
    if isinstance(evidence_odme, dict):
        data.update({k: v for k, v in evidence_odme.items() if _sb_has_value(v)})
    if isinstance(live, dict):
        # Live ODME result contains raw tables as well as summary values.
        # Keep only display-safe values in the compact Level-3 terminal.
        for k, v in live.items():
            if _sb_has_value(v):
                data[k] = v
    return data


def _sb_first(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        v = data.get(key)
        if _sb_has_value(v):
            return v
    return ""


def _sb_comment_section(text: str, heading: str) -> str:
    if not text:
        return ""
    target = heading.lower() + ":"
    known = ["odme verdict:", "what changed:", "positioning:", "walls:", "ce action:", "pe action:", "final action:", "risk note:"]
    active = False
    out = []
    for raw in str(text).splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith(target):
            active = True
            out.append(line.split(":", 1)[1].strip())
            continue
        if active and any(low.startswith(k) for k in known):
            break
        if active and line:
            out.append(line)
    return " ".join(x for x in out if x).strip()


def _sb_render_freshness(packet: Dict[str, Any]) -> None:
    odme = _sb_odme_data(packet)
    parts = _sb_tv_freshness(packet)
    odme_ts = _sb_first(odme, "ts", "generated_at")
    if odme_ts:
        expiry = str(_sb_first(odme, "expiry") or (packet.get("mapping") or {}).get("selected_expiry") or "")
        parts.append(f"ODME {_sb_fmt_time(odme_ts)}" + (f" · {expiry}" if expiry else ""))
    elif (packet.get("mapping") or {}).get("selected_expiry"):
        parts.append("ODME not refreshed")
    if parts:
        st.caption("  |  ".join(parts))


def _sb_render_odme(packet: Dict[str, Any]) -> None:
    odme = _sb_odme_data(packet)
    st.markdown("### Options / ODME")
    if not odme:
        st.write("No ODME snapshot is available for this instrument.")
        return
    tilt = str(_sb_first(odme, "odme_tilt", "tilt") or "—")
    poc = _sb_first(odme, "option_poc", "poc")
    ce_wall = _sb_first(odme, "active_ce_wall", "ce_wall")
    pe_wall = _sb_first(odme, "active_pe_wall", "pe_wall")
    safe_ce = _sb_first(odme, "safer_sell_ce", "safe_ce")
    safe_pe = _sb_first(odme, "safer_sell_pe", "safe_pe")
    va_low = _sb_first(odme, "value_area_low")
    va_high = _sb_first(odme, "value_area_high")
    st.write(f"**{tilt}**")
    assessment = _sb_odme_assessment(packet)
    direction = assessment.get("direction", "UNKNOWN")
    strength = assessment.get("strength", "NONE")
    control = assessment.get("control_quality", "UNKNOWN")
    expansion_dir = assessment.get("expansion_direction", "NONE")
    expansion_risk = assessment.get("expansion_risk", "LOW")
    expression = assessment.get("preferred_expression", "NONE")
    if direction in {"BULLISH", "BEARISH"}:
        support_text = f"{strength} SUPPORT" if strength in {"STRONG", "WEAK"} else "NO SUPPORT"
        directional_bits = [f"{direction} · {support_text}", f"{control} CONTROL"]
        if expansion_dir != "NONE":
            directional_bits.append(f"{expansion_dir} EXPANSION {expansion_risk}")
        if expression != "NONE":
            directional_bits.append(f"Preferred: {expression}")
        st.markdown("**Directional read:** " + " · ".join(directional_bits))
    elif direction == "NEUTRAL":
        st.markdown("**Directional read:** NEUTRAL · NO DIRECTIONAL SUPPORT")
    level_bits = [f"POC {_sb_fmt_level(poc)}", f"CE wall {_sb_fmt_level(ce_wall)}", f"PE wall {_sb_fmt_level(pe_wall)}", f"Safer CE {_sb_fmt_level(safe_ce)}", f"Safer PE {_sb_fmt_level(safe_pe)}"]
    if va_low not in (None, "") and va_high not in (None, ""):
        level_bits.append(f"Value area {_sb_fmt_level(va_low)}–{_sb_fmt_level(va_high)}")
    st.caption("  |  ".join(level_bits))
    commentary = str(_sb_first(odme, "commentary") or "")
    final_action = str(_sb_first(odme, "final_action") or _sb_comment_section(commentary, "Final Action") or "").strip()
    ce_action = str(_sb_first(odme, "ce_action") or _sb_comment_section(commentary, "CE Action") or "").strip()
    pe_action = str(_sb_first(odme, "pe_action") or _sb_comment_section(commentary, "PE Action") or "").strip()
    if final_action:
        st.write(final_action)
    elif commentary:
        verdict = _sb_comment_section(commentary, "ODME Verdict")
        if verdict:
            st.write(verdict)
    small = []
    if ce_action:
        small.append(f"CE: {ce_action}")
    if pe_action:
        small.append(f"PE: {pe_action}")
    if small:
        st.caption("  |  ".join(small))


def _sb_setup_md(row: Dict[str, Any]) -> Dict[str, Any]:
    return _sb_json((row or {}).get("metadata_json", ""))


def _sb_setup_for_trade(store: Any, trade: Dict[str, Any]) -> Dict[str, Any]:
    md = _sb_json((trade or {}).get("metadata_json", ""))
    rid = str(md.get("parent_setup_id") or md.get("origin_setup_record_id") or "").strip()
    if not rid:
        return {}
    try:
        df = store.list_superbrain_setups(instrument=str(trade.get("instrument", "") or ""))
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    hit = df[df["record_id"].astype(str).eq(rid)] if "record_id" in df.columns else pd.DataFrame()
    return hit.iloc[-1].to_dict() if not hit.empty else {}


def _sb_exposure_text(trade: Dict[str, Any]) -> str:
    legs = _sb_list_json(trade.get("legs_json", ""))
    labels = []
    for leg in legs:
        status = str(leg.get("status", "ACTIVE") or "ACTIVE").upper()
        if status not in {"ACTIVE", "OPEN", ""}:
            continue
        side = str(leg.get("side", "") or "").upper()
        opt = str(leg.get("option_type") or leg.get("option") or "").upper()
        strike = leg.get("strike")
        expiry = str(leg.get("expiry", "") or "")
        bits = [x for x in [side, _sb_fmt_level(strike) if strike not in (None, "") else "", opt, expiry] if x]
        if bits:
            labels.append(" ".join(bits))
    return " + ".join(labels) if labels else str(trade.get("strategy_type", "campaign") or "campaign")



def _sb_management_value(value: Any) -> Any:
    """Preserve user-entered contract fields while normalizing simple numerics."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    try:
        n = float(s.replace(",", ""))
        return int(n) if n.is_integer() else n
    except Exception:
        return s


def _sb_leg_identifier(trade_id: str, leg: Dict[str, Any], index: int) -> str:
    existing = str((leg or {}).get("leg_id") or "").strip()
    if existing:
        return existing
    return f"{trade_id or 'TRADE'}-L{index + 1}"


def _sb_active_leg_rows(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    trade_id = str((trade or {}).get("trade_id") or (trade or {}).get("record_id") or "TRADE").strip()
    rows = []
    for idx, raw in enumerate(_sb_list_json((trade or {}).get("legs_json", ""))):
        leg = dict(raw)
        status = str(leg.get("status", "ACTIVE") or "ACTIVE").upper()
        if status not in {"ACTIVE", "OPEN", ""}:
            continue
        leg_id = _sb_leg_identifier(trade_id, leg, idx)
        side = str(leg.get("side", "") or "").upper()
        opt = str(leg.get("option_type") or leg.get("option") or leg.get("instrument_type") or "").upper()
        strike = leg.get("strike")
        expiry = str(leg.get("expiry", "") or "")
        qty = leg.get("quantity", leg.get("qty", leg.get("lots", leg.get("size", ""))))
        bits = [x for x in [
            side,
            _sb_fmt_level(strike) if strike not in (None, "") else "",
            opt,
            expiry,
            f"Qty {qty}" if qty not in (None, "") else "",
        ] if x]
        rows.append({
            "index": idx,
            "leg": leg,
            "leg_id": leg_id,
            "label": " ".join(bits) if bits else f"Leg {idx + 1}",
        })
    return rows


def _sb_management_recommendation(trade: Dict[str, Any], setup: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """Use a structured current-plan management instruction when the deployed reasoner exposes one."""
    plan = (analysis or {}).get("trade_plan") or {}
    same_trade = str(plan.get("trade_id", "") or "").strip() == str((trade or {}).get("trade_id", "") or "").strip()
    if same_trade:
        for key in (
            "management_action", "recommended_adjustment", "recommendation",
            "next_action", "management", "action"
        ):
            value = plan.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _sb_campaign_management(trade, setup)


def _sb_record_management_adjustment(
    store: Any,
    packet: Dict[str, Any],
    trade: Dict[str, Any],
    action_code: str,
    selected_leg_index: Optional[int] = None,
    side: str = "",
    contract_type: str = "",
    strike: Any = "",
    expiry: str = "",
    quantity: Any = "",
    note: str = "",
) -> Dict[str, Any]:
    """Update the SAME active campaign after a user-confirmed management action.

    Old legs are retained with terminal leg statuses, while only current exposure
    remains ACTIVE/OPEN. This preserves campaign lineage and gives SuperBrain and
    the Level-2 monitor one authoritative current position.
    """
    if not trade:
        raise ValueError("Active campaign is missing.")

    trade_id = str(trade.get("trade_id") or "").strip()
    instrument = str(trade.get("instrument") or "").strip()
    if not trade_id or not instrument:
        raise ValueError("Active campaign is missing trade_id or instrument.")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    legs = [dict(x) for x in _sb_list_json(trade.get("legs_json", ""))]

    # Stabilize leg identifiers without changing existing identifiers.
    for idx, leg in enumerate(legs):
        leg.setdefault("leg_id", _sb_leg_identifier(trade_id, leg, idx))

    active_indexes = [
        i for i, leg in enumerate(legs)
        if str(leg.get("status", "ACTIVE") or "ACTIVE").upper() in {"ACTIVE", "OPEN", ""}
    ]

    action = str(action_code or "").upper().strip()
    needs_existing = action in {"ROLL", "CLOSE", "RESIZE"}
    if needs_existing:
        if selected_leg_index is None or selected_leg_index not in active_indexes:
            raise ValueError("Select the active leg that you adjusted.")

    tmd = _sb_json(trade.get("metadata_json", ""))
    try:
        revision = int(float(tmd.get("management_revision") or 0)) + 1
    except Exception:
        revision = 1
    event_id = f"MGMT-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{revision}"
    event: Dict[str, Any] = {
        "event_id": event_id,
        "recorded_at": now,
        "trade_id": trade_id,
        "action": action,
        "note": str(note or "").strip(),
        "management_revision": revision,
        "price": ((packet or {}).get("analysis") or {}).get("price"),
        "scan_id": ((packet or {}).get("memory") or {}).get("scan_id")
                   or ((packet or {}).get("analysis") or {}).get("scan_id")
                   or "",
        "source": "USER_CONFIRMED_SUPERBRAIN",
    }

    if action == "ROLL":
        old = legs[selected_leg_index]
        old_id = str(old.get("leg_id") or _sb_leg_identifier(trade_id, old, selected_leg_index))
        old_snapshot = {
            "leg_id": old_id,
            "side": old.get("side", ""),
            "option_type": old.get("option_type") or old.get("option") or old.get("instrument_type") or "",
            "strike": old.get("strike", ""),
            "expiry": old.get("expiry", ""),
            "quantity": old.get("quantity", old.get("qty", old.get("lots", old.get("size", "")))),
        }

        old["status"] = "ROLLED"
        old["closed_at"] = now
        old["management_event_id"] = event_id

        new_id = f"{trade_id}-M{revision}"
        new_leg = dict(old)
        for key in ("closed_at", "close_reason", "replaced_by_leg_id", "rolled_to_leg_id"):
            new_leg.pop(key, None)
        new_leg["leg_id"] = new_id
        new_leg["status"] = "ACTIVE"
        new_leg["opened_at"] = now
        new_leg["management_event_id"] = event_id
        new_leg["rolled_from_leg_id"] = old_id
        new_leg["side"] = str(side or old_snapshot["side"] or "").upper()
        new_leg["option_type"] = str(contract_type or old_snapshot["option_type"] or "").upper()
        if str(strike).strip():
            new_leg["strike"] = _sb_management_value(strike)
        if str(expiry or "").strip():
            new_leg["expiry"] = str(expiry).strip()
        if str(quantity).strip():
            new_leg["quantity"] = _sb_management_value(quantity)
        old["rolled_to_leg_id"] = new_id
        legs.append(new_leg)

        event["from_leg"] = old_snapshot
        event["to_leg"] = {
            "leg_id": new_id,
            "side": new_leg.get("side", ""),
            "option_type": new_leg.get("option_type", ""),
            "strike": new_leg.get("strike", ""),
            "expiry": new_leg.get("expiry", ""),
            "quantity": new_leg.get("quantity", new_leg.get("qty", new_leg.get("lots", new_leg.get("size", "")))),
        }

    elif action == "ADD":
        new_id = f"{trade_id}-M{revision}"
        new_leg = {
            "leg_id": new_id,
            "status": "ACTIVE",
            "opened_at": now,
            "management_event_id": event_id,
            "side": str(side or "").upper(),
            "option_type": str(contract_type or "").upper(),
            "strike": _sb_management_value(strike),
            "expiry": str(expiry or "").strip(),
            "quantity": _sb_management_value(quantity),
        }
        legs.append(new_leg)
        event["added_leg"] = dict(new_leg)

    elif action == "CLOSE":
        old = legs[selected_leg_index]
        old["status"] = "CLOSED"
        old["closed_at"] = now
        old["close_reason"] = str(note or "User-confirmed management close").strip()
        old["management_event_id"] = event_id
        event["closed_leg"] = {
            "leg_id": old.get("leg_id", ""),
            "side": old.get("side", ""),
            "option_type": old.get("option_type") or old.get("option") or "",
            "strike": old.get("strike", ""),
            "expiry": old.get("expiry", ""),
            "quantity": old.get("quantity", old.get("qty", old.get("lots", old.get("size", "")))),
        }

    elif action == "RESIZE":
        if not str(quantity).strip():
            raise ValueError("Enter the new size/quantity.")
        old = legs[selected_leg_index]
        previous_qty = old.get("quantity", old.get("qty", old.get("lots", old.get("size", ""))))
        old["quantity"] = _sb_management_value(quantity)
        old["management_event_id"] = event_id
        old["resized_at"] = now
        event["resized_leg"] = {
            "leg_id": old.get("leg_id", ""),
            "previous_quantity": previous_qty,
            "new_quantity": old.get("quantity", ""),
        }

    else:
        raise ValueError("Unknown management adjustment.")

    history = tmd.get("management_history")
    if not isinstance(history, list):
        history = []
    history.append(event)
    # Keep compact durable history; detailed prior legs remain in legs_json.
    history = history[-40:]

    tmd["management_revision"] = revision
    tmd["last_management_at"] = now
    tmd["last_management_action"] = action
    tmd["last_management_event_id"] = event_id
    tmd["management_history"] = history
    tmd["current_exposure_source"] = "USER_CONFIRMED"

    updated = dict(trade)
    remaining_active = [
        leg for leg in legs
        if str(leg.get("status", "ACTIVE") or "ACTIVE").upper() in {"ACTIVE", "OPEN", ""}
    ]
    if action == "CLOSE" and not remaining_active:
        updated["status"] = "CLOSED"
        updated["closed_at"] = now
        updated["close_reason"] = str(note or "All active legs closed by user-confirmed management action").strip()
    else:
        updated["status"] = "ACTIVE"
        updated["closed_at"] = ""
        updated["close_reason"] = ""
    updated["legs_json"] = json.dumps(legs, separators=(",", ":"), default=str)
    updated["metadata_json"] = json.dumps(tmd, separators=(",", ":"), default=str)

    return store.upsert_superbrain_trade(updated)


def _sb_render_management_confirmation(
    store: Any,
    packet: Dict[str, Any],
    trade: Dict[str, Any],
    setup: Dict[str, Any],
    analysis: Dict[str, Any],
    final_decisions: Optional[List[Dict[str, Any]]] = None,
    companion_action: Optional[Dict[str, Any]] = None,
) -> None:
    """User confirmation layer for rolls/adds/closes/resizes on an active campaign."""
    trade_id = str(trade.get("trade_id") or trade.get("record_id") or "campaign").strip()
    active_legs = _sb_active_leg_rows(trade)
    decision_lines = [_sb_campaign_decision_line(x) for x in (final_decisions or [])]
    if companion_action:
        c_action = str(companion_action.get("action") or "WATCH").upper()
        c_opt = str(companion_action.get("option_type") or "").upper()
        c_rank = companion_action.get("rank")
        c_strike = companion_action.get("strike")
        if c_action == "ADD" and c_strike is not None:
            decision_lines.append(f"ADD / SELL {_sb_fmt_level(c_strike)} {c_opt} at L{c_rank}.")
        else:
            decision_lines.append(f"{c_action} {c_opt} at L{c_rank}.")
    recommendation = " ".join(decision_lines) if decision_lines else "No strike-ladder adjustment is recommended on this scan."

    with st.expander("I did a management adjustment", expanded=False):
        st.caption(
            "Record only an adjustment you actually executed. This updates the same campaign; "
            "it does not create a new trade."
        )
        if recommendation:
            st.write(f"**Current management context:** {recommendation}")
        st.caption(f"Current exposure: {_sb_exposure_text(trade)}")

        action_labels = ["Add a new management leg"]
        if active_legs:
            action_labels = [
                "Roll / replace an active leg",
                "Add a new management leg",
                "Close an active leg",
                "Change size of an active leg",
            ]

        action_label = st.selectbox(
            "What did you do?",
            action_labels,
            key=f"sb_mgmt_action_{trade_id}",
        )
        action_map = {
            "Roll / replace an active leg": "ROLL",
            "Add a new management leg": "ADD",
            "Close an active leg": "CLOSE",
            "Change size of an active leg": "RESIZE",
        }
        action_code = action_map[action_label]

        selected_leg = None
        selected_idx = None
        if action_code in {"ROLL", "CLOSE", "RESIZE"}:
            selected_label = st.selectbox(
                "Which active leg did you adjust?",
                [x["label"] for x in active_legs],
                key=f"sb_mgmt_leg_{trade_id}_{action_code}",
            )
            selected_leg = next(x for x in active_legs if x["label"] == selected_label)
            selected_idx = int(selected_leg["index"])

        base_leg = selected_leg["leg"] if selected_leg else {}
        base_side = str(base_leg.get("side", "") or "SELL").upper()
        base_type = str(
            base_leg.get("option_type") or base_leg.get("option")
            or base_leg.get("instrument_type") or "CE"
        ).upper()
        side_options = list(dict.fromkeys([base_side, "SELL", "BUY"]))
        type_options = list(dict.fromkeys([base_type, "CE", "PE", "FUT", "CASH", "OTHER"]))

        with st.form(f"sb_mgmt_form_{trade_id}_{action_code}"):
            side = base_side
            contract_type = base_type
            strike = str(base_leg.get("strike", "") or "")
            expiry = str(base_leg.get("expiry", "") or "")
            quantity = str(
                base_leg.get("quantity", base_leg.get("qty", base_leg.get("lots", base_leg.get("size", ""))))
                or ""
            )

            if action_code in {"ROLL", "ADD"}:
                side = st.selectbox("Side", side_options, key=f"sb_mgmt_side_{trade_id}_{action_code}")
                contract_type = st.selectbox(
                    "Contract type", type_options, key=f"sb_mgmt_type_{trade_id}_{action_code}"
                )
                strike = st.text_input(
                    "New strike / contract level",
                    value=strike if action_code == "ROLL" else "",
                    key=f"sb_mgmt_strike_{trade_id}_{action_code}",
                    help="Required for CE/PE. Leave blank for a non-option leg if no strike applies.",
                )
                expiry = st.text_input(
                    "Expiry",
                    value=expiry,
                    key=f"sb_mgmt_expiry_{trade_id}_{action_code}",
                )
                quantity = st.text_input(
                    "Size / quantity / lots",
                    value=quantity,
                    key=f"sb_mgmt_qty_{trade_id}_{action_code}",
                )
            elif action_code == "RESIZE":
                quantity = st.text_input(
                    "New size / quantity / lots",
                    value=quantity,
                    key=f"sb_mgmt_qty_{trade_id}_{action_code}",
                )

            note = st.text_input(
                "Adjustment note (optional)",
                value="",
                key=f"sb_mgmt_note_{trade_id}_{action_code}",
            )

            submitted = st.form_submit_button(
                "I did this adjustment — update campaign",
                type="primary",
                use_container_width=True,
            )

        if submitted:
            try:
                if action_code in {"ROLL", "ADD"} and str(contract_type).upper() in {"CE", "PE"} and not str(strike).strip():
                    raise ValueError("Enter the executed option strike.")

                updated_trade = _sb_record_management_adjustment(
                    store=store,
                    packet=packet,
                    trade=trade,
                    action_code=action_code,
                    selected_leg_index=selected_idx,
                    side=side,
                    contract_type=contract_type,
                    strike=strike,
                    expiry=expiry,
                    quantity=quantity,
                    note=note,
                )

                # Refresh the visible packet immediately so the terminal shows the
                # new exposure without waiting for another scan.
                open_trades = []
                for item in (packet.get("open_trades") or []):
                    if str(item.get("trade_id", "") or "") == str(updated_trade.get("trade_id", "") or ""):
                        open_trades.append(updated_trade)
                    else:
                        open_trades.append(item)
                packet["open_trades"] = open_trades
                st.session_state.public_superbrain_last = packet
                st.session_state["superbrain_notice"] = (
                    f"Management adjustment recorded for {updated_trade.get('trade_id', trade_id)}. "
                    "The campaign now uses the updated exposure; run the next SuperBrain scan for fresh management intelligence."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Adjustment could not be recorded: {type(exc).__name__}: {exc}")


def _sb_current_market_line(analysis: Dict[str, Any]) -> str:
    price = analysis.get("price")
    macro = str(analysis.get("macro", "") or "NEUTRAL")
    of = str(analysis.get("exec_of", "") or "NEUTRAL")
    aur = analysis.get("aurora") or {}
    aura = str(aur.get("state", "") or "—")
    fork = analysis.get("fork") or {}
    pf = "—"
    if fork.get("valid"):
        pf = f"{str(fork.get('slope','')).upper()} / {str(fork.get('position','')).replace('_',' ')}"
    half = str(analysis.get("battlefield_half", "") or "—").replace("_", " ")
    return f"Price {_sb_fmt_level(price)} | Macro {macro} | OF {of} | AURORA {aura} | Pitchfork {pf} | Battlefield {half}"


def _sb_location_line(analysis: Dict[str, Any], packet: Optional[Dict[str, Any]] = None) -> str:
    # Prefer the same authoritative all-source ladder used for L1/L2/L3 decisions.
    if packet:
        try:
            level_map = _sb_combined_level_ladder(packet, analysis)
            if level_map.get("level_rank_reliable"):
                above = level_map.get("above") or []
                below = level_map.get("below") or []
                a1 = above[0] if above else None
                b1 = below[0] if below else None
                return (
                    "Nearest ranked levels — Above L1 " + _sb_level_group_text(a1, with_rank=False)
                    + " | Below L1 " + _sb_level_group_text(b1, with_rank=False)
                )
        except Exception:
            pass

    price = analysis.get("price")
    hurdles = analysis.get("hurdles") or []
    ranked = []
    for h in hurdles:
        try:
            p, lo, hi = float(price), float(h.get("low")), float(h.get("high"))
            dist = 0 if lo <= p <= hi else min(abs(p-lo), abs(p-hi))
            ranked.append((dist, h))
        except Exception:
            continue
    if ranked:
        h = sorted(ranked, key=lambda x: x[0])[0][1]
        side = str(h.get("side", "") or "").title()
        strength = str(h.get("strength", "") or "")
        status = str(h.get("status", "") or "").replace("_", " ").title()
        return f"Nearest {side.lower()} {_sb_fmt_level(h.get('low'))}–{_sb_fmt_level(h.get('high'))}" + (f" · {strength}" if strength else "") + (f" · {status}" if status else "")
    defender = analysis.get("defender") or {}
    challenger = analysis.get("challenger") or {}
    bits=[]
    if defender.get("low") not in (None, "") and defender.get("high") not in (None, ""):
        bits.append(f"Defender {_sb_fmt_level(defender.get('low'))}–{_sb_fmt_level(defender.get('high'))}")
    if challenger.get("low") not in (None, "") and challenger.get("high") not in (None, ""):
        bits.append(f"Challenger {_sb_fmt_level(challenger.get('low'))}–{_sb_fmt_level(challenger.get('high'))}")
    return " | ".join(bits) or "No nearby qualified FP zone."

def _sb_hidden_line(analysis: Dict[str, Any]) -> str:
    bits = []
    liq = analysis.get("liquidity") or {}
    if liq.get("meaning"):
        bits.append(str(liq.get("meaning")))
    elif liq.get("directional_read"):
        bits.append(f"liquidity read {str(liq.get('directional_read')).lower()}")
    of = str(analysis.get("exec_of", "") or "")
    if of:
        bits.append(f"OF {of.lower()}")
    aur = analysis.get("aurora") or {}
    if aur.get("state"):
        bits.append(f"AURORA {aur.get('state')}")
    fork = analysis.get("fork") or {}
    if fork.get("valid"):
        bits.append(f"Pitchfork {str(fork.get('slope','')).lower()} / {str(fork.get('position','')).replace('_',' ').lower()}")
    return "; ".join(bits[:4]) or "No material hidden-intelligence change is visible on this scan."


def _sb_campaign_expected(trade: Dict[str, Any], setup: Dict[str, Any]) -> str:
    tmd = _sb_json(trade.get("metadata_json", ""))
    if tmd.get("expected_behavior"):
        return str(tmd.get("expected_behavior"))
    if tmd.get("expected_behaviour"):
        return str(tmd.get("expected_behaviour"))
    smd = _sb_setup_md(setup)
    name = str(smd.get("setup_name") or trade.get("strategy_type") or "").upper()
    if name == "SHORT_CE_AFTER_DEMAND_BREAK":
        return "Broken demand should stay unreclaimed and downside travel should remain effective while the sold CE stays protected."
    if name == "SHORT_PE_AFTER_SUPPLY_BREAK":
        return "Broken supply should stay accepted above and upside travel should remain effective while the sold PE stays protected."
    direction = str(smd.get("direction") or trade.get("direction") or "").upper()
    if "LONG" in direction or direction == "BULLISH":
        return "Price should separate upward from the entry demand/location without persistent adverse pressure."
    if "SHORT" in direction or direction == "BEARISH":
        return "Price should separate downward from the entry supply/location without persistent adverse pressure."
    return str(trade.get("thesis", "") or "Campaign thesis should continue to hold.")


def _sb_campaign_management(trade: Dict[str, Any], setup: Dict[str, Any]) -> str:
    tmd = _sb_json(trade.get("metadata_json", ""))
    smd = _sb_setup_md(setup)
    family = str(tmd.get("management_family") or "").upper()
    setup_name = str(smd.get("setup_name") or tmd.get("origin_setup_name") or trade.get("strategy_type") or "").upper()
    if family == "FP_BREAK_OPTION_CAMPAIGN_MANAGEMENT" or setup_name in {"SHORT_CE_AFTER_DEMAND_BREAK", "SHORT_PE_AFTER_SUPPLY_BREAK"}:
        return "Adverse move: roll outward to the applicable 3rd-level short option and size only enough to recover accumulated loss. Recovery: roll inward again as the valid level improves."
    direction = str(smd.get("direction") or tmd.get("origin_setup_direction") or trade.get("direction") or "").upper()
    if "LONG" in direction or direction == "BULLISH":
        return "Watch for accepted loss of the locked demand/Defender/location. Location failure activates same-direction roll-out/recovery; active demand break activates the opposite-side CE at the 2nd level."
    if "SHORT" in direction or direction == "BEARISH":
        return "Watch for accepted loss of the locked supply/Challenger/location. Location failure activates same-direction roll-out/recovery; active supply break activates the opposite-side PE at the 2nd level."
    return "Watch the campaign's locked invalidation/location event; use current ODME before any roll or new leg."


def _sb_campaign_option_question(trade: Dict[str, Any], setup: Dict[str, Any]) -> str:
    tmd = _sb_json(trade.get("metadata_json", ""))
    smd = _sb_setup_md(setup)
    name = str(smd.get("setup_name") or tmd.get("origin_setup_name") or "").upper()
    original = str(smd.get("odme_requirement") or tmd.get("origin_setup_odme_requirement") or "").strip()
    if name == "SHORT_CE_AFTER_DEMAND_BREAK":
        return "Check bearish ODME support, CE writer defence, CE wall/POC migration, safer CE and whether 2nd/3rd-level protection still holds."
    if name == "SHORT_PE_AFTER_SUPPLY_BREAK":
        return "Check bullish ODME support, PE writer defence, PE wall/POC migration, safer PE and whether 2nd/3rd-level protection still holds."
    direction = str(smd.get("direction") or tmd.get("origin_setup_direction") or trade.get("direction") or "").upper()
    if "LONG" in direction or direction == "BULLISH":
        return ("Check that bullish option positioning has not materially deteriorated; PE writer defence, PE wall/POC migration and safer PE"
                + (f". Original requirement: {original}" if original else "."))
    if "SHORT" in direction or direction == "BEARISH":
        return ("Check that bearish option positioning has not materially deteriorated; CE writer defence, CE wall/POC migration and safer CE"
                + (f". Original requirement: {original}" if original else "."))
    return "Check current ODME positioning against the recorded campaign thesis."


def _sb_pending_setups(store: Any, instrument: str) -> List[Dict[str, Any]]:
    try:
        df = store.list_superbrain_setups(instrument=instrument, statuses=["PENDING_ENTRY", "BREAK_WATCH"])
    except Exception:
        return []
    if df is None or df.empty:
        return []
    rows = df.to_dict("records")
    rows.sort(key=lambda x: (1 if str(x.get("status", "")).upper() == "PENDING_ENTRY" else 0, str(x.get("updated_at", "") or x.get("created_at", ""))), reverse=True)
    return rows


def _sb_odme_score(odme: Dict[str, Any], nested_key: str, flat_key: str) -> float:
    scores = odme.get("scores") if isinstance(odme.get("scores"), dict) else {}
    value = scores.get(nested_key, odme.get(flat_key, 0))
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sb_odme_assessment(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Interpret ODME regime separately from directional support.

    A MIXED/RANGE headline must not erase a strong asymmetric options read.
    Direction is established from the directional score imbalance (with the
    explicit tilt retained as an override). Support strength is then upgraded
    when same-direction expansion risk is high. Control quality remains MIXED
    when ODME itself says there is no clean edge/control.
    """
    odme = _sb_odme_data(packet)
    tilt = str(_sb_first(odme, "odme_tilt", "tilt") or "").upper().strip()
    if not odme and not tilt:
        return {
            "regime": "UNKNOWN", "direction": "UNKNOWN", "strength": "NONE",
            "control_quality": "UNKNOWN", "expansion_direction": "NONE",
            "expansion_risk": "LOW", "preferred_expression": "NONE",
            "bullish_score": 0.0, "bearish_score": 0.0, "range_score": 0.0,
            "expansion_score": 0.0,
        }

    bull = _sb_odme_score(odme, "Bullish", "bullish_score")
    bear = _sb_odme_score(odme, "Bearish", "bearish_score")
    range_score = _sb_odme_score(odme, "Range", "range_score")
    expansion = _sb_odme_score(odme, "Expansion", "expansion_score")

    commentary = " ".join(str(x or "") for x in [
        _sb_first(odme, "commentary"),
        _sb_first(odme, "final_action", "hero_action"),
        _sb_first(odme, "ce_action"),
        _sb_first(odme, "pe_action"),
        _sb_first(odme, "premium_alert"),
    ]).lower()

    if "RANGE" in tilt:
        regime = "RANGE"
    elif "MIXED" in tilt or "NO CLEAN EDGE" in tilt:
        regime = "MIXED"
    elif "BULLISH" in tilt or "BEARISH" in tilt:
        regime = "DIRECTIONAL"
    else:
        regime = "UNKNOWN"

    explicit_bull = "BULLISH" in tilt and "BEARISH" not in tilt
    explicit_bear = "BEARISH" in tilt and "BULLISH" not in tilt
    if explicit_bull:
        direction = "BULLISH"
    elif explicit_bear:
        direction = "BEARISH"
    elif bear >= 55 and bear >= bull + 20:
        direction = "BEARISH"
    elif bull >= 55 and bull >= bear + 20:
        direction = "BULLISH"
    else:
        bearish_text = any(x in commentary for x in (
            "cleaner bearish condition", "downside expansion", "downside stress",
            "overhead pressure", "pe side is not clean support", "put writers covering",
        ))
        bullish_text = any(x in commentary for x in (
            "cleaner bullish condition", "upside expansion", "upside stress",
            "underlying support", "ce side is not clean resistance", "call writers covering",
        ))
        if bearish_text and not bullish_text:
            direction = "BEARISH"
        elif bullish_text and not bearish_text:
            direction = "BULLISH"
        else:
            direction = "NEUTRAL"

    if expansion >= 75:
        expansion_risk = "HIGH"
    elif expansion >= 55:
        expansion_risk = "ELEVATED"
    elif expansion >= 35:
        expansion_risk = "WATCH"
    else:
        expansion_risk = "LOW"

    if direction == "BEARISH":
        directional_score, opposing_score = bear, bull
        expansion_direction = "DOWNSIDE" if expansion >= 35 else "NONE"
        preferred_expression = "SELL CE"
        confirming_text = any(x in commentary for x in (
            "cleaner bearish condition", "ce pressure is valid", "ce selling is acceptable",
            "downside stress", "put writers covering", "pe side is not clean support",
        ))
    elif direction == "BULLISH":
        directional_score, opposing_score = bull, bear
        expansion_direction = "UPSIDE" if expansion >= 35 else "NONE"
        preferred_expression = "SELL PE"
        confirming_text = any(x in commentary for x in (
            "cleaner bullish condition", "pe pressure is valid", "pe selling is acceptable",
            "upside stress", "call writers covering", "ce side is not clean resistance",
        ))
    else:
        directional_score = opposing_score = 0.0
        expansion_direction = "NONE"
        preferred_expression = "NONE"
        confirming_text = False

    strength = "NONE"
    if direction in {"BULLISH", "BEARISH"}:
        # High same-direction expansion is STRONG support even when the headline
        # regime remains MIXED / NO CLEAN EDGE. Direction must already be clear.
        if (
            expansion >= 75
            and directional_score >= 70
            and directional_score >= opposing_score + 25
        ) or (
            directional_score >= 85
            and directional_score >= opposing_score + 30
            and confirming_text
        ):
            strength = "STRONG"
        else:
            strength = "WEAK"

    mixed_control_terms = (
        "no clean edge", "not clean bearish control", "not clean bullish control",
        "not clean control", "mixed",
    )
    control_quality = "MIXED" if regime in {"MIXED", "RANGE"} or any(x in commentary for x in mixed_control_terms) else "CLEAN"

    return {
        "regime": regime,
        "direction": direction,
        "strength": strength,
        "control_quality": control_quality,
        "expansion_direction": expansion_direction,
        "expansion_risk": expansion_risk,
        "preferred_expression": preferred_expression,
        "bullish_score": bull,
        "bearish_score": bear,
        "range_score": range_score,
        "expansion_score": expansion,
    }



def _sb_num(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        n = float(value)
        return n if n == n else None
    except Exception:
        return None


def _sb_level_zone(label: str, low: Any, high: Any = None, source: str = "", strength: str = "") -> Optional[Dict[str, Any]]:
    lo = _sb_num(low)
    hi = _sb_num(high if high is not None else low)
    if lo is None or hi is None:
        return None
    if lo > hi:
        lo, hi = hi, lo
    return {
        "low": lo,
        "high": hi,
        "labels": [str(label or source or "Level").strip()],
        "sources": [str(source or label or "LEVEL").strip()],
        "strengths": [str(strength or "").strip()] if str(strength or "").strip() else [],
    }


def _sb_merge_level_groups(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge only truly overlapping hurdles so confluence is not double-counted."""
    groups: List[Dict[str, Any]] = []
    for item in sorted(items, key=lambda x: (float(x["low"]), float(x["high"]))):
        current = {
            "low": float(item["low"]),
            "high": float(item["high"]),
            "labels": list(item.get("labels") or []),
            "sources": list(item.get("sources") or []),
            "strengths": list(item.get("strengths") or []),
        }
        if groups and current["low"] <= float(groups[-1]["high"]):
            hit = groups[-1]
            hit["low"] = min(float(hit["low"]), current["low"])
            hit["high"] = max(float(hit["high"]), current["high"])
            for key in ("labels", "sources", "strengths"):
                for value in current.get(key) or []:
                    if value and value not in hit[key]:
                        hit[key].append(value)
        else:
            groups.append(current)
    return groups


def _sb_tf_key(value: Any) -> str:
    s = str(value or "").strip().upper()
    if s.endswith("M") and s[:-1].replace(".", "", 1).isdigit():
        s = s[:-1]
    try:
        n = float(s)
        return str(int(n)) if n.is_integer() else str(n)
    except Exception:
        return s


def _sb_boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().upper() in {"TRUE", "YES", "Y", "1", "ACTIVE"}

def _sb_authoritative_edge_context(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Return the authoritative current EDGE row plus exact structural context.

    Prefer the ACTIVE EDGE row whose chart TF equals the required execution TF.
    Defender/Challenger and the one-sided FP book are read from that same row so
    an old handoff row cannot contaminate the final level map.
    """
    tv_rows = (packet or {}).get("tv_rows")
    if tv_rows is None:
        return {
            "available": False, "row": {}, "raw": {}, "price": None, "exec_tf": "",
            "paired": None, "defender": {}, "challenger": {}, "hurdles": [],
            "book_available": False, "one_sided_side": "", "eligibility": "",
            "warning": "", "context_note": "",
        }

    try:
        if hasattr(tv_rows, "to_dict"):
            rows = tv_rows.to_dict("records")
        elif isinstance(tv_rows, list):
            rows = [x for x in tv_rows if isinstance(x, dict)]
        else:
            rows = []
    except Exception:
        rows = []

    edge_rows = [r for r in rows if str((r or {}).get("source") or "").strip().upper() == "EDGE"]
    if not edge_rows:
        return {
            "available": False, "row": {}, "raw": {}, "price": None, "exec_tf": "",
            "paired": None, "defender": {}, "challenger": {}, "hurdles": [],
            "book_available": False, "one_sided_side": "", "eligibility": "",
            "warning": "", "context_note": "",
        }

    def row_score(row: Dict[str, Any]) -> tuple:
        tf = _sb_tf_key(row.get("tf"))
        req = _sb_tf_key(row.get("edge_required_exec_tf") or row.get("exec_tf"))
        tracking = str(row.get("edge_tracking_state") or "").strip().upper()
        raw = _sb_json(row.get("raw_json"))
        has_book = isinstance(raw.get("fp_hurdle_book"), dict)
        close_ms = _sb_num(row.get("bar_time")) or 0.0
        return (
            1 if tf and req and tf == req else 0,
            1 if tracking == "ACTIVE" else 0,
            1 if has_book else 0,
            close_ms,
        )

    row = sorted(edge_rows, key=row_score, reverse=True)[0]
    raw = _sb_json(row.get("raw_json"))
    book = raw.get("fp_hurdle_book") if isinstance(raw.get("fp_hurdle_book"), dict) else None
    exec_tf = _sb_tf_key(row.get("edge_required_exec_tf") or row.get("exec_tf") or row.get("tf"))
    price = _sb_num(row.get("close"))

    def role_zone(role: str) -> Dict[str, Any]:
        side = str(row.get(f"{role}_side") or "").strip().upper()
        state = str(row.get(f"{role}_state") or "").strip().upper()
        low = _sb_num(row.get(f"{role}_low"))
        high = _sb_num(row.get(f"{role}_high"))
        if side not in {"DEMAND", "SUPPLY"} or state == "NONE" or low is None or high is None:
            return {}
        if low > high:
            low, high = high, low
        # For hurdle ranking the strategic role contributes ONE exact distal
        # level. The whole EMA zone must not swallow FP/ODME levels inside it.
        distal = low if side == "DEMAND" else high
        proximal = high if side == "DEMAND" else low
        return {
            "role": role.upper(), "side": side, "state": state or "ACTIVE",
            "low": low, "high": high, "distal": distal, "proximal": proximal,
            "tf": row.get(f"{role}_tf"),
        }

    defender = role_zone("defender")
    challenger = role_zone("challenger")

    paired_raw = row.get("battlefield_paired")
    if paired_raw not in (None, ""):
        paired = _sb_boolish(paired_raw)
    else:
        # The typed sheet schema may not expose battlefield_paired. Infer only
        # from the actual current strategic roles; never invent the missing side.
        paired = bool(defender and challenger)

    hurdles = [x for x in ((book or {}).get("hurdles") or []) if isinstance(x, dict)]
    eligibility = str((book or {}).get("eligibility") or "")
    one_sided_capable = paired is True or "ONE_SIDED" in eligibility.upper()
    one_sided_side = ""
    if paired is False and defender.get("side") in {"DEMAND", "SUPPLY"}:
        one_sided_side = str(defender.get("side"))

    warning = ""
    if book is None:
        warning = "EDGE raw FP hurdle book is unavailable on the authoritative execution row."
    elif paired is False and not one_sided_capable:
        warning = (
            "EDGE terminal feed has not yet delivered the one-sided battlefield FP export — "
            "recreate/update the EDGE terminal alert before relying on FP ranking."
        )

    context_note = ""
    if one_sided_side and one_sided_capable:
        context_note = (
            f"One-sided battlefield: {one_sided_side} DEFENDER retained; opposite strategic side is missing. "
            "No synthetic opposite boundary or divider is used."
        )

    return {
        "available": True,
        "row": row,
        "raw": raw,
        "price": price,
        "exec_tf": exec_tf,
        "paired": paired,
        "defender": defender,
        "challenger": challenger,
        "hurdles": hurdles,
        "book_available": book is not None,
        "one_sided_side": one_sided_side,
        "one_sided_capable": one_sided_capable,
        "eligibility": eligibility,
        "demand_total": int(_sb_num((book or {}).get("demand_total")) or 0),
        "supply_total": int(_sb_num((book or {}).get("supply_total")) or 0),
        "warning": warning,
        "context_note": context_note,
    }


def _sb_authoritative_edge_hurdle_book(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility wrapper used by the final level ladder."""
    ctx = _sb_authoritative_edge_context(packet)
    if not ctx.get("available"):
        return {
            "available": False, "hurdles": [], "exec_tf": "", "lowest_tf": False,
            "paired": None, "one_sided_side": "", "one_sided_capable": False,
            "demand_total": 0, "supply_total": 0, "warning": "", "context_note": "",
            "eligibility": "",
        }
    try:
        tf_num = float(ctx.get("exec_tf") or "")
    except Exception:
        tf_num = None
    return {
        "available": bool(ctx.get("book_available")),
        "hurdles": list(ctx.get("hurdles") or []),
        "exec_tf": ctx.get("exec_tf", ""),
        "lowest_tf": tf_num is not None and tf_num <= 3,
        "paired": ctx.get("paired"),
        "one_sided_side": ctx.get("one_sided_side", ""),
        "one_sided_capable": bool(ctx.get("one_sided_capable")),
        "demand_total": ctx.get("demand_total", 0),
        "supply_total": ctx.get("supply_total", 0),
        "warning": ctx.get("warning", ""),
        "context_note": ctx.get("context_note", ""),
        "eligibility": ctx.get("eligibility", ""),
    }


def _sb_directional_level_item(item: Dict[str, Any], price: float, direction: str) -> Optional[Dict[str, Any]]:
    """Project one source hurdle into ABOVE or BELOW price.

    If price is inside an FP zone, DEMAND remains the current below-price support
    hurdle and SUPPLY remains the current above-price resistance hurdle. Thus an
    interacting zone is still visible as L1 instead of disappearing from ranking.
    """
    lo, hi = float(item["low"]), float(item["high"])
    semantic = str(item.get("semantic_side") or "").upper()
    d = str(direction or "").upper()

    if lo > price:
        actual = "ABOVE"
    elif hi < price:
        actual = "BELOW"
    else:
        if semantic == "DEMAND":
            actual = "BELOW"
        elif semantic == "SUPPLY":
            actual = "ABOVE"
        else:
            actual = d

    if actual != d:
        return None

    out = {
        "low": lo,
        "high": hi,
        "labels": list(item.get("labels") or []),
        "sources": list(item.get("sources") or []),
        "strengths": list(item.get("strengths") or []),
        "semantic_side": semantic,
        "kind": str(item.get("kind") or ("POINT" if abs(hi - lo) < 1e-12 else "ZONE")),
    }
    if d == "ABOVE":
        out["approach"] = price if lo <= price <= hi else lo
        out["far"] = hi
    else:
        out["approach"] = price if lo <= price <= hi else hi
        out["far"] = lo
    out["side"] = d
    return out


def _sb_directional_levels_confluent(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Merge true confluence without letting a broad zone swallow interior levels."""
    alo, ahi = float(a["low"]), float(a["high"])
    blo, bhi = float(b["low"]), float(b["high"])
    aa, ba = float(a["approach"]), float(b["approach"])
    aw, bw = max(0.0, ahi - alo), max(0.0, bhi - blo)
    eps = max(1e-9, max(abs(aa), abs(ba), 1.0) * 1e-10)

    if abs(aa - ba) <= eps:
        return True

    a_point = aw <= eps
    b_point = bw <= eps
    if a_point and b_point:
        return False

    # A point deep inside a broad zone is a later level, not the same first-contact
    # hurdle. Merge only when the point is near the zone's approach edge.
    if a_point != b_point:
        point = aa if a_point else ba
        zone = b if a_point else a
        zlo, zhi = float(zone["low"]), float(zone["high"])
        zw = max(zhi - zlo, eps)
        if zlo - eps <= point <= zhi + eps:
            return abs(point - float(zone["approach"])) <= max(eps, zw * 0.10)
        return False

    overlap = max(0.0, min(ahi, bhi) - max(alo, blo))
    if overlap <= eps:
        return False
    return abs(aa - ba) <= max(eps, min(aw, bw) * 0.50)


def _sb_merge_directional_levels(items: List[Dict[str, Any]], direction: str) -> List[Dict[str, Any]]:
    d = str(direction or "").upper()
    if d == "ABOVE":
        ordered = sorted(items, key=lambda x: (float(x["approach"]), float(x["far"])))
    else:
        ordered = sorted(items, key=lambda x: (-float(x["approach"]), -float(x["far"])))

    groups: List[Dict[str, Any]] = []
    for item in ordered:
        merged = False
        for g in reversed(groups[-2:]):
            if not _sb_directional_levels_confluent(g, item):
                continue
            g["low"] = min(float(g["low"]), float(item["low"]))
            g["high"] = max(float(g["high"]), float(item["high"]))
            if d == "ABOVE":
                g["approach"] = min(float(g["approach"]), float(item["approach"]))
                g["far"] = max(float(g["far"]), float(item["far"]))
            else:
                g["approach"] = max(float(g["approach"]), float(item["approach"]))
                g["far"] = min(float(g["far"]), float(item["far"]))
            for key in ("labels", "sources", "strengths"):
                for value in item.get(key) or []:
                    if value and value not in g[key]:
                        g[key].append(value)
            merged = True
            break
        if not merged:
            groups.append(dict(item))

    if d == "ABOVE":
        groups.sort(key=lambda x: (float(x["approach"]), float(x["far"])))
    else:
        groups.sort(key=lambda x: (-float(x["approach"]), -float(x["far"])))
    for idx, g in enumerate(groups, start=1):
        g["rank"] = idx
    return groups


def _sb_combined_level_ladder(packet: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Build one authoritative L1/L2/L3 ladder above and below current price.

    Every current FP hurdle, Defender, Challenger, ODME POC, walls and safer
    strikes is considered. Ranking itself is purely geometric; TV/ODME decide
    which rank a setup or campaign needs, not what L1/L2/L3 mean.
    """
    edge_ctx = _sb_authoritative_edge_context(packet)
    price = _sb_num(edge_ctx.get("price"))
    if price is None:
        price = _sb_num((analysis or {}).get("price"))
    if price is None:
        return {
            "price": None, "above": [], "below": [], "at_price": [],
            "fp_rank_reliable": False, "level_rank_reliable": False,
        }

    raw_items: List[Dict[str, Any]] = []

    fp_hurdles = list(edge_ctx.get("hurdles") or [])
    if not fp_hurdles and not edge_ctx.get("book_available"):
        fp_hurdles = list((analysis or {}).get("hurdles") or [])

    for h in fp_hurdles:
        side = str((h or {}).get("side") or "FP").upper()
        strength = str((h or {}).get("strength") or "").strip()
        z = _sb_level_zone(
            f"FP {side.title()}", (h or {}).get("low"), (h or {}).get("high"),
            source="FP", strength=strength,
        )
        if z:
            z["semantic_side"] = side
            z["kind"] = "ZONE"
            z["zone_id"] = (h or {}).get("zone_id")
            raw_items.append(z)

    # Defender/Challenger are exact distal hurdle levels, not their whole EMA zones.
    for role in ("defender", "challenger"):
        rz = edge_ctx.get(role) or {}
        side = str(rz.get("side") or "").upper()
        distal = _sb_num(rz.get("distal"))
        if distal is None:
            continue
        z = _sb_level_zone(
            role.title() + (f" {side.title()}" if side else ""),
            distal, source=role.upper(),
        )
        if z:
            z["semantic_side"] = side
            z["kind"] = "POINT"
            raw_items.append(z)

    odme = _sb_odme_data(packet)
    odme_points = [
        ("ODME POC", _sb_first(odme, "option_poc", "poc"), "POC", ""),
        ("CE Wall", _sb_first(odme, "active_ce_wall", "ce_wall"), "CE_WALL", "SUPPLY"),
        ("PE Wall", _sb_first(odme, "active_pe_wall", "pe_wall"), "PE_WALL", "DEMAND"),
        ("Safer CE", _sb_first(odme, "safer_sell_ce", "safe_ce"), "SAFER_CE", "SUPPLY"),
        ("Safer PE", _sb_first(odme, "safer_sell_pe", "safe_pe"), "SAFER_PE", "DEMAND"),
    ]
    for label, value, source, semantic in odme_points:
        z = _sb_level_zone(label, value, source=source)
        if z:
            z["semantic_side"] = semantic
            z["kind"] = "POINT"
            raw_items.append(z)

    above_items: List[Dict[str, Any]] = []
    below_items: List[Dict[str, Any]] = []
    at_price: List[Dict[str, Any]] = []
    for item in raw_items:
        lo, hi = float(item["low"]), float(item["high"])
        if lo <= price <= hi:
            at_price.append(dict(item))
        a = _sb_directional_level_item(item, price, "ABOVE")
        b = _sb_directional_level_item(item, price, "BELOW")
        if a:
            above_items.append(a)
        if b:
            below_items.append(b)

    above = _sb_merge_directional_levels(above_items, "ABOVE")
    below = _sb_merge_directional_levels(below_items, "BELOW")

    book_ok = bool(edge_ctx.get("book_available"))
    one_sided_ok = bool(edge_ctx.get("paired") is True or edge_ctx.get("one_sided_capable"))
    level_rank_reliable = bool(edge_ctx.get("available")) and book_ok and one_sided_ok
    if not edge_ctx.get("available"):
        level_rank_reliable = bool(raw_items)

    return {
        "price": price,
        "above": above,
        "below": below,
        "at_price": at_price,
        "raw_levels": raw_items,
        "fp_hurdles_considered": bool(edge_ctx.get("book_available")),
        "fp_rank_reliable": level_rank_reliable,
        "level_rank_reliable": level_rank_reliable,
        "fp_hurdle_source": "EDGE_RAW" if edge_ctx.get("book_available") else "ANALYSIS_FALLBACK",
        "exec_tf": edge_ctx.get("exec_tf", ""),
        "one_sided_side": edge_ctx.get("one_sided_side", ""),
        "paired": edge_ctx.get("paired"),
        "defender": edge_ctx.get("defender") or {},
        "challenger": edge_ctx.get("challenger") or {},
        "level_warning": edge_ctx.get("warning", ""),
        "context_note": edge_ctx.get("context_note", ""),
    }

def _sb_level_group_text(group: Optional[Dict[str, Any]], with_rank: bool = True) -> str:
    if not group:
        return "not available"
    lo = float(group.get("low"))
    hi = float(group.get("high"))
    rng = _sb_fmt_level(lo) if abs(hi - lo) < 1e-12 else f"{_sb_fmt_level(lo)}–{_sb_fmt_level(hi)}"
    labels = " + ".join(group.get("labels") or [])
    rank = f"L{group.get('rank')} " if with_rank and group.get("rank") else ""
    return f"{rank}{rng}" + (f" ({labels})" if labels else "")


def _sb_level_map_summary(level_map: Dict[str, Any]) -> str:
    above = level_map.get("above") or []
    below = level_map.get("below") or []
    def pick(seq: List[Dict[str, Any]], idx: int) -> Optional[Dict[str, Any]]:
        return seq[idx] if len(seq) > idx else None
    return (
        "Final level map — Above: L1 " + _sb_level_group_text(pick(above, 0), with_rank=False)
        + " | L2 " + _sb_level_group_text(pick(above, 1), with_rank=False)
        + " | L3 " + _sb_level_group_text(pick(above, 2), with_rank=False)
        + " · Below: L1 " + _sb_level_group_text(pick(below, 0), with_rank=False)
        + " | L2 " + _sb_level_group_text(pick(below, 1), with_rank=False)
        + " | L3 " + _sb_level_group_text(pick(below, 2), with_rank=False)
    )


def _sb_strike_relation(strike: float, ladder: List[Dict[str, Any]], option_type: str) -> Dict[str, Any]:
    """Locate a sold strike against the ordered structural hurdle ladder."""
    opt = str(option_type or "").upper()
    passed = 0
    inside_rank = None
    before_rank = None
    for g in ladder:
        lo, hi, rank = float(g["low"]), float(g["high"]), int(g.get("rank") or 0)
        if opt == "CE":
            if lo <= strike <= hi:
                inside_rank = rank
                break
            if strike > hi:
                passed = max(passed, rank)
                continue
            before_rank = rank
            break
        if opt == "PE":
            if lo <= strike <= hi:
                inside_rank = rank
                break
            if strike < lo:
                passed = max(passed, rank)
                continue
            before_rank = rank
            break
    if inside_rank:
        if inside_rank > 3:
            text = f"beyond L3 (within L{inside_rank})"
        else:
            text = f"within L{inside_rank}"
    elif passed >= 3:
        text = "beyond L3" + (f", before L{before_rank}" if before_rank and before_rank > 3 else "")
    elif passed:
        if before_rank:
            text = f"beyond L{passed}, before L{before_rank}"
        else:
            text = f"beyond L{passed}"
    elif before_rank:
        text = f"inside the path before L{before_rank}"
    else:
        text = "outside the mapped ladder"
    return {"passed": passed, "inside_rank": inside_rank, "before_rank": before_rank, "text": text}


def _sb_live_tv_required_rank(analysis: Dict[str, Any], option_type: str) -> Dict[str, Any]:
    """Dynamic protection tier from current TV conditions for a sold option campaign.

    L2 is allowed only when current execution OF supports the campaign direction and
    AURORA is not in the locked hard-danger colour. Otherwise TV requires L3.
    This does not create/validate an entry; it only sizes protection distance.
    """
    opt = str(option_type or "").upper()
    exec_of = str((analysis or {}).get("exec_of") or "NEUTRAL").upper()
    aura = str(((analysis or {}).get("aurora") or {}).get("state") or "UNKNOWN").upper()
    if opt == "CE":
        supports = "BEARISH" in exec_of
        hard_danger = aura == "GREEN"
        direction = "SHORT"
    elif opt == "PE":
        supports = "BULLISH" in exec_of
        hard_danger = aura == "RED"
        direction = "LONG"
    else:
        return {"rank": 3, "direction": "UNKNOWN", "supports": False, "hard_danger": False,
                "reason": "Unknown sold-option side requires conservative L3 protection."}
    rank = 2 if supports and not hard_danger else 3
    why = (
        f"TV allows L2 because execution OF supports the {direction} campaign and AURORA is not in hard danger."
        if rank == 2 else
        f"TV requires L3 because execution OF is not cleanly supportive for the {direction} campaign or AURORA is in hard danger."
    )
    return {"rank": rank, "direction": direction, "supports": supports, "hard_danger": hard_danger, "reason": why}


def _sb_setup_tv_required_rank(row: Dict[str, Any], option_type: str) -> Dict[str, Any]:
    """Protection tier encoded by the persisted Level-2 setup."""
    md = _sb_setup_md(row)
    explicit = str(md.get("option_level") or "").upper()
    decision = str(md.get("decision") or "").upper()
    of_rel = str(md.get("of_relationship") or "").upper()
    if "3RD_LEVEL" in explicit or "3RD_LEVEL" in decision:
        return {"rank": 3, "reason": "TV setup explicitly requires 3rd-level protection."}
    if "2ND_LEVEL" in explicit or "2ND_LEVEL" in decision:
        return {"rank": 2, "reason": "TV setup explicitly allows 2nd-level protection."}
    if of_rel == "SUPPORTS":
        return {"rank": 2, "reason": "TV setup has aligned execution OF, so L2 is acceptable if ODME also supports it."}
    return {"rank": 3, "reason": "TV setup is neutral/reversal/non-aligned, so use L3 protection."}


def _sb_odme_required_rank(option_type: str, assessment: Dict[str, Any]) -> Dict[str, Any]:
    """ODME protection posture after the entry/management gate has already been decided.

    Important: OPPOSING ODME is NOT an L3 entry permission. For a fresh campaign it
    blocks entry under the locked rulebook. For an ACTIVE campaign it is context
    used to choose a safer target only after a TV/level management trigger exists.
    """
    opt = str(option_type or "").upper()
    direction = str((assessment or {}).get("direction") or "NEUTRAL").upper()
    strength = str((assessment or {}).get("strength") or "NONE").upper()
    expected = "BEARISH" if opt == "CE" else "BULLISH" if opt == "PE" else "UNKNOWN"
    if direction == expected and strength == "STRONG":
        return {"rank": 2, "state": "STRONG_SUPPORT", "entry_allowed": True,
                "reason": f"ODME gives STRONG {expected} support, so L2 may be used when the TV rule also permits it."}
    if direction == expected:
        return {"rank": 3, "state": "WEAK_SUPPORT", "entry_allowed": True,
                "reason": f"ODME supports {expected} but not strongly; use the safer L3 unless the locked setup explicitly specifies its tier."}
    if direction in {"BULLISH", "BEARISH"} and direction != expected:
        return {"rank": 3, "state": "OPPOSING", "entry_allowed": False,
                "reason": f"ODME is {direction}, opposing the sold {opt}. Fresh entry is NO TRADE / WAIT; for an active campaign this does not by itself trigger a roll."}
    return {"rank": 3, "state": "NEUTRAL", "entry_allowed": True,
            "reason": "ODME is neutral/unclear; where neutral is allowed by the stored setup, use conservative L3 protection."}


def _sb_live_option_strikes(packet: Dict[str, Any]) -> List[float]:
    """Exact listed strikes from the fresh ODME chain; never synthesize/round strikes."""
    try:
        result = ((packet.get("odme_outcome") or {}).get("result") or {})
        table = result.get("strike_table")
        if table is not None and hasattr(table, "columns") and "strike" in table.columns:
            vals = pd.to_numeric(table["strike"], errors="coerce").dropna().tolist()
            return sorted({float(x) for x in vals})
    except Exception:
        pass
    return []


def _sb_exact_tradable_strike(packet: Dict[str, Any], option_type: str, boundary: Optional[float]) -> Optional[float]:
    """First actual listed strike beyond the exact structural boundary."""
    if boundary is None:
        return None
    strikes = _sb_live_option_strikes(packet)
    opt = str(option_type or "").upper()
    if opt == "CE":
        for x in strikes:
            if x >= float(boundary) - 1e-9:
                return x
    elif opt == "PE":
        for x in reversed(strikes):
            if x <= float(boundary) + 1e-9:
                return x
    return None


def _sb_new_campaign_decision(row: Dict[str, Any], packet: Dict[str, Any]) -> Dict[str, Any]:
    """Decisive pre-entry verdict: ODME gate first, then L2/L3 and exact strike.

    Locked rulebook order:
      1) TV setup must be ready.
      2) ODME must satisfy the setup gate. Material opposition is NO ENTRY / WAIT.
      3) Only after the gate passes do we choose L2/L3.
         - Explicit BRK-D*/BRK-S* 2ND/3RD tier from Level 2 is authoritative.
         - Otherwise combine the TV tier with ODME strength conservatively.
    """
    evaluation = _sb_evaluate_pending_setup(row, packet)
    analysis = packet.get("analysis") or {}
    md = _sb_setup_md(row)
    name = str(md.get("setup_name") or row.get("strategy_type") or "").upper()
    direction = str(md.get("direction") or row.get("direction") or "").upper()
    if "SHORT_CE" in name or "SHORT_CE" in direction:
        opt = "CE"
    elif "SHORT_PE" in name or "SHORT_PE" in direction:
        opt = "PE"
    elif "LONG" in direction or direction == "BULLISH" or name.startswith("LONG"):
        opt = "PE"
    elif "SHORT" in direction or direction == "BEARISH" or name.startswith("SHORT"):
        opt = "CE"
    else:
        opt = ""

    if evaluation.get("status") != "ENTRY READY" or opt not in {"CE", "PE"}:
        return {"status": evaluation.get("status", "WAIT"), "option_type": opt, "evaluation": evaluation}

    assessment = _sb_odme_assessment(packet)
    odme_req = _sb_odme_required_rank(opt, assessment)
    expected_odme = "BEARISH" if opt == "CE" else "BULLISH"
    odme_dir = str(assessment.get("direction") or "NEUTRAL").upper()
    odme_strength = str(assessment.get("strength") or "NONE").upper()
    req_text = str(md.get("odme_requirement") or "").upper()
    requires_support = (
        "SUPPORTS" in req_text
        or "REVERSAL" in name
        or "AFTER_DEMAND_BREAK" in name
        or "AFTER_SUPPLY_BREAK" in name
    )

    # Hard safety backstop: even if a stale/legacy setup record has a permissive
    # requirement, materially opposing ODME can never be converted into an L3 entry.
    if odme_dir in {"BULLISH", "BEARISH"} and odme_dir != expected_odme:
        return {
            "status": "WAIT", "option_type": opt, "evaluation": evaluation,
            "odme_state": "OPPOSING",
            "reason": f"NO ENTRY / WAIT — ODME is {odme_dir}, opposing this {name or opt} campaign. Wait for ODME to become neutral/supportive as required by the locked setup rule.",
        }
    if requires_support and not (odme_dir == expected_odme and odme_strength in {"WEAK", "STRONG"}):
        return {
            "status": "WAIT", "option_type": opt, "evaluation": evaluation,
            "odme_state": "NOT_SUPPORTIVE",
            "reason": f"NO ENTRY / WAIT — this setup requires {expected_odme} ODME support before a new {opt} campaign can be opened.",
        }

    ladder_map = _sb_combined_level_ladder(packet, analysis)
    ladder = ladder_map.get("above", []) if opt == "CE" else ladder_map.get("below", [])
    if not ladder_map.get("fp_rank_reliable"):
        return {
            "status": "WAIT", "option_type": opt, "evaluation": evaluation,
            "reason": "Entry gates are ready, but a reliable FP hurdle rank is not available; do not invent an L2/L3 strike.",
            "level_warning": ladder_map.get("level_warning", ""),
        }

    tv_req = _sb_setup_tv_required_rank(row, opt)
    explicit = str(md.get("option_level") or "").upper()
    stored_decision = str(md.get("decision") or "").upper()
    explicit_tier = ("2ND_LEVEL" in explicit or "3RD_LEVEL" in explicit or
                     "2ND_LEVEL" in stored_decision or "3RD_LEVEL" in stored_decision)

    # Break-option rules already encode the exact L2/L3 decision from TV/OF.
    # ODME is a pass/fail gate for those rows, not a second mechanism that can
    # silently turn BRK-D1/BRK-S1 (L2) into L3 merely because support is WEAK.
    if explicit_tier:
        required_rank = int(tv_req["rank"])
        tier_reason = f"The locked Level-2 setup explicitly requires L{required_rank}; ODME support gate is satisfied."
    else:
        required_rank = max(int(tv_req["rank"]), int(odme_req["rank"]))
        tier_reason = f"Final tier is L{required_rank}: TV requires L{tv_req['rank']} and ODME protection posture requires L{odme_req['rank']}."

    if len(ladder) < required_rank:
        return {
            "status": "WAIT", "option_type": opt, "evaluation": evaluation,
            "required_rank": required_rank, "tv_rank": tv_req["rank"], "odme_rank": odme_req["rank"],
            "reason": f"{tier_reason} That hurdle is not available in the current final map.",
        }

    group = ladder[required_rank - 1]
    boundary = float(group["far"])
    strike = _sb_exact_tradable_strike(packet, opt, boundary)
    odme = _sb_odme_data(packet)
    if strike is None:
        safer = _sb_num(_sb_first(odme, "safer_sell_ce", "safe_ce") if opt == "CE" else _sb_first(odme, "safer_sell_pe", "safe_pe"))
        if safer is not None and ((opt == "CE" and safer >= boundary - 1e-9) or (opt == "PE" and safer <= boundary + 1e-9)):
            strike = safer
    return {
        "status": "ENTRY READY",
        "option_type": opt,
        "required_rank": required_rank,
        "tv_rank": tv_req["rank"],
        "odme_rank": odme_req["rank"],
        "tv_reason": tv_req["reason"],
        "odme_reason": odme_req["reason"],
        "boundary": boundary,
        "strike": strike,
        "level": group,
        "evaluation": evaluation,
        "reason": tier_reason,
    }


def _sb_campaign_name(trade: Dict[str, Any], setup: Dict[str, Any]) -> str:
    tmd = _sb_json((trade or {}).get("metadata_json", ""))
    smd = _sb_setup_md(setup or {})
    return str(
        smd.get("setup_name")
        or tmd.get("origin_setup_name")
        or (trade or {}).get("strategy_type")
        or ""
    ).upper().strip()


def _sb_origin_fp_metadata(trade: Dict[str, Any], setup: Dict[str, Any]) -> Dict[str, Any]:
    tmd = _sb_json((trade or {}).get("metadata_json", ""))
    smd = _sb_setup_md(setup or {})
    fp = smd.get("fp") if isinstance(smd.get("fp"), dict) else None
    if not fp and isinstance(tmd.get("fp"), dict):
        fp = tmd.get("fp")
    return dict(fp or {})


def _sb_same_fp_identity(current: Dict[str, Any], origin: Dict[str, Any]) -> bool:
    if not current or not origin:
        return False
    cur_id = str(current.get("zone_id") or current.get("zoneId") or "").strip()
    org_id = str(origin.get("zone_id") or origin.get("zoneId") or "").strip()
    if cur_id and org_id and cur_id != org_id:
        return False
    cur_time = str(current.get("formation_time") or current.get("formationTime") or "").strip()
    org_time = str(origin.get("formation_time") or origin.get("formationTime") or "").strip()
    if cur_time and org_time and cur_time != org_time:
        return False
    return bool((cur_id and org_id) or (cur_time and org_time))


def _sb_origin_fp_live_state(
    packet: Dict[str, Any],
    trade: Dict[str, Any],
    setup: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Current status of the setup's immutable origin FP, using the live EDGE book."""
    origin = _sb_origin_fp_metadata(trade, setup)
    side = str(origin.get("side") or "").upper()
    lo = _sb_num(origin.get("low"))
    hi = _sb_num(origin.get("high"))
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo

    edge = _sb_authoritative_edge_context(packet)
    price = _sb_num(edge.get("price"))
    if price is None:
        price = _sb_num((analysis or {}).get("price"))

    matched = None
    for h in edge.get("hurdles") or []:
        if _sb_same_fp_identity(h, origin):
            matched = h
            break

    if matched:
        state = "PRESENT"
    elif not origin or side not in {"DEMAND", "SUPPLY"}:
        state = "UNKNOWN"
    else:
        state = "ABSENT"

    return {
        "state": state, "present": matched is not None, "current": matched or {},
        "origin": origin, "side": side, "low": lo, "high": hi, "price": price,
        "edge": edge,
    }


def _sb_break_thesis_state(
    packet: Dict[str, Any], trade: Dict[str, Any], setup: Dict[str, Any], analysis: Dict[str, Any]
) -> Dict[str, Any]:
    """Is the original FP break still intact, retesting, or reclaimed?"""
    name = _sb_campaign_name(trade, setup)
    ctx = _sb_origin_fp_live_state(packet, trade, setup, analysis)
    price, lo, hi = ctx.get("price"), ctx.get("low"), ctx.get("high")

    if name == "SHORT_CE_AFTER_DEMAND_BREAK":
        if ctx.get("present"):
            state = "FAILED_RECLAIMED"
            reason = "the original broken demand FP is active again"
        elif price is not None and lo is not None and hi is not None:
            if price < lo:
                state = "INTACT"
                reason = "price remains accepted below the original broken demand"
            elif price > hi:
                state = "FAILED_RECLAIMED"
                reason = "price has reclaimed above the original demand zone"
            else:
                state = "RETEST"
                reason = "price is retesting the original broken demand zone"
        else:
            state = "UNKNOWN"
            reason = "the original demand-break geometry cannot be resolved"
        expected = "BEARISH"
        opt = "CE"
    elif name == "SHORT_PE_AFTER_SUPPLY_BREAK":
        if ctx.get("present"):
            state = "FAILED_RECLAIMED"
            reason = "the original broken supply FP is active again"
        elif price is not None and lo is not None and hi is not None:
            if price > hi:
                state = "INTACT"
                reason = "price remains accepted above the original broken supply"
            elif price < lo:
                state = "FAILED_RECLAIMED"
                reason = "price has fallen back below the original supply zone"
            else:
                state = "RETEST"
                reason = "price is retesting the original broken supply zone"
        else:
            state = "UNKNOWN"
            reason = "the original supply-break geometry cannot be resolved"
        expected = "BULLISH"
        opt = "PE"
    else:
        return {**ctx, "state": "NOT_BREAK_CAMPAIGN", "reason": "", "expected": "", "option_type": ""}

    return {**ctx, "state": state, "reason": reason, "expected": expected, "option_type": opt}


def _sb_rank_target(
    packet: Dict[str, Any], ladder: List[Dict[str, Any]], option_type: str, rank: int
) -> Dict[str, Any]:
    group = ladder[rank - 1] if len(ladder) >= rank else None
    boundary = float(group["far"]) if group else None
    strike = _sb_exact_tradable_strike(packet, option_type, boundary) if boundary is not None else None
    if strike is None:
        strike = boundary
    return {"rank": rank, "group": group, "boundary": boundary, "strike": strike}


def _sb_campaign_companion_action(
    packet: Dict[str, Any], trade: Dict[str, Any], setup: Dict[str, Any], analysis: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Locked opposite-side management action for a failed primary demand/supply setup."""
    name = _sb_campaign_name(trade, setup)
    if name in {"SHORT_CE_AFTER_DEMAND_BREAK", "SHORT_PE_AFTER_SUPPLY_BREAK"}:
        return None

    origin = _sb_origin_fp_live_state(packet, trade, setup, analysis)
    if origin.get("state") != "ABSENT":
        return None

    ladder_map = _sb_combined_level_ladder(packet, analysis)
    if not ladder_map.get("level_rank_reliable"):
        return None

    direction = str(
        _sb_setup_md(setup).get("direction")
        or _sb_json((trade or {}).get("metadata_json", "")).get("origin_setup_direction")
        or (trade or {}).get("direction")
        or ""
    ).upper()
    side = str(origin.get("side") or "").upper()

    if side == "DEMAND" and ("LONG" in direction or direction == "BULLISH"):
        opt, ladder, event = "CE", ladder_map.get("above") or [], "ACTIVE_DEMAND_FAILED"
        rule = "Locked long-campaign demand failure: keep the campaign active and sell the opposite-side CE at the current L2 level."
    elif side == "SUPPLY" and ("SHORT" in direction or direction == "BEARISH"):
        opt, ladder, event = "PE", ladder_map.get("below") or [], "ACTIVE_SUPPLY_FAILED"
        rule = "Locked short-campaign supply failure: keep the campaign active and sell the opposite-side PE at the current L2 level."
    else:
        return None

    target = _sb_rank_target(packet, ladder, opt, 2)
    if not target.get("group"):
        return {
            "action": "WATCH", "option_type": opt, "rank": 2, "strike": None,
            "event": event, "reason": rule + " Current L2 is not available, so do not invent a strike.",
            "level": None,
        }
    return {
        "action": "ADD", "option_type": opt, "rank": 2, "strike": target.get("strike"),
        "boundary": target.get("boundary"), "event": event, "reason": rule,
        "level": target.get("group"),
    }


def _sb_campaign_final_decisions(packet: Dict[str, Any], trade: Dict[str, Any], setup: Dict[str, Any], analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decisive active-campaign management using the locked campaign event first.

    Critical ordering:
      1) Build the factual L1/L2/L3 map.
      2) Decide whether a LOCKED management event is actually active.
      3) Only then use L2/L3 and ODME to choose the target.

    A changed level map or opposing ODME never creates an outward roll by itself.
    """
    ladder_map = _sb_combined_level_ladder(packet, analysis)
    rank_reliable = bool(ladder_map.get("level_rank_reliable"))
    odme_a = _sb_odme_assessment(packet)
    odme_dir = str(odme_a.get("direction") or "NEUTRAL").upper()
    odme_strength = str(odme_a.get("strength") or "NONE").upper()
    odme_control = str(odme_a.get("control_quality") or "UNKNOWN").upper()
    odme = _sb_odme_data(packet)
    name = _sb_campaign_name(trade, setup)
    break_ctx = _sb_break_thesis_state(packet, trade, setup, analysis)
    origin_ctx = _sb_origin_fp_live_state(packet, trade, setup, analysis)
    exec_of = str((analysis or {}).get("exec_of") or "NEUTRAL").upper()
    aura = str(((analysis or {}).get("aurora") or {}).get("state") or "UNKNOWN").upper()
    decisions: List[Dict[str, Any]] = []

    for row in _sb_active_leg_rows(trade):
        leg = row.get("leg") or {}
        side = str(leg.get("side") or "").upper()
        opt = str(leg.get("option_type") or leg.get("option") or leg.get("instrument_type") or "").upper()
        strike = _sb_num(leg.get("strike"))
        if side != "SELL" or opt not in {"CE", "PE"} or strike is None:
            continue

        ladder = ladder_map.get("above", []) if opt == "CE" else ladder_map.get("below", [])
        relation = _sb_strike_relation(strike, ladder, opt) if ladder else {"text": "outside the mapped ladder", "passed": 0}
        l1 = ladder[0] if len(ladder) >= 1 else None
        l2 = ladder[1] if len(ladder) >= 2 else None
        l3 = ladder[2] if len(ladder) >= 3 else None
        expected_odme = "BEARISH" if opt == "CE" else "BULLISH"
        same_direction = odme_dir == expected_odme
        opposing = odme_dir in {"BULLISH", "BEARISH"} and odme_dir != expected_odme
        safer = _sb_num(
            _sb_first(odme, "safer_sell_ce", "safe_ce") if opt == "CE"
            else _sb_first(odme, "safer_sell_pe", "safe_pe")
        )
        odme_req = _sb_odme_required_rank(opt, odme_a)
        posture = str(odme_req.get("state") or "NEUTRAL")

        # ------------------------------------------------------------
        # FP BREAK OPTION CAMPAIGNS
        # ------------------------------------------------------------
        if name in {"SHORT_CE_AFTER_DEMAND_BREAK", "SHORT_PE_AFTER_SUPPLY_BREAK"}:
            expected = str(break_ctx.get("expected") or expected_odme)
            supports = ("BEARISH" in exec_of) if expected == "BEARISH" else ("BULLISH" in exec_of)
            hard_danger = aura == ("GREEN" if expected == "BEARISH" else "RED")
            break_state = str(break_ctx.get("state") or "UNKNOWN")

            # Only an actual break failure/reclaim, or a retest accompanied by a
            # full hard TV reversal, is an outward management trigger.
            adverse_trigger = break_state == "FAILED_RECLAIMED" or (
                break_state == "RETEST" and (not supports) and hard_danger
            )
            recovery_state = break_state == "INTACT" and supports and not hard_danger

            action = "HOLD"
            target = None
            target_label = ""
            management_trigger = "NONE"
            required_rank = 2 if recovery_state else 3 if adverse_trigger else None

            if adverse_trigger:
                management_trigger = "BREAK_FAILED_OR_HARD_REVERSAL"
                t = _sb_rank_target(packet, ladder, opt, 3) if rank_reliable else {"group": None, "strike": None}
                target = t.get("strike")
                target_label = "L3" if t.get("group") else ""

                # When the current L3 is unavailable, the ODME safer boundary is
                # the conservative fallback. Opposing ODME may also push a valid
                # L3 farther out, but only because the adverse trigger already exists.
                if safer is not None:
                    if target is None:
                        target, target_label = safer, f"ODME safer {opt}"
                    elif opposing and ((opt == "CE" and safer > target) or (opt == "PE" and safer < target)):
                        target, target_label = safer, f"ODME safer {opt} beyond L3"

                if target is not None:
                    underprotected = (opt == "CE" and strike < target - 1e-9) or (opt == "PE" and strike > target + 1e-9)
                    if underprotected:
                        action = "ROLL FARTHER"
                        reason = (
                            f"Adverse break-management event: {break_ctx.get('reason')}. "
                            f"Roll outward to {target_label or 'the safe outer level'}."
                        )
                    else:
                        reason = (
                            f"Adverse break-management event: {break_ctx.get('reason')}, but the current {opt} "
                            "already satisfies the outer protection target. Hold."
                        )
                else:
                    reason = (
                        f"Adverse break-management event: {break_ctx.get('reason')}, but no current L3/safer "
                        "strike is available. Hold rather than inventing a target."
                    )

            elif recovery_state:
                management_trigger = "RECOVERY_ON_PLAN"
                t = _sb_rank_target(packet, ladder, opt, 2) if rank_reliable else {"group": None, "strike": None}
                # Recovery permits an inward roll from >=L3 to L2 only when ODME
                # strongly supports the campaign. It never forces an outward roll
                # merely because today's L2 moved beyond the current strike.
                if same_direction and odme_strength == "STRONG" and relation.get("passed", 0) >= 3 and t.get("strike") is not None:
                    action = "ROLL IN"
                    target = t.get("strike")
                    target_label = "L2"
                    reason = (
                        f"Break thesis is on plan: {break_ctx.get('reason')}; OF and ODME support the campaign. "
                        "Recovery permits an inward roll from >=L3 to the current L2."
                    )
                else:
                    reason = (
                        f"Break thesis is on plan: {break_ctx.get('reason')}; current OF is supportive"
                        + (" and ODME is strongly supportive." if same_direction and odme_strength == "STRONG" else ".")
                        + " No adverse management trigger exists, so do not roll outward just because the level map changed."
                    )

            else:
                if break_state == "RETEST":
                    management_trigger = "BREAK_RETEST_WATCH"
                reason = (
                    f"No locked outward-management trigger is confirmed. {break_ctx.get('reason')}. "
                    "Hold and watch the original break/reclaim state; ODME alone does not create a roll."
                )

            decisions.append({
                "leg_id": row.get("leg_id"), "option_type": opt, "strike": strike,
                "action": action, "target": target, "target_label": target_label,
                "relation": relation.get("text"), "required_rank": required_rank,
                "tv_required_rank": required_rank, "odme_required_rank": odme_req.get("rank"),
                "management_trigger": management_trigger, "posture": posture, "reason": reason,
                "campaign_state": break_state, "campaign_state_reason": break_ctx.get("reason"),
                "odme_direction": odme_dir, "odme_strength": odme_strength, "odme_control": odme_control,
                "l1": l1, "l2": l2, "l3": l3, "ladder": ladder, "rank_reliable": rank_reliable,
                "level_warning": ladder_map.get("level_warning", ""),
            })
            continue

        # ------------------------------------------------------------
        # PRIMARY / OTHER CAMPAIGNS
        # ------------------------------------------------------------
        # Primary management is event-driven. A missing/broken origin demand or
        # supply activates the locked opposite-side L2 response; the existing sold
        # leg is not rolled merely because OF/AURORA or the numeric ladder changed.
        origin_failed = origin_ctx.get("state") == "ABSENT" and origin_ctx.get("side") in {"DEMAND", "SUPPLY"}
        if origin_failed:
            reason = (
                f"The originating {str(origin_ctx.get('side')).lower()} FP is no longer active. "
                "This is a locked structural management event; manage it with the opposite-side L2 action shown below. "
                f"Hold the existing {opt} unless a separate same-leg recovery rule is explicitly triggered."
            )
            trigger = f"ORIGIN_{str(origin_ctx.get('side')).upper()}_FAILED"
        else:
            reason = (
                "No locked structural management event is active for this campaign. "
                "Hold the current leg; ODME/OF/AURORA context may warn, but does not independently manufacture a roll."
            )
            trigger = "NONE"

        decisions.append({
            "leg_id": row.get("leg_id"), "option_type": opt, "strike": strike,
            "action": "HOLD", "target": None, "target_label": "",
            "relation": relation.get("text"), "required_rank": None,
            "tv_required_rank": None, "odme_required_rank": odme_req.get("rank"),
            "management_trigger": trigger, "posture": posture, "reason": reason,
            "campaign_state": "ORIGIN_FP_FAILED" if origin_failed else "ON_WATCH",
            "campaign_state_reason": reason,
            "odme_direction": odme_dir, "odme_strength": odme_strength, "odme_control": odme_control,
            "l1": l1, "l2": l2, "l3": l3, "ladder": ladder, "rank_reliable": rank_reliable,
            "level_warning": ladder_map.get("level_warning", ""),
        })

    return decisions

def _sb_campaign_decision_line(decision: Dict[str, Any]) -> str:
    action = str(decision.get("action") or "HOLD").upper()
    strike = _sb_fmt_level(decision.get("strike"))
    opt = str(decision.get("option_type") or "").upper()
    target = decision.get("target")
    if action in {"ROLL IN", "ROLL FARTHER"} and target is not None:
        return f"{action} — move {strike} {opt} to {decision.get('target_label')} {_sb_fmt_level(target)}."
    return f"HOLD {strike} {opt}."


def _sb_odme_bias(packet: Dict[str, Any]) -> str:
    # Compatibility wrapper: callers that only need direction still receive it,
    # but the direction now comes from the full ODME assessment rather than the
    # headline tilt alone.
    return str(_sb_odme_assessment(packet).get("direction") or "UNKNOWN")


def _sb_odme_label(packet: Dict[str, Any]) -> str:
    odme = _sb_odme_data(packet)
    return str(_sb_first(odme, "odme_tilt", "tilt") or "NO ODME READ").upper()


def _sb_odme_support_label(packet: Dict[str, Any]) -> str:
    a = _sb_odme_assessment(packet)
    direction = a.get("direction", "UNKNOWN")
    strength = a.get("strength", "NONE")
    if direction in {"BULLISH", "BEARISH"} and strength in {"STRONG", "WEAK"}:
        return f"{strength} {direction} SUPPORT"
    if direction == "NEUTRAL":
        return "NEUTRAL / NO DIRECTIONAL SUPPORT"
    return "ODME DIRECTION UNKNOWN"


def _sb_is_legacy_pitchfork_gate(condition: str) -> bool:
    u = str(condition or "").upper()
    return any(k in u for k in ("PITCHFORK", "PF_", "UPSLOPE", "DOWNSLOPE"))


def _sb_pitchfork_location_text(analysis: Dict[str, Any], direction: str) -> str:
    fork = analysis.get("fork") or {}
    if not fork.get("valid"):
        return "Pitchfork is unavailable; it does not block entry and provides no location guidance on this scan."

    slope = str(fork.get("slope") or "").upper()
    pos = str(fork.get("position") or "").upper()
    d = str(direction or "").upper()
    bullish = d in {"LONG", "BULLISH"}

    if pos == "ABOVE_UPPER_2SD":
        return "Pitchfork: price is structurally stretched up above +2SD. If the other entry gates align, entry is allowed without waiting for Pitchfork; carry a stretched-up warning."
    if pos == "BELOW_LOWER_2SD":
        return "Pitchfork: price is structurally stretched down below -2SD. If the other entry gates align, entry is allowed without waiting for Pitchfork; carry a stretched-down warning."

    if bullish:
        if slope == "DOWN":
            return "Pitchfork location: downslope. Preferred LONG location is a bullish reclaim of any Pitchfork level; the downslope itself does not block entry."
        if slope == "UP":
            return "Pitchfork location: upslope. Preferred LONG location is a retracement to any Pitchfork level; the upslope itself does not block entry."
        return "Pitchfork location is neutral/unclear; it does not block an otherwise valid LONG."

    if slope == "DOWN":
        return "Pitchfork location: downslope. Preferred SHORT location is a retracement to any Pitchfork level; the downslope itself does not block entry."
    if slope == "UP":
        return "Pitchfork location: upslope. Preferred SHORT location is a bearish rejection/loss of any Pitchfork level; the upslope itself does not block entry."
    return "Pitchfork location is neutral/unclear; it does not block an otherwise valid SHORT."


def _sb_live_tv_condition_text(condition: str, analysis: Dict[str, Any]) -> str:
    raw = str(condition or "").strip()
    upper = raw.upper()
    aurora = str(((analysis.get("aurora") or {}).get("state")) or "UNKNOWN").upper()
    fork = analysis.get("fork") or {}
    if fork.get("valid"):
        fork_now = f"{str(fork.get('slope', '') or '').upper()} / {str(fork.get('position', '') or '').replace('_', ' ').upper()}".strip(" /")
    else:
        fork_now = "NOT VALID"
    of_now = str(analysis.get("exec_of", "") or "NEUTRAL").upper()

    if "AURORA MUST IMPROVE TO" in upper:
        target = raw[upper.find("AURORA MUST IMPROVE TO") + len("AURORA MUST IMPROVE TO"):].strip()
        return f"AURORA is {aurora}; wait for {target}."
    if "AURORA" in upper and "IMPROVE" in upper:
        return f"AURORA is {aurora}; {raw}."
    if _sb_is_legacy_pitchfork_gate(raw):
        return f"Pitchfork is {fork_now}; this is location guidance only and is not an entry blocker."
    if "OPPOSING OF MUST FAIL" in upper or "OF FAILURE" in upper:
        return f"OF is {of_now}; the required failure-to-progress condition is not yet confirmed."
    return raw.rstrip(".") + "."


def _sb_odme_gate_text(requirement: str, packet: Dict[str, Any], satisfied: bool) -> str:
    req = str(requirement or "").upper().strip()
    label = _sb_odme_label(packet)
    a = _sb_odme_assessment(packet)
    direction = str(a.get("direction") or "UNKNOWN")
    strength = str(a.get("strength") or "NONE")
    control = str(a.get("control_quality") or "UNKNOWN")
    expansion_dir = str(a.get("expansion_direction") or "NONE")
    expansion_risk = str(a.get("expansion_risk") or "LOW")
    expression = str(a.get("preferred_expression") or "NONE")

    if direction == "UNKNOWN":
        return f"ODME is {label}; the ODME gate cannot be confirmed on this scan."

    support_phrase = ""
    if direction in {"BULLISH", "BEARISH"}:
        support_phrase = f"{strength.lower()} {direction.lower()} support" if strength in {"STRONG", "WEAK"} else f"{direction.lower()} direction"
    else:
        support_phrase = "neutral / no directional support"

    extras = []
    if expansion_dir != "NONE" and expansion_risk in {"HIGH", "ELEVATED"}:
        extras.append(f"{expansion_dir.lower()} expansion risk is {expansion_risk.lower()}")
    if expression != "NONE":
        extras.append(f"{expression} preferred")
    extras.append(f"{control.lower()} control")
    context = "; ".join(extras)

    if not req:
        return f"ODME: {support_phrase}; {context}. No additional ODME gate is required."
    if "LONG_NOT_OPPOSING" in req:
        return (f"ODME: {support_phrase}; {context}. LONG non-opposition gate is satisfied."
                if satisfied else f"ODME: {support_phrase}; {context}. LONG non-opposition gate is not satisfied because ODME is bearish.")
    if "SHORT_NOT_OPPOSING" in req:
        return (f"ODME: {support_phrase}; {context}. SHORT non-opposition gate is satisfied."
                if satisfied else f"ODME: {support_phrase}; {context}. SHORT non-opposition gate is not satisfied because ODME is bullish.")
    if "LONG_SUPPORTS" in req or "BULLISH ODME SUPPORT" in req:
        return (f"ODME: {support_phrase}; {context}. LONG_SUPPORTS is satisfied."
                if satisfied else f"ODME: {support_phrase}; {context}. LONG_SUPPORTS is not satisfied.")
    if "SHORT_SUPPORTS" in req or "BEARISH ODME SUPPORT" in req:
        return (f"ODME: {support_phrase}; {context}. SHORT_SUPPORTS is satisfied."
                if satisfied else f"ODME: {support_phrase}; {context}. SHORT_SUPPORTS is not satisfied.")
    return (f"ODME: {support_phrase}; {context}. The stored ODME requirement is satisfied."
            if satisfied else f"ODME: {support_phrase}; {context}. The stored ODME requirement ({requirement}) is not positively satisfied by this scan.")


def _sb_evaluate_pending_setup(row: Dict[str, Any], packet: Dict[str, Any]) -> Dict[str, Any]:
    md = _sb_setup_md(row)
    req_raw = str(md.get("odme_requirement") or "").strip()
    req = req_raw.upper()
    raw_remaining = [str(x) for x in (md.get("remaining_conditions") or []) if str(x).strip() and str(x).strip() != "ONLY ODME CHECK REMAINS"]
    # Legacy pending records may still carry old Pitchfork gate text. Pitchfork is
    # now location-only and must never keep ENTRY READY on WAIT by itself.
    remaining = [x for x in raw_remaining if not _sb_is_legacy_pitchfork_gate(x)]
    analysis = packet.get("analysis") or {}
    assessment = _sb_odme_assessment(packet)
    bias = str(assessment.get("direction") or "UNKNOWN")
    support_strength = str(assessment.get("strength") or "NONE")

    satisfied = False
    if "LONG_NOT_OPPOSING" in req:
        satisfied = bias in {"BULLISH", "NEUTRAL"}
    elif "SHORT_NOT_OPPOSING" in req:
        satisfied = bias in {"BEARISH", "NEUTRAL"}
    elif "LONG_SUPPORTS" in req or "BULLISH ODME SUPPORT" in req:
        # Both WEAK and STRONG same-direction support satisfy the support gate.
        satisfied = bias == "BULLISH" and support_strength in {"WEAK", "STRONG"}
    elif "SHORT_SUPPORTS" in req or "BEARISH ODME SUPPORT" in req:
        # High downside expansion can produce STRONG bearish support even when
        # the ODME regime remains MIXED / NO CLEAN EDGE.
        satisfied = bias == "BEARISH" and support_strength in {"WEAK", "STRONG"}
    elif not req:
        satisfied = True

    tv_details = [_sb_live_tv_condition_text(x, analysis) for x in remaining]
    odme_detail = _sb_odme_gate_text(req_raw, packet, satisfied)
    pf_detail = _sb_pitchfork_location_text(analysis, md.get("direction") or row.get("direction"))
    status = "ENTRY READY" if not remaining and satisfied else "WAIT"
    parts = []
    if tv_details:
        parts.extend(tv_details)
    else:
        parts.append("TV directional/timing conditions are satisfied on this scan.")
    parts.append(odme_detail)
    parts.append(pf_detail)
    reason = " ".join(x for x in parts if x).strip()
    return {
        "status": status,
        "reason": reason,
        "odme_bias": bias,
        "odme_strength": support_strength,
        "odme_assessment": assessment,
        "odme_label": _sb_odme_label(packet),
        "odme_satisfied": satisfied,
        "tv_satisfied": not remaining,
        "tv_details": tv_details,
        "odme_detail": odme_detail,
        "pitchfork_detail": pf_detail,
        "requirement": req_raw,
    }


def _sb_pending_event_text(row: Dict[str, Any]) -> Dict[str, str]:
    md = _sb_setup_md(row)
    name = str(md.get("setup_name") or row.get("strategy_type") or "Setup")
    remaining = md.get("remaining_conditions") if isinstance(md.get("remaining_conditions"), list) else []
    remaining = [x for x in remaining if str(x).strip() and str(x).strip() != "ONLY ODME CHECK REMAINS" and not _sb_is_legacy_pitchfork_gate(str(x))]
    odme = str(md.get("odme_requirement") or "").strip()
    status = str(row.get("status", "") or "").upper()
    if status == "BREAK_WATCH":
        return {
            "event": f"{name} break-watch",
            "look": "Wait for an accepted FP break; if it occurs, ODME must support the break direction before the opposite-side short-option campaign is eligible.",
            "odme": odme or "ODME confirmation is required only after the break."
        }
    look = " | ".join(str(x) for x in remaining if str(x).strip()) or "TV conditions are ready; check ODME now."
    return {"event": name, "look": look, "odme": odme or "Check ODME against the setup requirement."}


def _sb_next_market_event(analysis: Dict[str, Any], packet: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    price = analysis.get("price")
    hurdles = analysis.get("hurdles") or []
    edge_book = {}
    if not hurdles and packet:
        edge_book = _sb_authoritative_edge_hurdle_book(packet)
        hurdles = [
            h for h in (edge_book.get("hurdles") or [])
            if "STRONG" in str((h or {}).get("strength") or "").upper()
            and (_sb_num((h or {}).get("hold")) or 0) >= 70
        ]
    valid = []
    for h in hurdles:
        try:
            lo, hi = float(h.get("low")), float(h.get("high"))
            p = float(price)
            dist = 0 if lo <= p <= hi else min(abs(p-lo), abs(p-hi))
            valid.append((dist, h))
        except Exception:
            continue
    if valid:
        _, h = sorted(valid, key=lambda x: x[0])[0]
        side = str(h.get("side", "") or "").upper()
        strength = str(h.get("strength", "") or "Strong").strip()
        rng = f"{_sb_fmt_level(h.get('low'))}–{_sb_fmt_level(h.get('high'))}"
        # analysis.hurdles contains only EDGE entry-qualified FP zones. Therefore
        # strength + correct battlefield-side qualification are already known facts;
        # Level 3 should state them, not ask the user to re-check them manually.
        one_side = str(edge_book.get("one_sided_side") or "").upper() if edge_book else ""
        if side == "DEMAND":
            if one_side:
                if one_side == "DEMAND":
                    look = f"This {strength} demand zone is inside the surviving one-sided DEMAND battlefield and can follow the normal long HOLD path; accepted break below shifts to the CE break campaign."
                else:
                    look = f"This {strength} demand zone is retained as a one-sided battlefield hurdle, but the surviving DEFENDER is SUPPLY; treat it as BREAK-WATCH only. Accepted break below can still shift to the CE break campaign."
                return {"event": f"Demand interaction around {rng}", "look": look}
            return {
                "event": f"Demand interaction around {rng}",
                "look": f"This {strength} demand zone is already qualified in the lower battlefield half. On interaction, WAIT keeps the normal long path alive; accepted break below shifts to the CE break campaign. OF, AURORA and ODME are evaluated automatically; Pitchfork is location/stretch guidance only."
            }
        if side == "SUPPLY":
            if one_side:
                if one_side == "SUPPLY":
                    look = f"This {strength} supply zone is inside the surviving one-sided SUPPLY battlefield and can follow the normal short HOLD path; accepted break above shifts to the PE break campaign."
                else:
                    look = f"This {strength} supply zone is retained as a one-sided battlefield hurdle, but the surviving DEFENDER is DEMAND; treat it as BREAK-WATCH only. Accepted break above can still shift to the PE break campaign."
                return {"event": f"Supply interaction around {rng}", "look": look}
            return {
                "event": f"Supply interaction around {rng}",
                "look": f"This {strength} supply zone is already qualified in the upper battlefield half. On interaction, WAIT keeps the normal short path alive; accepted break above shifts to the PE break campaign. OF, AURORA and ODME are evaluated automatically; Pitchfork is location/stretch guidance only."
            }
    waits = analysis.get("waiting_for") or []
    if waits:
        return {"event": "Next confirmation", "look": str(waits[0])}
    return {"event": "No immediate setup", "look": "Wait for a qualified strong FP interaction or an accepted break-watch event."}


def _sb_infer_entry_contract(row: Dict[str, Any]) -> Dict[str, str]:
    """Infer only the contract family/side from the persisted setup; never invent a strike."""
    md = _sb_setup_md(row)
    hay = " ".join(
        str(x or "").upper()
        for x in (
            md.get("setup_name"), row.get("strategy_type"), md.get("direction"), row.get("direction")
        )
    )
    if "SHORT_CE" in hay or "SELL_CE" in hay:
        return {"side": "SELL", "contract_type": "CE"}
    if "SHORT_PE" in hay or "SELL_PE" in hay:
        return {"side": "SELL", "contract_type": "PE"}
    if "LONG_CE" in hay or "BUY_CE" in hay:
        return {"side": "BUY", "contract_type": "CE"}
    if "LONG_PE" in hay or "BUY_PE" in hay:
        return {"side": "BUY", "contract_type": "PE"}
    # Directional setup with no explicit derivative family: do not guess a strike.
    if "LONG" in hay or "BULLISH" in hay:
        return {"side": "BUY", "contract_type": "OTHER"}
    if "SHORT" in hay or "BEARISH" in hay:
        return {"side": "SELL", "contract_type": "OTHER"}
    return {"side": "SELL", "contract_type": "OTHER"}


def _sb_first_plan_leg(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort prefill from a fresh structured plan, without making it authoritative."""
    if not isinstance(plan, dict):
        return {}
    for key in ("legs", "trade_legs", "option_legs"):
        value = plan.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return dict(value[0])
        if isinstance(value, dict):
            return dict(value)
    raw = plan.get("legs_json")
    legs = _sb_list_json(raw)
    if legs:
        return dict(legs[0])
    for key in ("leg", "option_leg", "entry_leg"):
        value = plan.get(key)
        if isinstance(value, dict):
            return dict(value)
    direct = {}
    for key in ("side", "option_type", "option", "instrument_type", "strike", "expiry", "quantity", "qty", "lots", "size"):
        if plan.get(key) not in (None, ""):
            direct[key] = plan.get(key)
    return direct


def _sb_record_selected_pending_entry(
    store: Any,
    packet: Dict[str, Any],
    setup_row: Dict[str, Any],
    evaluation: Dict[str, Any],
    side: str,
    contract_type: str,
    strike: Any,
    expiry: str,
    quantity: Any,
    note: str = "",
) -> Dict[str, Any]:
    """Create an ACTIVE campaign directly from the selected persisted PENDING_ENTRY.

    The persisted setup is the authority. This intentionally does not require the
    current scan to expose trade_plan.kind == NEW; that old bridge gate is what
    prevented legitimate user-confirmed pending/WAIT entries from being recorded.
    """
    row = dict(setup_row or {})
    rid = str(row.get("record_id", "") or "").strip()
    instrument = str(row.get("instrument") or packet.get("instrument") or "").upper().strip()
    if not rid:
        raise ValueError("Selected setup has no record_id.")
    if not instrument:
        raise ValueError("Selected setup has no instrument.")

    # Never create a duplicate active campaign for the same setup, even if the
    # browser packet is stale after a previous successful write.
    try:
        existing = store.list_superbrain_trades(instrument=instrument, open_only=True)
    except Exception:
        existing = pd.DataFrame()
    if existing is not None and not existing.empty:
        for old in existing.to_dict("records"):
            omd = _sb_json(old.get("metadata_json", ""))
            parent = str(
                omd.get("parent_setup_id") or omd.get("origin_setup_record_id")
                or omd.get("setup_record_id") or ""
            ).strip()
            if parent == rid:
                return old

    side_u = str(side or "").upper().strip()
    ctype = str(contract_type or "").upper().strip()
    if not side_u:
        raise ValueError("Select whether the executed leg was BUY or SELL.")
    if not ctype:
        raise ValueError("Select the executed contract type.")
    if ctype in {"CE", "PE"} and not str(strike or "").strip():
        raise ValueError("Enter the option strike you actually executed.")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    trade_id = f"SB-{uuid.uuid4().hex[:12].upper()}"
    md = _sb_setup_md(row)
    setup_name = str(md.get("setup_name") or row.get("strategy_type") or "Campaign").strip()
    setup_direction = str(md.get("direction") or row.get("direction") or "").strip()
    readiness = str((evaluation or {}).get("status") or "PENDING").upper().strip()
    is_manual_override = readiness != "ENTRY READY"

    leg = {
        "leg_id": f"{trade_id}-E1",
        "status": "ACTIVE",
        "opened_at": now,
        "source": "USER_CONFIRMED_ENTRY",
        "side": side_u,
        "option_type": ctype,
        "strike": _sb_management_value(strike),
        "expiry": str(expiry or "").strip(),
        "quantity": _sb_management_value(quantity),
    }

    management_family = (
        "FP_BREAK_OPTION_CAMPAIGN_MANAGEMENT"
        if setup_name.upper() in {"SHORT_CE_AFTER_DEMAND_BREAK", "SHORT_PE_AFTER_SUPPLY_BREAK"}
        else "PRIMARY_CAMPAIGN_MANAGEMENT"
    )
    tmd = dict(md)
    tmd.update({
        "memory_schema": "SB_TRADE_USER_CONFIRMED_2",
        "parent_setup_id": rid,
        "origin_setup_record_id": rid,
        "setup_record_id": rid,
        "origin_setup_name": setup_name,
        "origin_setup_direction": setup_direction,
        "origin_setup_odme_requirement": md.get("odme_requirement", ""),
        "entry_recorded_at": now,
        "entry_recorded_by_user": True,
        "entry_readiness_at_recording": readiness,
        "manual_entry_override": bool(is_manual_override),
        "manual_entry_note": str(note or "").strip(),
        "management_family": management_family,
        "management_revision": 0,
        "current_exposure_source": "USER_CONFIRMED_ENTRY",
    })

    analysis = packet.get("analysis", {}) or {}
    memory = packet.get("memory", {}) or {}
    trade = {
        "instrument": instrument,
        "trade_id": trade_id,
        "status": "ACTIVE",
        "strategy_type": row.get("strategy_type") or setup_name,
        "direction": row.get("direction") or setup_direction,
        "created_at": now,
        "updated_at": now,
        "closed_at": "",
        "scan_id": memory.get("scan_id") or analysis.get("scan_id") or row.get("scan_id", ""),
        "previous_scan_id": memory.get("previous_scan_id") or row.get("previous_scan_id", ""),
        "mode": packet.get("mode") or row.get("mode") or "USER_CONFIRMED",
        "action": "TRADE_TAKEN",
        "thesis": row.get("thesis") or setup_name,
        "entry_reference": row.get("entry_reference", ""),
        "target": row.get("target", ""),
        "invalidation": row.get("invalidation", ""),
        "expected_eta": row.get("expected_eta", ""),
        "risk": row.get("risk", ""),
        "reward": row.get("reward", ""),
        "rr": row.get("rr", ""),
        "legs_json": json.dumps([leg], separators=(",", ":"), default=str),
        "market_state_json": json.dumps({
            "price": analysis.get("price"),
            "macro": analysis.get("macro"),
            "exec_of": analysis.get("exec_of"),
            "battlefield_half": analysis.get("battlefield_half"),
            "recorded_from_scan": memory.get("scan_id") or analysis.get("scan_id") or "",
        }, separators=(",", ":"), default=str),
        "previous_state_json": "",
        "metadata_json": json.dumps(tmd, separators=(",", ":"), default=str),
        "close_reason": "",
    }
    saved = store.upsert_superbrain_trade(trade)

    # Newer stores may expose a setup-upsert API. Mark the setup entered when
    # available, but never make campaign recording depend on that optional API.
    setup_upsert = getattr(store, "upsert_superbrain_setup", None)
    if callable(setup_upsert):
        try:
            updated_setup = dict(row)
            updated_setup["status"] = "ENTERED"
            updated_setup["trade_id"] = str(saved.get("trade_id") or trade_id)
            updated_setup["updated_at"] = now
            smd = _sb_setup_md(updated_setup)
            smd["entered_at"] = now
            smd["entered_trade_id"] = str(saved.get("trade_id") or trade_id)
            updated_setup["metadata_json"] = json.dumps(smd, separators=(",", ":"), default=str)
            setup_upsert(updated_setup)
        except Exception:
            pass

    return saved


def _sb_render_trade_confirmation(store: Any, instrument: str, packet: Dict[str, Any]) -> None:
    """Render user-confirmed campaign recording for every persisted PENDING_ENTRY."""
    analysis = packet.get("analysis", {}) or {}
    plan = analysis.get("trade_plan", {}) or {}

    # Include persisted active trades as well as the current packet so a stale
    # browser session cannot offer an already-entered setup twice.
    linked_setup_ids = set()
    all_open = list(packet.get("open_trades") or [])
    try:
        persisted = store.list_superbrain_trades(instrument=instrument, open_only=True)
        if persisted is not None and not persisted.empty:
            all_open.extend(persisted.to_dict("records"))
    except Exception:
        pass
    for trade in all_open:
        tmd = _sb_json((trade or {}).get("metadata_json", ""))
        for key in ("parent_setup_id", "origin_setup_record_id", "setup_record_id"):
            rid = str(tmd.get(key) or "").strip()
            if rid:
                linked_setup_ids.add(rid)

    pending_rows = [
        row for row in _sb_pending_setups(store, instrument)
        if str(row.get("status", "") or "").upper() == "PENDING_ENTRY"
        and str(row.get("record_id", "") or "").strip()
        and str(row.get("record_id", "") or "").strip() not in linked_setup_ids
    ]
    if not pending_rows:
        return

    plan_setup_id = ""
    if str(plan.get("kind", "") or "").upper() == "NEW":
        for key in ("setup_record_id", "record_id", "parent_setup_id", "origin_setup_record_id"):
            candidate = str(plan.get(key) or "").strip()
            if candidate:
                plan_setup_id = candidate
                break

    choices = []
    for row in pending_rows:
        rid = str(row.get("record_id", "") or "").strip()
        md = _sb_setup_md(row)
        evaluation = _sb_evaluate_pending_setup(row, packet)
        readiness = str(evaluation.get("status", "PENDING") or "PENDING").upper()
        setup_name = str(md.get("setup_name") or row.get("strategy_type") or "Setup").strip()
        direction = str(md.get("direction") or row.get("direction") or "").upper().strip()
        entry_ref = row.get("entry_reference")
        label_bits = []
        if rid == plan_setup_id:
            label_bits.append("CURRENT NEW")
        label_bits.extend([readiness, setup_name])
        if direction:
            label_bits.append(direction)
        if entry_ref not in (None, ""):
            label_bits.append(f"Entry {entry_ref}")
        choices.append({
            "record_id": rid,
            "row": row,
            "evaluation": evaluation,
            "label": " · ".join(str(x) for x in label_bits if str(x).strip()),
        })

    choices.sort(key=lambda x: 0 if x["evaluation"].get("status") == "ENTRY READY" else 1)

    st.markdown("### Record trade taken")
    selected = choices[0]
    if len(choices) > 1:
        selected_label = st.selectbox(
            "Which setup did you execute?",
            [x["label"] for x in choices],
            key=f"pending_setup_choice_{instrument}",
        )
        selected = next(x for x in choices if x["label"] == selected_label)
    else:
        st.caption(selected["label"])

    evaluation = selected["evaluation"]
    new_decision = _sb_new_campaign_decision(selected["row"], packet) if str(evaluation.get("status", "")).upper() == "ENTRY READY" else {}
    if str(evaluation.get("status", "")).upper() == "ENTRY READY":
        if new_decision.get("status") == "ENTRY READY":
            _nd_opt = new_decision.get("option_type")
            _nd_rank = new_decision.get("required_rank")
            _nd_strike = new_decision.get("strike")
            if _nd_strike is not None:
                st.success(f"Selected setup is ENTRY READY — recommended SELL {_sb_fmt_level(_nd_strike)} {_nd_opt} at L{_nd_rank} protection.")
            else:
                st.success(f"Selected setup is ENTRY READY — L{_nd_rank} {_nd_opt} protection is required; exact listed strike is not available in the live chain packet.")
        else:
            st.warning(f"TV/ODME entry is ready, but final strike selection is not complete: {new_decision.get('reason','')}")
    else:
        st.warning(
            "Selected setup is still WAIT in SuperBrain. If you took it anyway, record the exact exposure you actually executed."
        )
        reason = str(evaluation.get("reason", "") or "").strip()
        if reason:
            st.caption(reason)

    defaults = _sb_infer_entry_contract(selected["row"])
    plan_leg = _sb_first_plan_leg(plan) if (
        str(plan.get("kind", "") or "").upper() == "NEW"
        and (not plan_setup_id or plan_setup_id == selected["record_id"])
    ) else {}
    default_side = str(("SELL" if new_decision.get("option_type") in {"CE", "PE"} else "") or plan_leg.get("side") or defaults["side"] or "SELL").upper()
    default_type = str(
        new_decision.get("option_type") or plan_leg.get("option_type") or plan_leg.get("option") or plan_leg.get("instrument_type")
        or defaults["contract_type"] or "OTHER"
    ).upper()
    default_strike = str(new_decision.get("strike") if new_decision.get("strike") is not None else (plan_leg.get("strike", "") or ""))
    default_expiry = str(
        plan_leg.get("expiry")
        or (packet.get("mapping") or {}).get("selected_expiry")
        or _sb_first(_sb_odme_data(packet), "expiry")
        or ""
    )
    default_qty = str(
        plan_leg.get("quantity", plan_leg.get("qty", plan_leg.get("lots", plan_leg.get("size", ""))))
        or ""
    )

    side_options = list(dict.fromkeys([default_side, "SELL", "BUY"]))
    type_options = list(dict.fromkeys([default_type, "CE", "PE", "FUT", "CASH", "OTHER"]))

    # Use normal widgets + button rather than a Streamlit form here. On mobile,
    # a failed form submit could leave the error below the fold and look like the
    # button did nothing. Normal widgets keep their keyed values and make the
    # record action explicit on every rerun.
    st.caption("Record the position actually executed. This becomes the campaign exposure SuperBrain manages.")
    backend_name = type(store).__name__
    st.caption(f"Storage backend: {backend_name}")
    side = st.selectbox("Side", side_options, key=f"entry_side_{instrument}_{selected['record_id']}")
    contract_type = st.selectbox(
        "Contract type", type_options, key=f"entry_type_{instrument}_{selected['record_id']}"
    )
    strike = st.text_input(
        "Executed strike / contract level",
        value=default_strike,
        key=f"entry_strike_{instrument}_{selected['record_id']}",
        help="Required for CE/PE. For FUT/CASH/OTHER, enter a level only if useful.",
    )
    expiry = st.text_input(
        "Expiry",
        value=default_expiry,
        key=f"entry_expiry_{instrument}_{selected['record_id']}",
    )
    quantity = st.text_input(
        "Size / quantity / lots (optional)",
        value=default_qty,
        key=f"entry_qty_{instrument}_{selected['record_id']}",
    )
    note = st.text_input(
        "Entry note (optional)",
        value="",
        key=f"entry_note_{instrument}_{selected['record_id']}",
    )

    ctype_now = str(contract_type or "").upper().strip()
    strike_missing = ctype_now in {"CE", "PE"} and not str(strike or "").strip()
    if strike_missing:
        st.warning("Executed option strike is required before this campaign can be recorded.")

    submitted = st.button(
        "I have taken this trade — record campaign",
        type="primary",
        use_container_width=True,
        disabled=strike_missing,
        key=f"record_taken_trade_btn_{instrument}_{selected['record_id']}",
    )

    if submitted:
        try:
            if backend_name == "LocalStore":
                raise RuntimeError(
                    "SuperBrain is running on LocalStore, not the Google Sheet store. "
                    "The campaign was not recorded because it would not be durable in superbrain_memory."
                )

            trade = _sb_record_selected_pending_entry(
                store=store,
                packet=packet,
                setup_row=selected["row"],
                evaluation=evaluation,
                side=side,
                contract_type=contract_type,
                strike=strike,
                expiry=expiry,
                quantity=quantity,
                note=note,
            )

            trade_id = str(trade.get("trade_id", "") or "").strip()
            if not trade_id:
                raise RuntimeError("The storage layer returned no trade_id after the write.")

            # Hard read-back verification. Never tell the user a campaign is
            # recorded unless the same store can immediately retrieve that TRADE.
            verified = False
            verify_error = ""
            try:
                persisted = store.list_superbrain_trades(instrument=instrument, open_only=False)
                if persisted is not None and not persisted.empty and "trade_id" in persisted.columns:
                    verified = bool(persisted["trade_id"].astype(str).eq(trade_id).any())
            except Exception as verify_exc:
                verify_error = f"{type(verify_exc).__name__}: {verify_exc}"

            if not verified:
                detail = f" Read-back error: {verify_error}" if verify_error else ""
                raise RuntimeError(
                    f"Trade {trade_id} was not found on read-back from {backend_name}.{detail}"
                )

            # Make the recorded campaign visible immediately; the next Scan now
            # will refresh reasoning against this persisted exposure.
            existing_open = [
                x for x in (packet.get("open_trades") or [])
                if str(x.get("trade_id", "") or "") != trade_id
            ]
            existing_open.append(trade)
            packet["open_trades"] = existing_open
            st.session_state.public_superbrain_last = packet
            st.session_state["superbrain_notice"] = (
                f"Campaign recorded and verified in {backend_name}: {trade_id}. "
                "SuperBrain will manage the recorded exposure from the next scan."
            )
            st.session_state.pop("superbrain_error_notice", None)
            st.rerun()
        except Exception as exc:
            st.session_state["superbrain_error_notice"] = (
                f"Campaign was NOT recorded: {type(exc).__name__}: {exc}"
            )
            st.rerun()

def render_public_superbrain() -> None:
    """Clean manual Level-3 scan terminal."""
    st.subheader("SuperBrain")

    notice = str(st.session_state.pop("superbrain_notice", "") or "").strip()
    if notice:
        st.success(notice)
    error_notice = str(st.session_state.pop("superbrain_error_notice", "") or "").strip()
    if error_notice:
        st.error(error_notice)

    try:
        store = get_store()
        _run_expired_cleanup_once(store, show_notice=False)
        mapping = build_instrument_map(store)
    except Exception:
        st.error("Market data is not available right now.")
        return

    if mapping is None or mapping.empty:
        st.info("No instruments are available yet.")
        return

    instruments = mapping["instrument"].astype(str).tolist()
    instrument = st.selectbox("Instrument", instruments, key="public_superbrain_instrument")

    if st.button("Scan now", type="primary", use_container_width=True, key="scan_superbrain_public"):
        with st.spinner("Scanning market and options..."):
            try:
                st.session_state.public_superbrain_last = prepare_superbrain_scan(store, instrument)
            except Exception:
                st.error("Scan could not be completed. Check the data/login connection and retry.")
                return

    packet = st.session_state.get("public_superbrain_last")
    if not packet or str(packet.get("instrument", "")) != instrument:
        return
    analysis = packet.get("analysis", {}) or {}
    if not analysis:
        st.warning("No current read is available for this instrument.")
        return

    _sb_render_freshness(packet)
    active = [x for x in (packet.get("open_trades") or []) if str(x.get("status", "ACTIVE") or "ACTIVE").upper() not in {"CLOSED","EXIT","TARGET","INVALIDATED","EXPIRED","CANCELLED"}]
    pending = _sb_pending_setups(store, instrument)

    if not active:
        st.markdown("### Market now")
        st.write(_sb_current_market_line(analysis))
        st.caption(_sb_location_line(analysis, packet))
        st.caption(_sb_hidden_line(analysis))
        final_level_map = _sb_combined_level_ladder(packet, analysis)
        if final_level_map.get("level_rank_reliable"):
            st.caption(_sb_level_map_summary(final_level_map))
        else:
            st.caption("Final level map — authoritative L1/L2/L3 ranking is unavailable on this scan.")
        if final_level_map.get("context_note"):
            st.caption(final_level_map.get("context_note"))
        if final_level_map.get("level_warning"):
            st.warning(final_level_map.get("level_warning"))

        st.markdown("### Next likely event")
        if pending:
            nxt = _sb_pending_event_text(pending[0])
            st.write(f"**{nxt['event']}**")
            if str(pending[0].get("status", "")).upper() == "PENDING_ENTRY":
                entry_eval = _sb_evaluate_pending_setup(pending[0], packet)
                if entry_eval["status"] == "ENTRY READY":
                    new_decision = _sb_new_campaign_decision(pending[0], packet)
                    if new_decision.get("status") == "ENTRY READY":
                        opt = new_decision.get("option_type")
                        rank = new_decision.get("required_rank")
                        strike = new_decision.get("strike")
                        boundary = new_decision.get("boundary")
                        if strike is not None:
                            st.success(f"ENTRY READY — SELL {_sb_fmt_level(strike)} {opt} at L{rank} protection.")
                        else:
                            st.success(f"ENTRY READY — use L{rank} {opt} protection beyond {_sb_fmt_level(boundary)}; exact listed strike is not available in the live chain packet.")
                        st.write(new_decision.get("reason"))
                        st.caption(f"L{rank}: {_sb_level_group_text(new_decision.get('level'), with_rank=False)}")
                    else:
                        st.warning(f"WAIT — {new_decision.get('reason') or entry_eval['reason']}")
                else:
                    st.warning(f"WAIT — {entry_eval['reason']}")
            else:
                st.write(nxt["look"])
                if nxt.get("odme"):
                    st.caption(f"ODME after break: {nxt['odme']}")
        else:
            nxt = _sb_next_market_event(analysis, packet)
            st.write(f"**{nxt['event']}**")
            st.write(nxt["look"])
    else:
        st.markdown("### Active campaign")
        st.caption(_sb_current_market_line(analysis))
        final_level_map = _sb_combined_level_ladder(packet, analysis)
        above_levels = final_level_map.get("above", [])
        below_levels = final_level_map.get("below", [])
        above_l1 = above_levels[0] if len(above_levels) >= 1 else None
        above_l2 = above_levels[1] if len(above_levels) >= 2 else None
        above_l3 = above_levels[2] if len(above_levels) >= 3 else None
        below_l1 = below_levels[0] if len(below_levels) >= 1 else None
        below_l2 = below_levels[1] if len(below_levels) >= 2 else None
        below_l3 = below_levels[2] if len(below_levels) >= 3 else None
        if final_level_map.get("level_rank_reliable"):
            st.caption(_sb_level_map_summary(final_level_map))
        else:
            st.caption("Final level map — authoritative level ranking is unavailable on this scan; no L1/L2/L3 target will be invented.")
        if final_level_map.get("context_note"):
            st.caption(final_level_map.get("context_note"))
        if final_level_map.get("level_warning"):
            st.warning(final_level_map.get("level_warning"))
        for i, trade in enumerate(active):
            setup = _sb_setup_for_trade(store, trade)
            tmd = _sb_json(trade.get("metadata_json", ""))
            smd = _sb_setup_md(setup)
            setup_name = str(smd.get("setup_name") or tmd.get("origin_setup_name") or trade.get("strategy_type") or "Campaign")
            scan_plan = analysis.get("trade_plan") or {}
            plan_health = scan_plan.get("behavior_health") if str(scan_plan.get("trade_id", "") or "") == str(trade.get("trade_id", "") or "") else ""
            health = str(plan_health or tmd.get("behavior_health") or "").replace("_", " ").title()
            title = f"{trade.get('trade_id','Campaign')} · {setup_name}"
            if len(active) > 1:
                st.markdown(f"**{title}**")
            else:
                st.write(f"**{title}**")
            st.write(f"Exposure: {_sb_exposure_text(trade)}")
            expected = _sb_campaign_expected(trade, setup)
            now = _sb_hidden_line(analysis)
            if health:
                st.write(f"**Behavior now:** {health}. {now}")
            else:
                st.write(f"**Behavior now:** {now}")
            st.caption(f"Expected: {expected}")

            final_decisions = _sb_campaign_final_decisions(packet, trade, setup, analysis)
            if final_decisions:
                for decision in final_decisions:
                    action = str(decision.get("action") or "HOLD").upper()
                    decision_line = _sb_campaign_decision_line(decision)
                    if action == "ROLL FARTHER":
                        st.error(f"**Campaign decision: {decision_line}**")
                    elif action == "ROLL IN":
                        st.success(f"**Campaign decision: {decision_line}**")
                    else:
                        st.info(f"**Campaign decision: {decision_line}**")

                    st.write(
                        f"Current {_sb_fmt_level(decision.get('strike'))} {decision.get('option_type')} is "
                        f"{decision.get('relation')}. "
                        f"ODME is {decision.get('odme_direction')} · {decision.get('odme_strength')} support · "
                        f"{decision.get('odme_control')} control. {decision.get('reason')}"
                    )
                    if decision.get("rank_reliable"):
                        required = decision.get("tv_required_rank")
                        protection_text = f"L{required}" if required in {2, 3} else "event-driven / no forced tier"
                        st.caption(
                            f"Management state: {decision.get('campaign_state') or 'WATCH'} · "
                            f"Protection requirement: {protection_text} · "
                            f"ODME posture: {decision.get('posture')} · "
                            f"Trigger: {decision.get('management_trigger')}."
                        )
                        l1_text = _sb_level_group_text(decision.get("l1"))
                        l2_text = _sb_level_group_text(decision.get("l2"))
                        l3_text = _sb_level_group_text(decision.get("l3"))
                        side_word = "above" if str(decision.get("option_type")).upper() == "CE" else "below"
                        st.caption(
                            f"Protection ladder {side_word} price: "
                            f"L1 {l1_text.replace('L1 ', '', 1)} | "
                            f"L2 {l2_text.replace('L2 ', '', 1)} | "
                            f"L3 {l3_text.replace('L3 ', '', 1)}"
                        )
                    else:
                        st.caption("Authoritative L1/L2/L3 ladder is unavailable on this scan; no strike adjustment is invented.")
            else:
                st.info("Campaign decision: HOLD — no active sold CE/PE leg with a valid strike requires strike-ladder management on this scan.")

            companion_action = _sb_campaign_companion_action(packet, trade, setup, analysis)
            if companion_action:
                c_action = str(companion_action.get("action") or "WATCH").upper()
                c_opt = str(companion_action.get("option_type") or "").upper()
                c_rank = companion_action.get("rank")
                c_strike = companion_action.get("strike")
                if c_action == "ADD" and c_strike is not None:
                    st.warning(
                        f"**Locked management action: ADD / SELL {_sb_fmt_level(c_strike)} {c_opt} at L{c_rank}.**"
                    )
                else:
                    st.warning(f"**Locked management action: {c_action} {c_opt} at L{c_rank}.**")
                st.write(companion_action.get("reason"))
                if companion_action.get("level"):
                    st.caption(f"L{c_rank}: {_sb_level_group_text(companion_action.get('level'), with_rank=False)}")

            _sb_render_management_confirmation(
                store, packet, trade, setup, analysis,
                final_decisions=final_decisions, companion_action=companion_action
            )
            if i < len(active) - 1:
                st.markdown("---")

        st.markdown("### Next new campaign")
        # Do not present an originating setup already linked to an active campaign.
        active_setup_ids = set()
        for active_trade in active:
            amd = _sb_json((active_trade or {}).get("metadata_json", ""))
            linked = str(amd.get("parent_setup_id") or amd.get("origin_setup_record_id") or amd.get("setup_record_id") or "").strip()
            if linked:
                active_setup_ids.add(linked)
        pending_new = [
            x for x in pending
            if str(x.get("status", "")).upper() in {"PENDING_ENTRY", "BREAK_WATCH"}
            and str(x.get("record_id", "") or "").strip() not in active_setup_ids
        ]
        if pending_new:
            nxt = _sb_pending_event_text(pending_new[0])
            st.write(f"**{nxt['event']}**")
            if str(pending_new[0].get("status", "")).upper() == "PENDING_ENTRY":
                entry_eval = _sb_evaluate_pending_setup(pending_new[0], packet)
                if entry_eval["status"] == "ENTRY READY":
                    new_decision = _sb_new_campaign_decision(pending_new[0], packet)
                    if new_decision.get("status") == "ENTRY READY":
                        opt = new_decision.get("option_type")
                        rank = new_decision.get("required_rank")
                        strike = new_decision.get("strike")
                        boundary = new_decision.get("boundary")
                        if strike is not None:
                            st.success(f"ENTRY READY — SELL {_sb_fmt_level(strike)} {opt} at L{rank} protection.")
                        else:
                            st.success(f"ENTRY READY — use L{rank} {opt} protection beyond {_sb_fmt_level(boundary)}; exact listed strike is not available in the live chain packet.")
                        st.write(new_decision.get("reason"))
                        st.caption(f"L{rank}: {_sb_level_group_text(new_decision.get('level'), with_rank=False)}")
                    else:
                        st.warning(f"WAIT — {new_decision.get('reason') or entry_eval['reason']}")
                else:
                    st.warning(f"WAIT — {entry_eval['reason']}")
            else:
                st.write(nxt["look"])
                if nxt.get("odme"):
                    st.caption(f"ODME after break: {nxt['odme']}")
        else:
            nxt = _sb_next_market_event(analysis, packet)
            st.write(f"**{nxt['event']}**")
            st.write(nxt["look"])

    _sb_render_odme(packet)
    _sb_render_trade_confirmation(store, instrument, packet)

def _run_expired_cleanup_once(store: Any, show_notice: bool = True) -> None:
    """Automatically remove finished-expiry ODME snapshots once per India date."""
    india_date = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    if st.session_state.get("last_expiry_cleanup_date") == india_date:
        return
    cleanup = store.cleanup_expired_data(tz_name="Asia/Kolkata")
    st.session_state["last_expiry_cleanup_date"] = india_date
    if show_notice and (cleanup.get("deleted_snapshots", 0) or cleanup.get("cleared_scan_settings", 0)):
        st.toast(
            f"Expired ODME cleanup: {cleanup.get('deleted_snapshots', 0)} snapshot(s) deleted; "
            f"{cleanup.get('cleared_scan_settings', 0)} scan setting(s) cleared."
        )


# =============================================================================
# UI helpers
# =============================================================================


def inject_css() -> None:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1rem; padding-bottom: 2rem; max-width: 1500px;}
        h1 {font-size: 1.42rem !important; margin-bottom: 0.10rem !important;}
        h2 {font-size: 1.12rem !important;}
        h3 {font-size: 0.98rem !important; margin-top: 0.6rem !important;}
        .small-note {font-size: 0.76rem; color: rgba(90,90,90,0.95);}
        .data-line {font-size: 0.86rem; font-weight: 850; margin: 0.25rem 0 0.55rem 0; color: rgba(25,25,25,0.95);}
        .data-sub {font-size: 0.72rem; font-weight: 650; color: rgba(105,105,105,0.95); margin-top: -0.35rem; margin-bottom: 0.55rem;}
        .fetch-failed {font-size: 0.86rem; font-weight: 900; color: #c73535; margin: 0.25rem 0 0.55rem 0;}
        .premium-alert {font-size: 0.86rem; font-weight: 900; color: #c73535; margin: 0.55rem 0 0.75rem 0;}
        .odme-card {
            border: 1px solid rgba(100,100,100,0.20);
            border-radius: 13px;
            padding: 0.55rem 0.62rem;
            min-height: 58px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.035);
            background: rgba(250,250,250,0.70);
        }
        .odme-card .label {
            font-size: 0.62rem;
            letter-spacing: 0.025rem;
            color: rgba(85,85,85,0.95);
            margin-bottom: 0.12rem;
            text-transform: uppercase;
            font-weight: 800;
        }
        .odme-card .value {
            font-size: 0.92rem;
            font-weight: 850;
            line-height: 1.13;
            color: rgba(18,18,18,0.95);
        }
        .odme-card .sub {
            font-size: 0.66rem;
            color: rgba(95,95,95,0.95);
            margin-top: 0.18rem;
            line-height: 1.18;
        }
        .hero-card {
            border-radius: 16px;
            padding: 0.90rem 1.00rem;
            border: 1px solid rgba(70,70,70,0.18);
            background: linear-gradient(135deg, rgba(246,248,252,1), rgba(255,255,255,0.95));
            box-shadow: 0 2px 10px rgba(0,0,0,0.055);
            margin-top: 0.20rem;
            margin-bottom: 0.70rem;
        }
        .hero-card .label {
            font-size: 0.70rem;
            font-weight: 850;
            color: rgba(75,75,75,0.94);
            text-transform: uppercase;
            letter-spacing: 0.045rem;
        }
        .hero-card .value {
            font-size: 1.02rem;
            font-weight: 760;
            margin-top: 0.20rem;
            line-height: 1.42;
        }
        .action-card {
            border-radius: 15px;
            padding: 0.76rem 0.86rem;
            min-height: 124px;
            border: 1px solid rgba(70,70,70,0.16);
            box-shadow: 0 1px 6px rgba(0,0,0,0.045);
        }
        .action-card .title {font-size: 0.72rem; font-weight: 900; letter-spacing: 0.04rem; text-transform: uppercase; color: rgba(70,70,70,0.96);}
        .action-card .level {font-size: 0.96rem; font-weight: 850; margin-top: 0.20rem;}
        .action-card .body {font-size: 0.82rem; line-height: 1.36; margin-top: 0.45rem; color: rgba(35,35,35,0.94);}
        .tint-green {background: linear-gradient(135deg, rgba(31,181,90,0.15), rgba(255,255,255,0.78)); border-color: rgba(31,181,90,0.32);}
        .tint-red {background: linear-gradient(135deg, rgba(221,65,65,0.15), rgba(255,255,255,0.78)); border-color: rgba(221,65,65,0.32);}
        .tint-amber {background: linear-gradient(135deg, rgba(232,159,34,0.20), rgba(255,255,255,0.78)); border-color: rgba(232,159,34,0.38);}
        .tint-blue {background: linear-gradient(135deg, rgba(65,125,220,0.14), rgba(255,255,255,0.78)); border-color: rgba(65,125,220,0.30);}
        .tint-grey {background: linear-gradient(135deg, rgba(120,120,120,0.12), rgba(255,255,255,0.78)); border-color: rgba(120,120,120,0.25);}
        div[data-testid="stMetric"] {background: rgba(250,250,250,0.45); border: 1px solid rgba(120,120,120,0.16); border-radius: 12px; padding: 0.50rem 0.58rem;}
        div[data-testid="stMetricLabel"] {font-size: 0.70rem !important;}
        div[data-testid="stMetricValue"] {font-size: 0.94rem !important;}
        div[data-testid="stMetricDelta"] {font-size: 0.68rem !important;}
        .stDataFrame {font-size: 0.76rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _html_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_num(value: Any, decimals: int = 0) -> str:
    try:
        if value is None or value == "":
            return "NA"
        x = float(value)
        if abs(x) < 1e-12:
            return "NA" if decimals == 0 else f"{x:,.{decimals}f}"
        if decimals == 0:
            return f"{x:,.0f}"
        return f"{x:,.{decimals}f}"
    except Exception:
        return "NA"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _movement_label_for_display(previous: Any, current: Any) -> str:
    """Return a compact movement marker for comparison rows."""
    try:
        if previous is None or previous == "" or current is None or current == "":
            return ""
        prev = float(previous)
        cur = float(current)
        if abs(cur - prev) < 1e-9:
            return ""
        return "↑" if cur > prev else "↓"
    except Exception:
        return ""


def _tint_for_tilt(tilt: str) -> str:
    tilt = str(tilt).upper()
    if "BULLISH" in tilt:
        return "tint-green"
    if "BEARISH" in tilt:
        return "tint-red"
    if "RANGE" in tilt:
        return "tint-blue"
    if "EXPANSION" in tilt or "TRAP" in tilt:
        return "tint-amber"
    return "tint-grey"


def _tint_for_action(text: str, default: str = "tint-grey") -> str:
    t = str(text).lower()
    if any(x in t for x in ["failure", "failing", "do not sell", "avoid", "reduce", "exit"]):
        return "tint-red"
    if any(x in t for x in ["under pressure", "safer", "wait", "not clean", "monitor", "only after"]):
        return "tint-amber"
    if any(x in t for x in ["working", "control", "acceptable", "valid", "usable"]):
        return "tint-green"
    return default


def _tint_for_change_pct(value: Any) -> str:
    pct = _safe_float(value)
    if pct > 0.01:
        return "tint-green"
    if pct < -0.01:
        return "tint-red"
    return "tint-grey"


def render_card(label: str, value: Any, sub: str = "", tint: str = "tint-grey") -> None:
    st.markdown(
        f"""
        <div class="odme-card {tint}">
            <div class="label">{_html_escape(label)}</div>
            <div class="value">{_html_escape(value).replace("&lt;br&gt;", "<br>")}</div>
            <div class="sub">{_html_escape(sub)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero(label: str, value: str, tint: str = "") -> None:
    cls = f"hero-card {tint}" if tint else "hero-card"
    st.markdown(
        f"""
        <div class="{cls}">
            <div class="label">{_html_escape(label)}</div>
            <div class="value">{_html_escape(value).replace("&lt;br&gt;", "<br>")}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_action_card(title: str, level_line: str, body: str, tint: str) -> None:
    st.markdown(
        f"""
        <div class="action-card {tint}">
            <div class="title">{_html_escape(title)}</div>
            <div class="level">{_html_escape(level_line)}</div>
            <div class="body">{_html_escape(body)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def split_commentary(commentary: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_key = None
    current_lines: List[str] = []
    for raw in str(commentary or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" in line:
            possible_key, rest = line.split(":", 1)
            if len(possible_key) <= 35 and possible_key.replace("/", "").replace("-", "").replace(" ", "").isalpha():
                if current_key:
                    sections[current_key] = " ".join(current_lines).strip()
                current_key = possible_key.strip()
                current_lines = [rest.strip()]
                continue
        if current_key:
            current_lines.append(line)
    if current_key:
        sections[current_key] = " ".join(current_lines).strip()
    return sections


def get_section(sections: Dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        if sections.get(name):
            return sections[name]
    return default


def render_score_bars_from_values(scores: Dict[str, Any]) -> None:
    st.markdown("### 5. Scores")
    cols = st.columns(4)
    items = [
        ("Bullish", "Bullish", "tint-green"),
        ("Bearish", "Bearish", "tint-red"),
        ("Range", "Range", "tint-blue"),
        ("Expansion", "Expansion / Trap", "tint-amber"),
    ]
    for col, (key, label, tint) in zip(cols, items):
        val = int(_safe_float(scores.get(key, 0)))
        with col:
            render_card(label, f"{val}/100", "", tint)
            st.progress(max(0, min(100, val)) / 100)


def result_to_display(result: Dict[str, Any]) -> Dict[str, Any]:
    sections = split_commentary(result.get("commentary", ""))
    prev = result.get("_previous_summary") or {}
    prev_spot = _safe_float(prev.get("spot"))
    spot_now = _safe_float(result.get("spot"))
    day_change = spot_now - prev_spot if prev_spot else 0.0
    day_change_pct = (day_change / prev_spot * 100.0) if prev_spot else 0.0
    return {
        "kind": "live",
        "ts": result.get("ts", ""),
        "tilt": result.get("tilt", "MIXED / NO CLEAN EDGE"),
        "spot": result.get("spot"),
        "previous_spot": prev_spot,
        "day_change": day_change,
        "day_change_pct": day_change_pct,
        "poc": result.get("poc"),
        "value_area_low": result.get("value_area_low"),
        "value_area_high": result.get("value_area_high"),
        "ce_wall": result.get("ce_wall"),
        "pe_wall": result.get("pe_wall"),
        "ce_wall_move": result.get("ce_wall_move", ""),
        "pe_wall_move": result.get("pe_wall_move", ""),
        "poc_move": result.get("poc_move", ""),
        "range_move": result.get("range_move", ""),
        "scores": result.get("scores", {}),
        "safer_sell_ce": result.get("safer_sell_ce"),
        "safer_sell_pe": result.get("safer_sell_pe"),
        "commentary": result.get("commentary", ""),
        "sections": sections,
        "final_action": result.get("final_action") or get_section(sections, "Final Action", default="No final action generated."),
        "ce_action": result.get("ce_action") or get_section(sections, "CE Action", default="CE side has no strong confirmation yet."),
        "pe_action": result.get("pe_action") or get_section(sections, "PE Action", default="PE side has no strong confirmation yet."),
        "risk_note": get_section(sections, "Risk Note", default="No risk note generated."),
        "verdict_text": get_section(sections, "ODME Verdict", default=result.get("tilt", "")),
        "session_read": get_section(sections, "Session Read", default=""),
        "cards": result.get("cards", {}),
        "premium_alert": result.get("premium_alert", ""),
        "hero_action": result.get("hero_action") or result.get("final_action") or get_section(sections, "Final Action", default="No final action generated."),
        "anchor_snapshot_ts": result.get("anchor_snapshot_ts", ""),
        "path_risk": result.get("path_risk", {}),
    }


def saved_row_to_display(row: Dict[str, Any], previous_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Display the last saved snapshot with the same card logic as the last fetch.

    This prevents the app from reopening into grey "Fetch live" cards. The saved
    Google Sheet summary contains compact key-strike data, so we can rebuild the
    last anchored card colours, arrows, premium alert, hero text and path assist
    without a fresh Angel fetch.
    """
    current_summary = parse_previous_summary(row or {})
    previous_summary = parse_previous_summary(previous_row or {}) if previous_row else {}
    rebuilt = reconstruct_saved_result(current_summary, previous_summary)
    sections = split_commentary(row.get("commentary", ""))
    prev_spot = _safe_float(previous_summary.get("spot"))
    spot_now = _safe_float(rebuilt.get("spot"))
    day_change = spot_now - prev_spot if prev_spot else 0.0
    day_change_pct = (day_change / prev_spot * 100.0) if prev_spot else 0.0
    return {
        "kind": "saved",
        "ts": row.get("ts", ""),
        "tilt": rebuilt.get("tilt") or row.get("odme_tilt", "MIXED / NO CLEAN EDGE"),
        "spot": rebuilt.get("spot"),
        "previous_spot": prev_spot,
        "day_change": day_change,
        "day_change_pct": day_change_pct,
        "poc": rebuilt.get("poc"),
        "value_area_low": rebuilt.get("value_area_low"),
        "value_area_high": rebuilt.get("value_area_high"),
        "ce_wall": rebuilt.get("ce_wall"),
        "pe_wall": rebuilt.get("pe_wall"),
        "ce_wall_move": rebuilt.get("ce_wall_move", ""),
        "pe_wall_move": rebuilt.get("pe_wall_move", ""),
        "poc_move": rebuilt.get("poc_move", ""),
        "range_move": row.get("range_shift", ""),
        "scores": rebuilt.get("scores", {}),
        "safer_sell_ce": rebuilt.get("safer_sell_ce"),
        "safer_sell_pe": rebuilt.get("safer_sell_pe"),
        "commentary": row.get("commentary", ""),
        "sections": sections,
        "final_action": rebuilt.get("final_action") or get_section(sections, "Final Action", default="Last saved ODME action unavailable."),
        "ce_action": get_section(sections, "CE Action", default=""),
        "pe_action": get_section(sections, "PE Action", default=""),
        "risk_note": get_section(sections, "Risk Note", default=""),
        "verdict_text": get_section(sections, "ODME Verdict", default=row.get("odme_tilt", "")),
        "session_read": get_section(sections, "Session Read", default=""),
        "cards": rebuilt.get("cards", {}),
        "premium_alert": rebuilt.get("premium_alert", ""),
        "hero_action": rebuilt.get("hero_action") or rebuilt.get("final_action") or get_section(sections, "Final Action", default="Last saved ODME action unavailable."),
        "anchor_snapshot_ts": previous_row.get("ts", "") if previous_row else "",
        "path_risk": rebuilt.get("path_risk", {}),
    }

def build_comparison_table(result: Dict[str, Any]) -> pd.DataFrame:
    prev = result.get("_previous_summary") or {}
    if not prev:
        return pd.DataFrame()
    rows = [
        {"Metric": "Future/Spot", "Previous": _fmt_num(prev.get("spot"), 2), "Current": _fmt_num(result.get("spot"), 2), "Change": _fmt_num(_safe_float(result.get("spot")) - _safe_float(prev.get("spot")), 2)},
        {"Metric": "POC", "Previous": _fmt_num(prev.get("poc") or prev.get("option_poc")), "Current": _fmt_num(result.get("poc")), "Change": result.get("poc_move", "")},
        {"Metric": "CE Wall", "Previous": _fmt_num(prev.get("ce_wall")), "Current": _fmt_num(result.get("ce_wall")), "Change": result.get("ce_wall_move", "")},
        {"Metric": "PE Wall", "Previous": _fmt_num(prev.get("pe_wall")), "Current": _fmt_num(result.get("pe_wall")), "Change": result.get("pe_wall_move", "")},
        {"Metric": "Safer CE Sell", "Previous": _fmt_num(prev.get("safer_sell_ce")), "Current": _fmt_num(result.get("safer_sell_ce")), "Change": _movement_label_for_display(prev.get("safer_sell_ce"), result.get("safer_sell_ce"))},
        {"Metric": "Safer PE Sell", "Previous": _fmt_num(prev.get("safer_sell_pe")), "Current": _fmt_num(result.get("safer_sell_pe")), "Change": _movement_label_for_display(prev.get("safer_sell_pe"), result.get("safer_sell_pe"))},
    ]
    return pd.DataFrame(rows)


def build_saved_comparison_table(history: pd.DataFrame) -> pd.DataFrame:
    if history is None or history.empty or len(history) < 2:
        return pd.DataFrame()
    h = history.sort_values("ts").tail(2).copy()
    prev = h.iloc[0].to_dict()
    cur = h.iloc[1].to_dict()
    rows = [
        {"Metric": "Future/Spot", "Previous": _fmt_num(prev.get("spot"), 2), "Current": _fmt_num(cur.get("spot"), 2), "Change": _fmt_num(_safe_float(cur.get("spot")) - _safe_float(prev.get("spot")), 2)},
        {"Metric": "POC", "Previous": _fmt_num(prev.get("option_poc")), "Current": _fmt_num(cur.get("option_poc")), "Change": cur.get("poc_shift", "")},
        {"Metric": "CE Wall", "Previous": _fmt_num(prev.get("ce_wall")), "Current": _fmt_num(cur.get("ce_wall")), "Change": cur.get("ce_wall_shift", "")},
        {"Metric": "PE Wall", "Previous": _fmt_num(prev.get("pe_wall")), "Current": _fmt_num(cur.get("pe_wall")), "Change": cur.get("pe_wall_shift", "")},
        {"Metric": "Safer CE Sell", "Previous": _fmt_num(prev.get("safer_sell_ce")), "Current": _fmt_num(cur.get("safer_sell_ce")), "Change": _movement_label_for_display(prev.get("safer_sell_ce"), cur.get("safer_sell_ce"))},
        {"Metric": "Safer PE Sell", "Previous": _fmt_num(prev.get("safer_sell_pe")), "Current": _fmt_num(cur.get("safer_sell_pe")), "Change": _movement_label_for_display(prev.get("safer_sell_pe"), cur.get("safer_sell_pe"))},
    ]
    return pd.DataFrame(rows)


def build_chain_view(result: Dict[str, Any], radius: int = 10) -> pd.DataFrame:
    """Plain ATM ± radius option-chain view.

    Reads are intentionally shown only on the OTM side:
    - strikes above spot: CE read only
    - strikes below spot: PE read only
    - nearest ATM row: reference row, no directional read
    """
    table = result.get("strike_table", pd.DataFrame())
    if table is None or table.empty:
        return pd.DataFrame()
    df = table.copy()
    spot = _safe_float(result.get("spot"))
    atm = None
    if spot and "strike" in df.columns:
        df["_dist"] = (pd.to_numeric(df["strike"], errors="coerce") - spot).abs()
        atm = _safe_float(df.sort_values("_dist").iloc[0]["strike"])
        strikes_sorted = sorted(pd.to_numeric(df["strike"], errors="coerce").dropna().unique().tolist())
        if atm in strikes_sorted:
            idx = strikes_sorted.index(atm)
            keep = set(strikes_sorted[max(0, idx - radius): idx + radius + 1])
            df = df[df["strike"].isin(keep)].copy()
    matrix = result.get("matrix", pd.DataFrame())
    ce_delta = pd.DataFrame()
    pe_delta = pd.DataFrame()
    if matrix is not None and not matrix.empty:
        m = matrix.copy()
        m["strike"] = pd.to_numeric(m["strike"], errors="coerce")
        ce_delta = m[m["side"].eq("CE")][["strike", "spot_adjusted_read"]].rename(columns={"spot_adjusted_read": "CE Read"})
        pe_delta = m[m["side"].eq("PE")][["strike", "spot_adjusted_read"]].rename(columns={"spot_adjusted_read": "PE Read"})
    out = df.merge(ce_delta, on="strike", how="left").merge(pe_delta, on="strike", how="left")
    out["Strike"] = pd.to_numeric(out["strike"], errors="coerce")
    out["Zone"] = ""
    if spot:
        out.loc[out["Strike"] > spot, "Zone"] = "Upside / OTM CE"
        out.loc[out["Strike"] < spot, "Zone"] = "Downside / OTM PE"
    if atm:
        out.loc[out["Strike"].round(6).eq(round(float(atm), 6)), "Zone"] = "ATM reference"
    # Show one clean OTM buildup column only:
    # - strikes above spot use CE read
    # - strikes below spot use PE read
    # - nearest ATM row is kept as reference with blank buildup
    out["Buildup"] = ""
    if spot:
        out.loc[out["Strike"] > spot, "Buildup"] = out.loc[out["Strike"] > spot, "CE Read"].fillna("")
        out.loc[out["Strike"] < spot, "Buildup"] = out.loc[out["Strike"] < spot, "PE Read"].fillna("")
    if atm:
        out.loc[out["Strike"].round(6).eq(round(float(atm), 6)), "Buildup"] = ""
    rename = {"ce_ltp": "CE LTP", "pe_ltp": "PE LTP"}
    keep = ["Strike", "Buildup", "ce_ltp", "pe_ltp"]
    out = out[[c for c in keep if c in out.columns]].rename(columns=rename).sort_values("Strike")
    return out

def style_chain_table(df: pd.DataFrame, result: Dict[str, Any]):
    if df.empty:
        return df
    poc = _safe_float(result.get("poc"))
    ce_wall = _safe_float(result.get("ce_wall"))
    pe_wall = _safe_float(result.get("pe_wall"))
    spot = _safe_float(result.get("spot"))
    max_ce_oi = max(float(pd.to_numeric(df.get("CE OI", pd.Series(dtype=float)), errors="coerce").max() or 0), 1)
    max_pe_oi = max(float(pd.to_numeric(df.get("PE OI", pd.Series(dtype=float)), errors="coerce").max() or 0), 1)
    max_combined = max(float(pd.to_numeric(df.get("Combined OI", pd.Series(dtype=float)), errors="coerce").max() or 0), 1)

    def row_style(row: pd.Series) -> List[str]:
        strike = _safe_float(row.get("Strike"))
        styles = ["" for _ in row.index]
        if strike == poc:
            styles = ["background-color: rgba(65,125,220,0.18); font-weight: 700;" for _ in row.index]
        if strike == ce_wall:
            styles = ["background-color: rgba(221,65,65,0.16); font-weight: 700;" for _ in row.index]
        if strike == pe_wall:
            styles = ["background-color: rgba(31,181,90,0.16); font-weight: 700;" for _ in row.index]
        if spot and abs(strike - spot) == min(abs(pd.to_numeric(df["Strike"], errors="coerce") - spot)):
            styles = [s + " border-top: 2px solid rgba(232,159,34,0.85); border-bottom: 2px solid rgba(232,159,34,0.85);" for s in styles]
        return styles

    def oi_tint(value: Any, side: str) -> str:
        x = _safe_float(value)
        denom = max_ce_oi if side == "CE" else max_pe_oi
        alpha = min(0.34, 0.06 + 0.28 * (x / denom))
        if side == "CE":
            return f"background-color: rgba(221,65,65,{alpha});"
        return f"background-color: rgba(31,181,90,{alpha});"

    def combined_tint(value: Any) -> str:
        x = _safe_float(value)
        alpha = min(0.30, 0.05 + 0.25 * (x / max_combined))
        return f"background-color: rgba(65,125,220,{alpha});"

    def delta_tint(value: Any) -> str:
        x = _safe_float(value)
        if x > 0:
            return "color: #14843b; font-weight: 700;"
        if x < 0:
            return "color: #c73535; font-weight: 700;"
        return "color: #777777;"

    styler = df.style.apply(row_style, axis=1)

    def apply_element_style(styler_obj, func, subset):
        # pandas 2.1+ uses Styler.map; older versions use Styler.applymap.
        # Streamlit Cloud may install a pandas version where applymap is removed.
        if hasattr(styler_obj, "map"):
            return styler_obj.map(func, subset=subset)
        return styler_obj.applymap(func, subset=subset)

    if "CE OI" in df.columns:
        styler = apply_element_style(styler, lambda v: oi_tint(v, "CE"), subset=["CE OI"])
    if "PE OI" in df.columns:
        styler = apply_element_style(styler, lambda v: oi_tint(v, "PE"), subset=["PE OI"])
    if "Combined OI" in df.columns:
        styler = apply_element_style(styler, combined_tint, subset=["Combined OI"])
    for col in ["CE ΔOI", "CE ΔPrem", "PE ΔOI", "PE ΔPrem"]:
        if col in df.columns:
            styler = apply_element_style(styler, delta_tint, subset=[col])
    numeric_cols = [c for c in df.columns if c not in ["CE Read", "PE Read"]]
    fmt0 = {c: "{:,.0f}" for c in numeric_cols if c not in ["CE LTP", "PE LTP", "CE ΔPrem", "PE ΔPrem"]}
    fmt2 = {c: "{:,.2f}" for c in ["CE LTP", "PE LTP", "CE ΔPrem", "PE ΔPrem"] if c in df.columns}
    return styler.format(fmt0).format(fmt2)


# =============================================================================
# App logic
# =============================================================================


def app_header(store) -> None:
    left, right = st.columns([3, 1])
    with left:
        st.title(APP_NAME)
        st.caption("Live Angel option-chain read → compact ODME summary saved to Google Sheets.")
    with right:
        if st.button("Logout"):
            for k in ["logged_in", "angel", "master", "angel_login_at"]:
                st.session_state[k] = False if k == "logged_in" else None
            _angel_session_cache().clear()
            st.rerun()
    angel = st.session_state.get("angel")
    session_text = angel.session_label() if angel is not None and hasattr(angel, "session_label") else "Angel session active"
    st.caption(session_text)


def fetch_analyze_save(store, angel: AngelConnector, master: pd.DataFrame, instrument: str, expiry: str, force: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch verified Angel futures LTP + option chain, analyze, and save compact ODME summary.

    No manual spot fallback is allowed. If Angel futures LTP cannot be verified against the
    selected option-chain strike range, analysis is blocked and the user sees the reason.
    """
    key = make_key(instrument, expiry)
    now = datetime.now(timezone.utc)
    last_map = st.session_state.get("last_refresh_by_key", {})
    last = last_map.get(key)
    if (not force) and last:
        age = (now - last).total_seconds()
        if age < REFRESH_INTERVAL_SECONDS:
            return None

    angel.ensure_session_ready()
    previous_raw = store.load_anchor_odme_snapshot(key)
    previous = parse_previous_summary(previous_raw)
    if isinstance(previous_raw, dict) and previous_raw.get("ts"):
        anchor_note = f"anchor=latest saved snapshot before today ({previous_raw.get('ts')})"
    else:
        anchor_note = "anchor=no saved snapshot before today; current snapshot saved, anchored comparison will begin from the next trading session"

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
    result["future_mapping_reason"] = info.get("future_mapping_reason", "")
    result["option_expiry_used_for_mapping"] = info.get("option_expiry_used_for_mapping", "")

    snapshot_id = make_snapshot_id(key)
    ts = utc_now_iso()
    result["ts"] = ts
    status_note = "OK" if usable > 0 else "Selected expiry has no usable OI. Choose another active expiry."
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
    store.append_odme_snapshot(result, meta)
    last_map[key] = now
    st.session_state.last_refresh_by_key = last_map
    st.session_state.last_result_by_key[key] = result
    return {"result": result, "meta": meta, "usable": usable, "contracts": len(chain), "future_ltp": future_ltp}




def _tint_from_card_color(color: str) -> str:
    color = str(color or "").lower()
    if color == "green":
        return "tint-green"
    if color == "red":
        return "tint-red"
    if color in ["orange", "amber"]:
        return "tint-amber"
    return "tint-grey"


def render_data_line(display: Dict[str, Any], live_result: Optional[Dict[str, Any]]) -> None:
    if live_result and live_result.get("error"):
        st.markdown(f'<div class="fetch-failed">Fetch failed — Reason: {_html_escape(live_result.get("error"))}</div>', unsafe_allow_html=True)
        return
    st.markdown('<div class="data-line">Live data</div>' if display.get("kind") == "live" else '<div class="data-line">Last saved data</div>', unsafe_allow_html=True)
    if display.get("kind") == "live" and live_result is not None and not live_result.get("anchor_snapshot_ts"):
        st.markdown('<div class="data-sub">No prior anchor — observation mode</div>', unsafe_allow_html=True)


def render_spot_futures_card(display: Dict[str, Any]) -> None:
    spot = _safe_float(display.get("spot"))
    pct = _safe_float(display.get("day_change_pct"))
    change = _safe_float(display.get("day_change"))
    if not spot:
        return
    if display.get("previous_spot"):
        sign = "+" if change > 0 else ""
        sub = f"Day change: {sign}{_fmt_num(change, 2)} ({sign}{pct:.2f}%) vs anchor"
    else:
        sub = "Day change unavailable — no prior anchor"
    render_card("Spot / Futures", _fmt_num(spot, 2), sub, _tint_for_change_pct(pct))


def render_trade_card(card: Dict[str, Any]) -> None:
    title = card.get("title", "")
    level = str(card.get("level", ""))
    arrow = str(card.get("arrow", ""))
    state = card.get("state", "")
    message = card.get("message", "")
    tint = _tint_from_card_color(card.get("color", "grey"))
    level_line = f"{level} {arrow}".strip()
    body = f"{state}<br><span style='font-size:0.74rem;color:rgba(80,80,80,0.95);'>{_html_escape(message)}</span>"
    st.markdown(
        f"""
        <div class="odme-card {tint}">
            <div class="label">{_html_escape(title)}</div>
            <div class="value">{_html_escape(level_line)}</div>
            <div class="sub"><b>{_html_escape(state)}</b><br>{_html_escape(message)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _fallback_cards(display: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        "poc": {"title": "POC", "level": _fmt_num(display.get("poc")), "arrow": "", "color": "grey", "state": "Saved view", "message": "Fetch live data for current POC read."},
        "ce_wall": {"title": "CE Wall", "level": f"{_fmt_num(display.get('ce_wall'))} CE", "arrow": "", "color": "grey", "state": "Saved view", "message": "Fetch live data for wall quality."},
        "pe_wall": {"title": "PE Wall", "level": f"{_fmt_num(display.get('pe_wall'))} PE", "arrow": "", "color": "grey", "state": "Saved view", "message": "Fetch live data for wall quality."},
        "safer_ce": {"title": "Safer CE Sell", "level": f"{_fmt_num(display.get('safer_sell_ce'))} CE", "arrow": "", "color": "grey", "state": "Saved view", "message": "Fetch live data for safer strike quality."},
        "safer_pe": {"title": "Safer PE Sell", "level": f"{_fmt_num(display.get('safer_sell_pe'))} PE", "arrow": "", "color": "grey", "state": "Saved view", "message": "Fetch live data for safer strike quality."},
    }


def render_level_cards(display: Dict[str, Any]) -> None:
    cards = display.get("cards") or _fallback_cards(display)
    cols = st.columns(5)
    keys = ["poc", "ce_wall", "pe_wall", "safer_ce", "safer_pe"]
    for col, key in zip(cols, keys):
        with col:
            render_trade_card(cards.get(key, {}))
    alert = str(display.get("premium_alert") or "").strip()
    if alert:
        st.markdown(f'<div class="premium-alert">{_html_escape(alert)}</div>', unsafe_allow_html=True)


def render_final_hero(display: Dict[str, Any]) -> None:
    text = display.get("hero_action") or display.get("final_action") or "No ODME action generated."
    render_hero("ODME Action", str(text).replace("\n", "<br>"), _tint_for_action(text, ""))


def render_anchor_comparison(display: Dict[str, Any], comparison: pd.DataFrame) -> None:
    with st.expander("Verify anchor comparison", expanded=False):
        anchor_ts = str(display.get("anchor_snapshot_ts") or "").strip()
        if anchor_ts:
            st.caption(f"Anchor used: latest saved snapshot before today — {anchor_ts}")
        if comparison is None or comparison.empty:
            st.info("No prior anchor available for this instrument+expiry yet. Current values will become the next trading-session anchor after saving.")
            return
        st.dataframe(comparison, use_container_width=True, hide_index=True, height=210)


def render_path_risk(display: Dict[str, Any]) -> None:
    path_risk = display.get("path_risk") or {}
    if not path_risk:
        return
    rows = []
    for key, label in [("upside", "Upside to CE wall"), ("downside", "Downside to PE wall")]:
        item = path_risk.get(key) or {}
        if not item:
            continue
        rows.append({
            "Path": label,
            "Speed": item.get("path", "No clear read"),
            "Wall": _fmt_num(item.get("wall")),
            "Read": item.get("read", ""),
        })
    if not rows:
        return
    st.markdown("### ODME path assist")
    st.caption("Option-buying context: Sharp means fewer positioning clusters before the wall; Grind means price may have to work through clusters.")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=118)


def render_top_cards(display: Dict[str, Any]) -> None:
    tilt = display.get("tilt", "MIXED / NO CLEAN EDGE")
    top = st.columns(5)
    with top[0]:
        render_card("ODME Tilt", tilt, display.get("ts", "current"), _tint_for_tilt(tilt))
    with top[1]:
        render_card("Future LTP", _fmt_num(display.get("spot"), 2), "Verified Angel futures", "tint-grey")
    with top[2]:
        render_card("Option POC", _fmt_num(display.get("poc")), display.get("poc_move", ""), "tint-blue")
    with top[3]:
        render_card("CE Wall", _fmt_num(display.get("ce_wall")), display.get("ce_wall_move", ""), "tint-red")
    with top[4]:
        render_card("PE Wall", _fmt_num(display.get("pe_wall")), display.get("pe_wall_move", ""), "tint-green")


def render_action_sections(display: Dict[str, Any]) -> None:
    tilt = display.get("tilt", "MIXED / NO CLEAN EDGE")
    final_action = display.get("final_action") or "No final action generated."
    render_hero("2. Final Action", final_action, _tint_for_action(final_action, _tint_for_tilt(tilt)))

    ce_col, pe_col = st.columns(2)
    with ce_col:
        ce_line = f"Active CE: {_fmt_num(display.get('ce_wall'))}  |  Safer CE: {_fmt_num(display.get('safer_sell_ce'))}"
        ce_action = display.get("ce_action", "CE side has no strong confirmation yet.")
        render_action_card("3A. CE Action", ce_line, ce_action, _tint_for_action(ce_action, "tint-red"))
    with pe_col:
        pe_line = f"Active PE: {_fmt_num(display.get('pe_wall'))}  |  Safer PE: {_fmt_num(display.get('safer_sell_pe'))}"
        pe_action = display.get("pe_action", "PE side has no strong confirmation yet.")
        render_action_card("3B. PE Action", pe_line, pe_action, _tint_for_action(pe_action, "tint-green"))


def render_previous_and_scores(comparison: pd.DataFrame, display: Dict[str, Any]) -> None:
    left, right = st.columns([1.05, 1.0])
    with left:
        st.markdown("### 4. Previous vs Current")
        if comparison is None or comparison.empty:
            st.info("First usable snapshot for this expiry. The next fetch will show previous-vs-current comparison.")
        else:
            st.dataframe(comparison, use_container_width=True, hide_index=True, height=210)
    with right:
        render_score_bars_from_values(display.get("scores", {}))


def render_chain_heatmap(live_result: Optional[Dict[str, Any]]) -> None:
    # Full option-chain rows are intentionally not saved. Show this only for the
    # current live fetch result, as a compact read-only confirmation table.
    if not live_result:
        return
    st.markdown("### Live option-chain read — ATM ± 10 strikes")
    chain_view = build_chain_view(live_result, radius=10)
    if chain_view.empty:
        st.info("No strike table available from current live result.")
    else:
        st.caption("Clean OTM buildup only: upside strikes show CE read; downside strikes show PE read.")
        st.dataframe(
            chain_view,
            use_container_width=True,
            hide_index=True,
            height=460,
            column_config={
                "Strike": st.column_config.NumberColumn("Strike", format="%.0f", width="small"),
                "Buildup": st.column_config.TextColumn("Buildup", width="large"),
                "CE LTP": st.column_config.NumberColumn("CE LTP", format="%.2f", width="small"),
                "PE LTP": st.column_config.NumberColumn("PE LTP", format="%.2f", width="small"),
            },
        )


def render_expandable_commentary(display: Dict[str, Any]) -> None:
    sections = display.get("sections", {}) or {}
    st.markdown("### 7. Summary & detailed commentary")
    c1, c2 = st.columns(2)
    with c1:
        render_hero("ODME Verdict", display.get("verdict_text") or display.get("tilt", ""), _tint_for_tilt(display.get("tilt", "")))
    with c2:
        render_hero("Risk Note", display.get("risk_note", ""), _tint_for_action(display.get("risk_note", ""), _tint_for_tilt(display.get("tilt", ""))))

    with st.expander("Open full ODME commentary", expanded=False):
        ordered = ["Session Read", "What changed", "Positioning", "Walls", "CE Action", "PE Action", "Heads-up", "Final Action", "Risk Note"]
        shown = set()
        for k in ordered:
            if sections.get(k):
                st.markdown(f"**{k}:** {sections[k]}")
                shown.add(k)
        for k, v in sections.items():
            if k not in shown and v:
                st.markdown(f"**{k}:** {v}")
        if not sections and display.get("commentary"):
            st.write(display.get("commentary"))


def render_matrix(live_result: Optional[Dict[str, Any]]) -> None:
    with st.expander("Compact OI / premium matrix", expanded=False):
        if not live_result:
            st.info("Matrix is available only immediately after a live fetch. Saved summary keeps the final read, not strike-by-strike raw matrix.")
            return
        matrix = live_result.get("matrix", pd.DataFrame())
        if matrix is None or matrix.empty:
            st.info("Matrix becomes meaningful from the second saved snapshot.")
        else:
            show_cols = ["strike", "side", "current_oi", "current_ltp", "delta_oi_vs_previous", "delta_premium_vs_previous", "spot_adjusted_read", "action_tag"]
            st.dataframe(matrix[[c for c in show_cols if c in matrix.columns]].sort_values(["strike", "side"]), use_container_width=True, hide_index=True)


def render_odme_dashboard(display: Dict[str, Any], comparison: pd.DataFrame, live_result: Optional[Dict[str, Any]] = None) -> None:
    if live_result and live_result.get("error"):
        render_data_line(display, live_result)
        return
    render_data_line(display, live_result)
    render_spot_futures_card(display)
    render_level_cards(display)
    render_final_hero(display)
    render_path_risk(display)
    render_chain_heatmap(live_result)
    render_anchor_comparison(display, comparison)



def _previous_row_for_saved_view(history: pd.DataFrame, latest_row: Dict[str, Any]) -> Dict[str, Any]:
    """Return the row used to rebuild the last saved comparison.

    Prefer the latest snapshot strictly before the latest saved snapshot's local
    date, because the live engine uses prior-session anchor logic. If that is not
    available, fall back to the immediate previous saved row.
    """
    if history is None or history.empty or not latest_row:
        return {}
    df = history.copy()
    df["_ts"] = pd.to_datetime(df.get("ts"), errors="coerce", utc=True)
    df = df.dropna(subset=["_ts"]).sort_values("_ts")
    if df.empty:
        return {}
    latest_ts = pd.to_datetime(latest_row.get("ts"), errors="coerce", utc=True)
    if pd.isna(latest_ts):
        return df.iloc[-2].drop(labels=["_ts"], errors="ignore").to_dict() if len(df) >= 2 else {}
    # Use Asia/Kolkata session date to match the store's anchor convention.
    try:
        latest_date = latest_ts.tz_convert("Asia/Kolkata").date()
        df["_local_date"] = df["_ts"].dt.tz_convert("Asia/Kolkata").dt.date
        prior_session = df[df["_local_date"] < latest_date]
        if not prior_session.empty:
            return prior_session.iloc[-1].drop(labels=["_ts", "_local_date"], errors="ignore").to_dict()
    except Exception:
        pass
    before_latest = df[df["_ts"] < latest_ts]
    if before_latest.empty:
        return {}
    return before_latest.iloc[-1].drop(labels=["_ts", "_local_date"], errors="ignore").to_dict()


def render_history(history: pd.DataFrame) -> None:
    if history is None or history.empty:
        return
    with st.expander("8. Snapshot history for selected expiry", expanded=False):
        cols = ["ts", "odme_tilt", "spot", "option_poc", "ce_wall", "pe_wall", "poc_shift", "ce_wall_shift", "pe_wall_shift", "bullish_score", "bearish_score", "range_score", "expansion_score"]
        show = history[[c for c in cols if c in history.columns]].sort_values("ts", ascending=False)
        st.dataframe(show, use_container_width=True, hide_index=True)
        chart_cols = [c for c in ["spot", "option_poc", "ce_wall", "pe_wall"] if c in history.columns]
        if chart_cols:
            chart_df = history.sort_values("ts").copy()
            chart_df["ts"] = pd.to_datetime(chart_df["ts"], errors="coerce")
            for c in chart_cols:
                chart_df[c] = pd.to_numeric(chart_df[c], errors="coerce")
            chart_df = chart_df.dropna(subset=["ts"]).set_index("ts")
            if not chart_df.empty:
                st.line_chart(chart_df[chart_cols])



def _active_expiries(option_rows: pd.DataFrame) -> List[str]:
    """Return non-expired option expiries in chronological order (India date)."""
    if option_rows is None or option_rows.empty or "expiry" not in option_rows.columns:
        return []
    temp = option_rows[["expiry", "expiry_dt"]].drop_duplicates().copy()
    today = pd.Timestamp(datetime.now(ZoneInfo("Asia/Kolkata")).date())
    if "expiry_dt" in temp.columns:
        temp = temp[temp["expiry_dt"].isna() | (temp["expiry_dt"] >= today)]
    temp = temp.sort_values(["expiry_dt", "expiry"], na_position="last")
    return temp["expiry"].dropna().astype(str).unique().tolist()


def _instrument_options(settings: pd.DataFrame) -> List[str]:
    if settings is None or settings.empty or "instrument" not in settings.columns:
        return []
    active = [str(x).upper().strip() for x in settings["instrument"].tolist() if str(x).strip()]
    active_set = set(active)
    defaults = [x for x in SUPPORTED_INSTRUMENTS if x in active_set]
    customs = sorted(x for x in active_set if x not in set(SUPPORTED_INSTRUMENTS))
    return defaults + customs


def _setting_for(settings: pd.DataFrame, instrument: str) -> Dict[str, Any]:
    if settings is None or settings.empty:
        return {}
    rows = settings[settings["instrument"].astype(str).str.upper().eq(str(instrument).upper())]
    return rows.iloc[-1].to_dict() if not rows.empty else {}


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _first_line(text: Any, max_len: int = 220) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    if len(line) > max_len:
        line = line[: max_len - 3].rstrip() + "..."
    return line


def _expansion_label(score: float) -> str:
    if score >= 75:
        return "HIGH"
    if score >= 55:
        return "ELEVATED"
    if score >= 35:
        return "WATCH"
    return "LOW"


def _move_arrow(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "higher" in text or "up" in text:
        return "↑"
    if "lower" in text or "down" in text:
        return "↓"
    if "same" in text or "unchanged" in text or "stable" in text:
        return "="
    return ""


def _batch_instrument_summary(instrument: str, expiry: str, outcome: Dict[str, Any]) -> str:
    result = outcome.get("result", {}) or {}
    scores = result.get("scores", {}) or {}
    cards = result.get("cards", {}) or {}
    path = result.get("path_risk", {}) or {}

    expansion = float(scores.get("Expansion", 0) or 0)
    ce_wall = result.get("ce_wall")
    pe_wall = result.get("pe_wall")
    safer_ce = result.get("safer_sell_ce")
    safer_pe = result.get("safer_sell_pe")
    poc = result.get("poc")

    ce_arrow = _move_arrow(result.get("ce_wall_move"))
    pe_arrow = _move_arrow(result.get("pe_wall_move"))
    poc_arrow = _move_arrow(result.get("poc_move"))

    up_path = path.get("upside", {}) or {}
    dn_path = path.get("downside", {}) or {}
    premium_alert = _first_line(result.get("premium_alert"))
    action = _first_line(result.get("hero_action") or result.get("final_action"))
    poc_card = cards.get("poc", {}) or {}
    poc_state = _first_line(poc_card.get("state"))

    read_bits: List[str] = []
    if poc_state:
        read_bits.append(poc_state)
    if premium_alert:
        clean_premium = premium_alert.replace("Premium alert:", "").strip()
        if clean_premium:
            read_bits.append(clean_premium)
    read_text = " ".join(read_bits).strip()

    lines = [
        f"{instrument} | {expiry} — {result.get('tilt', 'NA')}",
        "",
        (
            f"Market: {_fmt_num(outcome.get('future_ltp') or result.get('spot'), 2)}"
            f" | POC {_fmt_num(poc, 0)} {poc_arrow}"
            f" | Expansion {_expansion_label(expansion)}"
        ),
        f"CE: Wall {_fmt_num(ce_wall, 0)} {ce_arrow} | Safer {_fmt_num(safer_ce, 0)}",
        f"PE: Wall {_fmt_num(pe_wall, 0)} {pe_arrow} | Safer {_fmt_num(safer_pe, 0)}",
        f"Path: Up {up_path.get('path', 'NA')} | Down {dn_path.get('path', 'NA')}",
    ]
    if read_text:
        lines.append(f"Read: {read_text}")
    if action:
        lines.append(f"Action: {action}")
    return "\n".join(lines)


def _run_enabled_batch_scan() -> Dict[str, Any]:
    """Immediately scan every active instrument marked Enable Scan."""
    store = get_store()
    settings = store.list_instrument_settings(active_only=True)
    if settings is None or settings.empty:
        raise RuntimeError("No active instruments are configured.")

    enabled = settings[settings["scan_enabled"].apply(_as_bool)].copy()
    if enabled.empty:
        raise RuntimeError("No instruments have Enable Scan turned on.")

    angel = AngelConnector(load_angel_credentials())
    angel.login_automatic()
    master = angel.load_instrument_master()

    blocks: List[str] = []
    details: List[str] = []
    ok_count = 0

    for _, row in enabled.iterrows():
        item = row.to_dict()
        instrument = str(item.get("instrument", "")).upper().strip()
        expiry = str(item.get("selected_expiry", "")).strip()
        if not instrument:
            continue
        if not expiry:
            blocks.append(f"{instrument} | Expiry not saved\nSCAN ERROR: Select and save an expiry in the dashboard first.")
            details.append(f"{instrument}: missing saved expiry")
            continue
        try:
            outcome = run_odme_scan(
                store,
                angel,
                master,
                instrument,
                expiry,
                save_only_if_changed=True,
            )
            blocks.append(_batch_instrument_summary(instrument, expiry, outcome))
            details.append(
                f"{instrument}: OK; changed={outcome.get('changed')} saved={outcome.get('saved')}"
            )
            ok_count += 1
        except Exception as exc:
            blocks.append(f"{instrument} | {expiry}\nSCAN ERROR: {type(exc).__name__}: {exc}")
            details.append(f"{instrument}: ERROR — {exc}")

    if not blocks:
        raise RuntimeError("No enabled instruments could be scanned.")

    return {
        "instrument_count": len(blocks),
        "ok_count": ok_count,
        "details": details,
        "blocks": blocks,
    }


def render_manual_batch_scan(key_suffix: str) -> None:
    st.subheader("Manual Scan All")
    st.caption("Scans every instrument with Enable Scan switched on, saves changed ODME state, and shows the consolidated result here.")
    if st.button("Scan All Enabled", type="primary", use_container_width=True, key=f"manual_scan_all_{key_suffix}"):
        with st.spinner("Automatic Angel login → scanning enabled instruments..."):
            try:
                report = _run_enabled_batch_scan()
                st.success(
                    f"Completed {report['ok_count']}/{report['instrument_count']} scan(s)."
                )
                for block in report.get("blocks", []):
                    st.text(block)
                failed = [x for x in report.get("details", []) if "ERROR" in x or "missing" in x]
                if failed:
                    st.warning("Some instruments need attention: " + " | ".join(failed))
            except Exception as exc:
                st.error(f"Manual batch scan failed: {exc}")


def render_history_management(store: Any, instrument: str) -> None:
    """Authenticated destructive history controls for the selected instrument."""
    with st.expander("History data management", expanded=False):
        st.caption(
            "Finished-expiry ODME snapshots are cleaned automatically. "
            "TradingView current state is live truth and is never deleted here."
        )
        history_instruments = {str(instrument).upper().strip()}
        try:
            odme_hist = store.load_odme_history(limit=100000)
            if odme_hist is not None and not odme_hist.empty and "instrument" in odme_hist.columns:
                history_instruments.update(
                    str(x).upper().strip() for x in odme_hist["instrument"] if str(x).strip()
                )
        except Exception:
            pass
        try:
            tv_current = store.load_tv_current()
            if tv_current is not None and not tv_current.empty and "instrument" in tv_current.columns:
                history_instruments.update(
                    str(x).upper().strip() for x in tv_current["instrument"] if str(x).strip()
                )
        except Exception:
            pass
        try:
            sb_trades = store.list_superbrain_trades()
            if sb_trades is not None and not sb_trades.empty and "instrument" in sb_trades.columns:
                history_instruments.update(
                    str(x).upper().strip() for x in sb_trades["instrument"] if str(x).strip()
                )
        except Exception:
            pass
        history_instruments = sorted(x for x in history_instruments if x)
        default_index = history_instruments.index(str(instrument).upper().strip()) if str(instrument).upper().strip() in history_instruments else 0
        history_instrument = st.selectbox(
            "Instrument history",
            history_instruments,
            index=default_index,
            key="history_management_instrument",
        )
        history_types = st.multiselect(
            "History to delete",
            ["ODME snapshots", "SuperBrain memory + trades"],
            key="history_types_delete",
        )
        period = st.selectbox(
            "Period",
            ["Today", "Last 7 days", "Last 30 days", "Custom", "All history"],
            key="history_period_delete",
        )

        today = datetime.now(ZoneInfo("Asia/Singapore")).date()
        start_date = end_date = None
        if period == "Today":
            start_date = end_date = today
        elif period == "Last 7 days":
            start_date, end_date = today - timedelta(days=6), today
        elif period == "Last 30 days":
            start_date, end_date = today - timedelta(days=29), today
        elif period == "Custom":
            dates = st.date_input(
                "Date range",
                value=(today - timedelta(days=7), today),
                key="history_dates_delete",
            )
            if isinstance(dates, (list, tuple)) and len(dates) == 2:
                start_date, end_date = dates[0], dates[1]
            else:
                st.info("Select both start and end dates.")

        confirmed = st.checkbox(
            f"Confirm deletion for {history_instrument}",
            key="history_confirm_delete",
        )
        if st.button(
            "Delete selected history",
            key="history_delete_selected",
            use_container_width=True,
            disabled=not bool(history_types) or not confirmed or (period == "Custom" and (start_date is None or end_date is None)),
        ):
            deleted_odme = 0
            deleted_sb = 0
            try:
                if "ODME snapshots" in history_types:
                    deleted_odme = store.delete_odme_history(history_instrument, start_date, end_date)
                if "SuperBrain memory + trades" in history_types:
                    deleted_sb = store.delete_superbrain_history(history_instrument, start_date, end_date)
                st.success(
                    f"Deleted {deleted_odme} ODME snapshot(s) and {deleted_sb} SuperBrain memory/trade row(s) for {history_instrument}."
                )
            except Exception as exc:
                st.error(f"History deletion failed: {exc}")


def main_page() -> None:
    inject_css()
    store = get_store()
    app_header(store)
    render_manual_batch_scan("main")
    st.markdown("---")
    angel: AngelConnector = st.session_state.angel
    master: pd.DataFrame = st.session_state.master

    # Expired option history is not comparable. Clean it once per India date.
    try:
        _run_expired_cleanup_once(store, show_notice=True)
    except Exception as exc:
        st.warning(f"Expired-history cleanup could not run: {exc}")

    with st.sidebar:
        st.header("Instrument")

        try:
            settings_df = store.list_instrument_settings(active_only=True)
        except Exception as exc:
            st.error(f"Could not load persistent instrument list: {exc}")
            st.stop()

        instrument_options = _instrument_options(settings_df)
        if not instrument_options:
            st.warning("No active instruments. Add one below.")

        with st.expander("Manage instruments", expanded=not bool(instrument_options)):
            new_instrument = st.text_input(
                "Add stock / instrument",
                placeholder="e.g. RELIANCE",
                key="new_instrument_input",
            ).upper().strip()
            if st.button("Add to dropdown", key="add_instrument_btn", use_container_width=True):
                if not new_instrument:
                    st.warning("Enter an instrument symbol first.")
                else:
                    try:
                        new_options = angel.get_option_rows(master, new_instrument)
                        new_futures = angel.get_future_rows(master, new_instrument)
                        new_expiries = _active_expiries(new_options)
                        if new_options.empty:
                            st.error(f"No Angel option contracts found for {new_instrument}.")
                        elif new_futures.empty:
                            st.error(f"No Angel futures contract found for {new_instrument}; ODME requires futures data.")
                        elif not new_expiries:
                            st.error(f"No active option expiry found for {new_instrument}.")
                        else:
                            store.upsert_instrument_setting(new_instrument, active=True)
                            st.success(f"{new_instrument} added to the persistent dropdown.")
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Could not add {new_instrument}: {exc}")

        if not instrument_options:
            st.stop()

        instrument = st.selectbox("Select instrument", instrument_options, index=0)
        current_setting = _setting_for(settings_df, instrument)

        option_rows = angel.get_option_rows(master, instrument)
        expiries = _active_expiries(option_rows)
        if not expiries:
            st.error("No active expiries found in Angel master for this instrument.")
            st.stop()

        saved_expiry = str(current_setting.get("selected_expiry", "")).strip()
        expiry_index = expiries.index(saved_expiry) if saved_expiry in expiries else 0
        expiry = st.selectbox("Select expiry (manual)", expiries, index=expiry_index)
        key = make_key(instrument, expiry)

        st.caption(
            "This exact expiry is used for the dashboard and for Manual Scan All. It never auto-rolls to another expiry."
        )
        st.caption(f"Saved batch-scan expiry: {saved_expiry or 'Not set'}")

        with st.expander("Manual batch scan", expanded=True):
            scan_enabled = st.checkbox(
                "Enable Scan",
                value=_as_bool(current_setting.get("scan_enabled", False)),
                key=f"scan_enabled_{instrument}",
                help="When enabled, this instrument is included whenever Scan All Enabled is pressed and is eligible for scheduled/manual batch ODME scans. Manual SuperBrain scans use the saved expiry whenever one is configured.",
            )

            if st.button("Save expiry + scan setting", key=f"save_scan_{instrument}", use_container_width=True):
                store.upsert_instrument_setting(
                    instrument,
                    active=True,
                    selected_expiry=expiry,
                    scan_enabled=scan_enabled,
                    email_alert=False,
                    scan_times="",
                    last_run_slot="",
                )
                st.success(
                    f"Saved: {instrument} / {expiry} / "
                    + ("included in Manual Scan All" if scan_enabled else "not included in Manual Scan All")
                )
                st.rerun()

        with st.expander("Remove instrument", expanded=False):
            st.caption("Removes it from the dropdown and disables scans. Existing unexpired ODME snapshots are not deleted here.")
            if st.button(f"Remove {instrument} from dropdown", key=f"remove_{instrument}", use_container_width=True):
                store.deactivate_instrument(instrument)
                st.success(f"{instrument} removed from the dropdown.")
                st.rerun()

        render_history_management(store, instrument)

        st.caption("Spot/future is fetched from the related Angel futures contract only. If futures LTP or contract mapping cannot be verified, ODME stops instead of assuming data.")
        st.caption(f"Option contracts found: {len(option_rows[option_rows['expiry'].astype(str).eq(str(expiry))])}")
        fetch = st.button("Fetch Live + Save ODME Summary", type="primary")

    if fetch:
        with st.spinner("Fetching live Angel chain, creating ODME commentary, saving compact summary..."):
            try:
                res = fetch_analyze_save(store, angel, master, instrument, expiry, force=True)
                if res and res["usable"] > 0:
                    st.success(f"ODME summary saved. Future LTP: {res.get('future_ltp', 0):,.2f}. Usable OI contracts: {res['usable']}. Comparison uses the latest saved snapshot before today as the fixed anchor. Full chain rows were not saved.")
                elif res:
                    st.warning("Summary saved, but this expiry has no usable OI. Select another active expiry.")
            except AngelSessionError as exc:
                st.error(str(exc))
                st.info("This is an Angel session issue. Enter a fresh TOTP only if the app says the session is inactive/expired or Streamlit Cloud restarted.")
            except AngelDataError as exc:
                st.error(str(exc))
                st.info("ODME did not save a snapshot because the live data was not verified. Try another expiry/instrument or fetch again after Angel quotes update.")
            except Exception as exc:
                st.error(f"Unexpected fetch error: {exc}")

    st.markdown("---")
    st.subheader(f"Selected: {instrument} / {expiry}")
    live_result = st.session_state.get("last_result_by_key", {}).get(key)
    history = store.load_odme_history(key, limit=30)

    if live_result:
        display = result_to_display(live_result)
        comparison = build_comparison_table(live_result)
        render_odme_dashboard(display, comparison, live_result=live_result)
    else:
        saved = store.load_latest_odme_snapshot(key)
        if saved:
            previous_saved = _previous_row_for_saved_view(history, saved)
            st.info("Showing last saved ODME summary. Cards and comparison are rebuilt from the last saved anchor; click Fetch Live + Save ODME Summary only when you want a fresh live update.")
            display = saved_row_to_display(saved, previous_saved)
            comparison = build_comparison_table({**reconstruct_saved_result(parse_previous_summary(saved), parse_previous_summary(previous_saved) if previous_saved else {}), "_previous_summary": parse_previous_summary(previous_saved) if previous_saved else {}, "anchor_snapshot_ts": previous_saved.get("ts", "") if previous_saved else ""}) if previous_saved else build_saved_comparison_table(history)
            render_odme_dashboard(display, comparison, live_result=None)
        else:
            st.info("No saved ODME summary for this instrument+expiry yet. Click Fetch Live + Save ODME Summary.")

    # Snapshot history intentionally hidden in final action-first UI.


def main() -> None:
    init_session()
    if not st.session_state.logged_in:
        login_page()
    else:
        main_page()


if __name__ == "__main__":
    main()
