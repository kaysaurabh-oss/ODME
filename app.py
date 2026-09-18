from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, List

import pandas as pd
import streamlit as st

from angel_connector import AngelConnector, AngelDataError, AngelSessionError, load_angel_credentials
from data_store import get_store, make_key, make_snapshot_id, parse_previous_summary, utc_now_iso
from email_notifier import send_email
from odme_config import APP_NAME, REFRESH_INTERVAL_SECONDS, SUPPORTED_INSTRUMENTS
from odme_engine import analyze_odme, reconstruct_saved_result
from runtime_config import get_list, get_secret
from scan_service import run_odme_scan

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
    st.caption("Manual ODME batch scanning — no background scheduler required.")

    render_manual_batch_scan("login")

    st.markdown("---")
    st.subheader("Angel login")
    st.info("Login is needed only for the interactive dashboard. The Scan All button above uses the stored Angel TOTP secret automatically.")

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
    """Immediately scan every active instrument marked Enable Scan and email one summary."""
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

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    sender = str(get_secret("GMAIL_SENDER", "") or "").strip()
    password = str(get_secret("GMAIL_APP_PASSWORD", "") or "")
    recipients = get_list("ALERT_EMAILS")
    subject = f"ODME Manual Scan — {now_ist.strftime('%d %b %Y %H:%M IST')} — {len(blocks)} instrument(s)"
    body = (
        "ODME MANUAL SCAN\n"
        + now_ist.strftime("%d %b %Y %H:%M IST")
        + "\n\n"
        + ("\n\n" + "-" * 72 + "\n\n").join(blocks)
    )
    sent = send_email(sender, password, recipients, subject, body)
    return {
        "instrument_count": len(blocks),
        "ok_count": ok_count,
        "recipient_count": sent,
        "details": details,
    }


def render_manual_batch_scan(key_suffix: str) -> None:
    st.subheader("Manual Scan All")
    st.caption("Scans every instrument with Enable Scan switched on, saves changed ODME state, and emails one consolidated summary immediately. No GitHub scheduler is used.")
    if st.button("Scan All Enabled + Email", type="primary", use_container_width=True, key=f"manual_scan_all_{key_suffix}"):
        with st.spinner("Automatic Angel login → scanning enabled instruments → sending ODME email..."):
            try:
                report = _run_enabled_batch_scan()
                st.success(
                    f"Completed {report['ok_count']}/{report['instrument_count']} scan(s). "
                    f"Email sent to {report['recipient_count']} recipient(s)."
                )
                failed = [x for x in report.get("details", []) if "ERROR" in x or "missing" in x]
                if failed:
                    st.warning("Some instruments need attention: " + " | ".join(failed))
            except Exception as exc:
                st.error(f"Manual batch scan failed: {exc}")


def main_page() -> None:
    inject_css()
    store = get_store()
    app_header(store)
    render_manual_batch_scan("main")
    st.markdown("---")
    angel: AngelConnector = st.session_state.angel
    master: pd.DataFrame = st.session_state.master

    # Expired option history is not comparable. Clean it once per India date.
    india_date = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    if st.session_state.get("last_expiry_cleanup_date") != india_date:
        try:
            cleanup = store.cleanup_expired_data(tz_name="Asia/Kolkata")
            st.session_state["last_expiry_cleanup_date"] = india_date
            if cleanup.get("deleted_snapshots", 0) or cleanup.get("cleared_scan_settings", 0):
                st.toast(
                    f"Expired ODME cleanup: {cleanup.get('deleted_snapshots', 0)} snapshot(s) deleted; "
                    f"{cleanup.get('cleared_scan_settings', 0)} scan setting(s) cleared."
                )
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
                help="When enabled, this instrument is included whenever Scan All Enabled + Email is pressed.",
            )

            if st.button("Save expiry + scan setting", key=f"save_scan_{instrument}", use_container_width=True):
                store.upsert_instrument_setting(
                    instrument,
                    active=True,
                    selected_expiry=expiry,
                    scan_enabled=scan_enabled,
                    email_alert=scan_enabled,
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
