from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import pandas as pd
import streamlit as st

from angel_connector import AngelConnector, load_angel_credentials
from data_store import get_store, make_key, make_snapshot_id, parse_previous_summary, utc_now_iso
from odme_config import APP_NAME, REFRESH_INTERVAL_SECONDS, SUPPORTED_INSTRUMENTS
from odme_engine import analyze_odme

st.set_page_config(page_title="ODME Angel", layout="wide")


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
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def login_page() -> None:
    inject_css()
    st.title(APP_NAME)
    st.subheader("Angel login")
    st.info("Enter only the current Angel TOTP. API key, Client ID and PIN are read from Streamlit Secrets or local config.")

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
            st.success("Angel login successful. Instrument master loaded.")
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
        .block-container {padding-top: 1.15rem; padding-bottom: 2rem;}
        h1 {font-size: 1.55rem !important; margin-bottom: 0.15rem !important;}
        h2 {font-size: 1.22rem !important;}
        h3 {font-size: 1.03rem !important;}
        .odme-card {
            border: 1px solid rgba(120,120,120,0.22);
            border-radius: 14px;
            padding: 0.75rem 0.85rem;
            min-height: 82px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.05);
            background: rgba(250,250,250,0.62);
        }
        .odme-card .label {
            font-size: 0.72rem;
            letter-spacing: 0.02rem;
            color: rgba(90,90,90,0.95);
            margin-bottom: 0.22rem;
            text-transform: uppercase;
            font-weight: 700;
        }
        .odme-card .value {
            font-size: 1.05rem;
            font-weight: 800;
            line-height: 1.18;
            color: rgba(20,20,20,0.95);
        }
        .odme-card .sub {
            font-size: 0.76rem;
            color: rgba(90,90,90,0.95);
            margin-top: 0.30rem;
        }
        .tint-green {background: linear-gradient(135deg, rgba(31,181,90,0.14), rgba(255,255,255,0.65)); border-color: rgba(31,181,90,0.28);}
        .tint-red {background: linear-gradient(135deg, rgba(221,65,65,0.14), rgba(255,255,255,0.65)); border-color: rgba(221,65,65,0.28);}
        .tint-amber {background: linear-gradient(135deg, rgba(232,159,34,0.18), rgba(255,255,255,0.65)); border-color: rgba(232,159,34,0.35);}
        .tint-blue {background: linear-gradient(135deg, rgba(65,125,220,0.14), rgba(255,255,255,0.65)); border-color: rgba(65,125,220,0.28);}
        .tint-grey {background: linear-gradient(135deg, rgba(120,120,120,0.12), rgba(255,255,255,0.65)); border-color: rgba(120,120,120,0.25);}
        .hero-card {
            border-radius: 16px;
            padding: 0.95rem 1.05rem;
            border: 1px solid rgba(70,70,70,0.18);
            background: linear-gradient(135deg, rgba(245,247,250,1), rgba(255,255,255,0.92));
            box-shadow: 0 2px 10px rgba(0,0,0,0.06);
            margin-top: 0.25rem;
            margin-bottom: 0.80rem;
        }
        .hero-card .label {font-size: 0.75rem; font-weight: 800; color: rgba(80,80,80,0.92); text-transform: uppercase; letter-spacing: 0.04rem;}
        .hero-card .value {font-size: 1.05rem; font-weight: 750; margin-top: 0.18rem; line-height: 1.42;}
        .small-note {font-size: 0.78rem; color: rgba(90,90,90,0.96);}
        div[data-testid="stMetric"] {background: rgba(250,250,250,0.45); border: 1px solid rgba(120,120,120,0.16); border-radius: 12px; padding: 0.55rem 0.65rem;}
        div[data-testid="stMetricLabel"] {font-size: 0.72rem !important;}
        div[data-testid="stMetricValue"] {font-size: 1.00rem !important;}
        div[data-testid="stMetricDelta"] {font-size: 0.70rem !important;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _html_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_num(value: Any, decimals: int = 0) -> str:
    try:
        x = float(value)
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
    if "failing" in t or "do not sell" in t or "avoid" in t or "reduce" in t:
        return "tint-red"
    if "under pressure" in t or "safer" in t or "wait" in t or "not clean" in t:
        return "tint-amber"
    if "working" in t or "control" in t or "acceptable" in t:
        return "tint-green"
    return default


def render_card(label: str, value: Any, sub: str = "", tint: str = "tint-grey") -> None:
    st.markdown(
        f"""
        <div class="odme-card {tint}">
            <div class="label">{_html_escape(label)}</div>
            <div class="value">{_html_escape(value)}</div>
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
            <div class="value">{_html_escape(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_score_bars(scores: Dict[str, Any]) -> None:
    st.markdown("### Scores")
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


def split_commentary(commentary: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    for part in str(commentary or "").split("\n\n"):
        if ":" in part:
            k, v = part.split(":", 1)
            sections[k.strip()] = v.strip()
    return sections


def build_comparison_table(result: Dict[str, Any]) -> pd.DataFrame:
    prev = result.get("_previous_summary") or {}
    if not prev:
        return pd.DataFrame()
    rows = [
        {"Metric": "Spot", "Previous": _fmt_num(prev.get("spot"), 2), "Current": _fmt_num(result.get("spot"), 2), "Change": _fmt_num(_safe_float(result.get("spot")) - _safe_float(prev.get("spot")), 2)},
        {"Metric": "Option POC", "Previous": _fmt_num(prev.get("poc") or prev.get("option_poc")), "Current": _fmt_num(result.get("poc")), "Change": result.get("poc_move", "")},
        {"Metric": "CE Wall", "Previous": _fmt_num(prev.get("ce_wall")), "Current": _fmt_num(result.get("ce_wall")), "Change": result.get("ce_wall_move", "")},
        {"Metric": "PE Wall", "Previous": _fmt_num(prev.get("pe_wall")), "Current": _fmt_num(result.get("pe_wall")), "Change": result.get("pe_wall_move", "")},
        {"Metric": "Tilt", "Previous": prev.get("tilt") or prev.get("odme_tilt") or "NA", "Current": result.get("tilt", "NA"), "Change": ""},
    ]
    return pd.DataFrame(rows)


def build_chain_view(result: Dict[str, Any], radius: int = 8) -> pd.DataFrame:
    table = result.get("strike_table", pd.DataFrame())
    if table is None or table.empty:
        return pd.DataFrame()
    df = table.copy()
    spot = _safe_float(result.get("spot"))
    if spot:
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
        ce_delta = m[m["side"].eq("CE")][["strike", "delta_oi_vs_previous", "delta_premium_vs_previous", "spot_adjusted_read", "action_tag"]].rename(columns={
            "delta_oi_vs_previous": "CE ΔOI",
            "delta_premium_vs_previous": "CE ΔPrem",
            "spot_adjusted_read": "CE Read",
            "action_tag": "CE State",
        })
        pe_delta = m[m["side"].eq("PE")][["strike", "delta_oi_vs_previous", "delta_premium_vs_previous", "spot_adjusted_read", "action_tag"]].rename(columns={
            "delta_oi_vs_previous": "PE ΔOI",
            "delta_premium_vs_previous": "PE ΔPrem",
            "spot_adjusted_read": "PE Read",
            "action_tag": "PE State",
        })
    out = df.merge(ce_delta, on="strike", how="left").merge(pe_delta, on="strike", how="left")
    rename = {
        "strike": "Strike",
        "ce_ltp": "CE LTP",
        "ce_oi": "CE OI",
        "pe_ltp": "PE LTP",
        "pe_oi": "PE OI",
        "combined_oi": "Combined OI",
    }
    keep = ["strike", "ce_ltp", "ce_oi", "CE ΔOI", "CE ΔPrem", "pe_ltp", "pe_oi", "PE ΔOI", "PE ΔPrem", "combined_oi", "CE Read", "PE Read"]
    out = out[[c for c in keep if c in out.columns]].rename(columns=rename).sort_values("Strike")
    for col in ["CE ΔOI", "CE ΔPrem", "PE ΔOI", "PE ΔPrem"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
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
    if "CE OI" in df.columns:
        styler = styler.applymap(lambda v: oi_tint(v, "CE"), subset=["CE OI"])
    if "PE OI" in df.columns:
        styler = styler.applymap(lambda v: oi_tint(v, "PE"), subset=["PE OI"])
    if "Combined OI" in df.columns:
        styler = styler.applymap(combined_tint, subset=["Combined OI"])
    for col in ["CE ΔOI", "CE ΔPrem", "PE ΔOI", "PE ΔPrem"]:
        if col in df.columns:
            styler = styler.applymap(delta_tint, subset=[col])
    numeric_cols = [c for c in df.columns if c not in ["CE Read", "PE Read"]]
    return styler.format({c: "{:,.0f}" for c in numeric_cols if c not in ["CE LTP", "PE LTP", "CE ΔPrem", "PE ΔPrem"]}).format({c: "{:,.2f}" for c in ["CE LTP", "PE LTP", "CE ΔPrem", "PE ΔPrem"] if c in df.columns})


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
            for k in ["logged_in", "angel", "master"]:
                st.session_state[k] = False if k == "logged_in" else None
            st.rerun()
    mode = "Google Sheets" if store.__class__.__name__ == "GoogleSheetStore" else "Local files"
    st.caption(f"Memory mode: {mode}. Stored tab: odme_snapshots. Full option-chain rows are not saved.")


def fetch_analyze_save(store, angel: AngelConnector, master: pd.DataFrame, instrument: str, expiry: str, manual_spot: Optional[float], force: bool = True) -> Optional[Dict[str, Any]]:
    key = make_key(instrument, expiry)
    now = datetime.now(timezone.utc)
    last_map = st.session_state.get("last_refresh_by_key", {})
    last = last_map.get(key)
    if (not force) and last:
        age = (now - last).total_seconds()
        if age < REFRESH_INTERVAL_SECONDS:
            return None

    previous_raw = store.load_latest_odme_snapshot(key)
    previous = parse_previous_summary(previous_raw)

    chain, info = angel.fetch_option_chain_snapshot(master, instrument, expiry)
    usable = int((pd.to_numeric(chain.get("oi", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum())
    result = analyze_odme(chain, instrument, manual_spot if manual_spot and manual_spot > 0 else None, previous_summary=previous)
    result["_previous_summary"] = previous

    snapshot_id = make_snapshot_id(key)
    ts = utc_now_iso()
    status_note = "OK" if usable > 0 else "Selected expiry has no usable OI. Choose another active expiry."
    meta = {
        "snapshot_id": snapshot_id,
        "key": key,
        "ts": ts,
        "instrument": instrument,
        "exchange": info.get("exchange", ""),
        "expiry": expiry,
        "source": "Angel SmartAPI FULL → ODME compact summary",
        "usable_oi_count": usable,
        "notes": f"{status_note}; contracts={len(chain)}; unfetched={info.get('unfetched_count', 0)}",
    }
    store.append_odme_snapshot(result, meta)
    last_map[key] = now
    st.session_state.last_refresh_by_key = last_map
    st.session_state.last_result_by_key[key] = result
    return {"result": result, "meta": meta, "usable": usable, "contracts": len(chain)}


def render_live_dashboard(result: Dict[str, Any]) -> None:
    if result.get("error"):
        st.warning(result["error"])
        return

    sections = split_commentary(result.get("commentary", ""))
    tilt = result.get("tilt", "MIXED / NO CLEAN EDGE")

    # 1. Compact top cards
    top = st.columns(5)
    with top[0]:
        render_card("ODME Tilt", tilt, "current verdict", _tint_for_tilt(tilt))
    with top[1]:
        render_card("Spot", _fmt_num(result.get("spot"), 2), "Angel proxy / manual override", "tint-grey")
    with top[2]:
        render_card("Option POC", _fmt_num(result.get("poc")), result.get("poc_move", ""), "tint-blue")
    with top[3]:
        render_card("CE Wall", _fmt_num(result.get("ce_wall")), result.get("ce_wall_move", ""), "tint-red")
    with top[4]:
        render_card("PE Wall", _fmt_num(result.get("pe_wall")), result.get("pe_wall_move", ""), "tint-green")

    # 2. Hero action
    render_hero("Final Action", result.get("final_action") or sections.get("Final Action", "No final action generated."), _tint_for_action(result.get("final_action", ""), _tint_for_tilt(tilt)))

    # 3. CE / PE action cards
    ce_col, pe_col = st.columns(2)
    with ce_col:
        render_card(
            "CE Side",
            f"Active: {_fmt_num(result.get('ce_wall'))} | Safer: {_fmt_num(result.get('safer_sell_ce'))}",
            result.get("ce_action", ""),
            _tint_for_action(result.get("ce_action", ""), "tint-red"),
        )
    with pe_col:
        render_card(
            "PE Side",
            f"Active: {_fmt_num(result.get('pe_wall'))} | Safer: {_fmt_num(result.get('safer_sell_pe'))}",
            result.get("pe_action", ""),
            _tint_for_action(result.get("pe_action", ""), "tint-green"),
        )

    # 4. Previous vs current + scores
    left, right = st.columns([1.08, 1.0])
    with left:
        st.markdown("### Previous vs Current")
        comparison = build_comparison_table(result)
        if comparison.empty:
            st.info("First snapshot for this expiry. The next fetch will show previous-vs-current comparison.")
        else:
            st.dataframe(comparison, use_container_width=True, hide_index=True)
    with right:
        render_score_bars(result.get("scores", {}))

    # 5. Option chain heatmap
    st.markdown("### Key option chain heatmap — ATM ± 8 strikes")
    chain_view = build_chain_view(result, radius=8)
    if chain_view.empty:
        st.info("No strike table available.")
    else:
        st.caption("Tint guide: red = CE OI concentration, green = PE OI concentration, blue = combined OI/POC area, amber border = ATM row. This is visual aid only; action still comes from ODME summary.")
        st.dataframe(style_chain_table(chain_view, result), use_container_width=True, hide_index=True, height=560)

    # 6. Readable commentary
    visible_cols = st.columns([1, 1])
    with visible_cols[0]:
        render_hero("ODME Verdict", sections.get("ODME Verdict", tilt), _tint_for_tilt(tilt))
    with visible_cols[1]:
        render_hero("Risk Note", sections.get("Risk Note", "No risk note generated."), _tint_for_tilt(tilt))

    with st.expander("Detailed ODME commentary", expanded=False):
        commentary = result.get("commentary", "")
        for block in str(commentary).split("\n\n"):
            if not block.strip():
                continue
            if ":" in block:
                head, body = block.split(":", 1)
                st.markdown(f"**{head.strip()}**: {body.strip()}")
            else:
                st.write(block)

    with st.expander("Compact OI / premium matrix", expanded=False):
        matrix = result.get("matrix", pd.DataFrame())
        if matrix is None or matrix.empty:
            st.info("Matrix becomes meaningful from the second saved snapshot.")
        else:
            show_cols = ["strike", "side", "current_oi", "current_ltp", "delta_oi_vs_previous", "delta_premium_vs_previous", "spot_adjusted_read", "action_tag"]
            st.dataframe(matrix[[c for c in show_cols if c in matrix.columns]].sort_values(["strike", "side"]), use_container_width=True, hide_index=True)


def render_saved_summary(row: Dict[str, Any]) -> None:
    if not row:
        st.info("No saved ODME summary yet. Fetch live snapshot first.")
        return
    tilt = row.get("odme_tilt", "Saved ODME Summary")
    top = st.columns(5)
    with top[0]:
        render_card("Last Saved Tilt", tilt, row.get("ts", ""), _tint_for_tilt(tilt))
    with top[1]:
        render_card("Saved Spot", _fmt_num(row.get("spot"), 2), "", "tint-grey")
    with top[2]:
        render_card("Saved POC", _fmt_num(row.get("option_poc")), row.get("poc_shift", ""), "tint-blue")
    with top[3]:
        render_card("Saved CE Wall", _fmt_num(row.get("active_ce_wall")), row.get("ce_wall_shift", ""), "tint-red")
    with top[4]:
        render_card("Saved PE Wall", _fmt_num(row.get("active_pe_wall")), row.get("pe_wall_shift", ""), "tint-green")

    render_hero("Last Saved Commentary", row.get("commentary", ""), _tint_for_tilt(tilt))
    st.info("This is the last saved summary. Click Fetch Live + Save ODME Summary for a fresh Angel read and new comparison.")


def main_page() -> None:
    inject_css()
    store = get_store()
    app_header(store)
    angel: AngelConnector = st.session_state.angel
    master: pd.DataFrame = st.session_state.master

    with st.sidebar:
        st.header("Instrument")
        instrument = st.selectbox("Select instrument", SUPPORTED_INSTRUMENTS, index=0)
        option_rows = angel.get_option_rows(master, instrument)
        expiries = angel.get_expiries(master, instrument)
        if not expiries:
            st.error("No expiries found in Angel master for this instrument.")
            st.stop()
        expiry = st.selectbox("Select expiry", expiries, index=0)
        key = make_key(instrument, expiry)
        manual_spot = st.number_input("Optional manual spot/future override", min_value=0.0, value=0.0, step=1.0)
        st.caption(f"Contracts found: {len(option_rows[option_rows['expiry'].astype(str).eq(str(expiry))])}")
        fetch = st.button("Fetch Live + Save ODME Summary", type="primary")
        auto_on = st.checkbox("Auto-refresh hourly while app is open", value=False)
        st.divider()
        st.write("Saved ODME summaries")
        try:
            init_df = store.list_initialized()
            if init_df.empty:
                st.caption("None yet.")
            else:
                st.dataframe(init_df.tail(10), use_container_width=True, hide_index=True, height=240)
        except Exception as exc:
            st.warning(f"Could not load saved summary list: {exc}")

    if fetch:
        with st.spinner("Fetching live Angel chain, creating ODME commentary, saving compact summary..."):
            try:
                res = fetch_analyze_save(store, angel, master, instrument, expiry, manual_spot, force=True)
                if res and res["usable"] > 0:
                    st.success(f"ODME summary saved. Usable OI contracts: {res['usable']}. Full chain rows were not saved.")
                elif res:
                    st.warning("Summary saved, but this expiry has no usable OI. Select another active expiry.")
            except Exception as exc:
                st.error(str(exc))

    if auto_on and store.is_initialized(key):
        try:
            auto_res = fetch_analyze_save(store, angel, master, instrument, expiry, manual_spot, force=False)
            if auto_res:
                st.toast("Hourly ODME summary refreshed.")
        except Exception as exc:
            st.warning(f"Auto refresh failed: {exc}")

    st.markdown("---")
    st.subheader(f"Selected: {instrument} / {expiry}")
    live_result = st.session_state.get("last_result_by_key", {}).get(key)
    if live_result:
        render_live_dashboard(live_result)
    else:
        saved = store.load_latest_odme_snapshot(key)
        if saved:
            render_saved_summary(saved)
        else:
            st.info("No saved ODME summary for this instrument+expiry yet. Click Fetch Live + Save ODME Summary.")

    history = store.load_odme_history(key, limit=30)
    if not history.empty:
        with st.expander("Saved history for selected expiry", expanded=False):
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


def main() -> None:
    init_session()
    if not st.session_state.logged_in:
        login_page()
    else:
        main_page()


if __name__ == "__main__":
    main()
