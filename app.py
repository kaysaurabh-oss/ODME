from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

from angel_connector import AngelConnector, load_angel_credentials
from data_store import get_store, make_key, make_snapshot_id, parse_previous_summary, utc_now_iso
from odme_config import APP_NAME, REFRESH_INTERVAL_SECONDS, SUPPORTED_INSTRUMENTS
from odme_engine import analyze_odme

st.set_page_config(page_title="ODME Angel", layout="wide")


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


def app_header(store) -> None:
    left, right = st.columns([3, 1])
    with left:
        st.title(APP_NAME)
        st.caption("Light Google Sheets mode: live chain is fetched from Angel, only ODME summary/commentary is saved.")
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


def _to_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def render_live_dashboard(result: Dict[str, Any]) -> None:
    if result.get("error"):
        st.warning(result["error"])
        return
    st.subheader(result["tilt"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spot/Future Proxy", f"{result['spot']:,.2f}")
    c2.metric("Tradable Option POC", f"{result['poc']:,.0f}", result.get("poc_move", ""))
    c3.metric("Value Area", f"{result['value_area_low']:,.0f}–{result['value_area_high']:,.0f}")
    c4.metric("Range", result.get("range_move", ""))

    scores = result.get("scores", {})
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Bullish Score", scores.get("Bullish", 0))
    s2.metric("Bearish Score", scores.get("Bearish", 0))
    s3.metric("Range Score", scores.get("Range", 0))
    s4.metric("Expansion Score", scores.get("Expansion", 0))

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Safer Sell CE", f"{result['safer_sell_ce']:,.0f}")
    g2.metric("Active CE Wall", f"{result['ce_wall']:,.0f}", result.get("ce_wall_move", ""))
    g3.metric("Safer Sell PE", f"{result['safer_sell_pe']:,.0f}")
    g4.metric("Active PE Wall", f"{result['pe_wall']:,.0f}", result.get("pe_wall_move", ""))

    st.markdown("### Plain-English ODME commentary")
    st.write(result["commentary"])

    tab1, tab2, tab3 = st.tabs(["Compact Matrix", "Current Strike Table", "HVN / LVN"])
    with tab1:
        matrix = result.get("matrix", pd.DataFrame())
        if matrix.empty:
            st.info("Matrix will become meaningful from the second saved snapshot.")
        else:
            st.dataframe(matrix, use_container_width=True, hide_index=True)
    with tab2:
        table = result.get("strike_table", pd.DataFrame())
        cols = ["strike", "combined_oi", "ce_oi", "pe_oi", "combined_volume", "ce_wall_score", "pe_wall_score", "ce_ltp", "pe_ltp"]
        if table.empty:
            st.info("No strike table available.")
        else:
            st.dataframe(table[[c for c in cols if c in table.columns]].sort_values("strike"), use_container_width=True, hide_index=True)
    with tab3:
        st.write("HVN/friction zones")
        st.dataframe(pd.DataFrame(result.get("hvn", [])), use_container_width=True, hide_index=True)
        st.write("LVN/vacuum zones")
        st.dataframe(pd.DataFrame(result.get("lvn", [])), use_container_width=True, hide_index=True)


def render_saved_summary(row: Dict[str, Any]) -> None:
    if not row:
        st.info("No saved ODME summary yet. Fetch live snapshot first.")
        return
    st.subheader(row.get("odme_tilt", "Saved ODME Summary"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Saved Spot/Future Proxy", f"{_to_float(row.get('spot')):,.2f}")
    c2.metric("Saved Option POC", f"{_to_float(row.get('option_poc')):,.0f}", row.get("poc_shift", ""))
    c3.metric("Saved Value Area", f"{_to_float(row.get('value_area_low')):,.0f}–{_to_float(row.get('value_area_high')):,.0f}")
    c4.metric("Saved Range", row.get("range_shift", ""))

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Bullish Score", row.get("bullish_score", 0))
    s2.metric("Bearish Score", row.get("bearish_score", 0))
    s3.metric("Range Score", row.get("range_score", 0))
    s4.metric("Expansion Score", row.get("expansion_score", 0))

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Safer Sell CE", f"{_to_float(row.get('safer_sell_ce')):,.0f}")
    g2.metric("Active CE Wall", f"{_to_float(row.get('active_ce_wall')):,.0f}", row.get("ce_wall_shift", ""))
    g3.metric("Safer Sell PE", f"{_to_float(row.get('safer_sell_pe')):,.0f}")
    g4.metric("Active PE Wall", f"{_to_float(row.get('active_pe_wall')):,.0f}", row.get("pe_wall_shift", ""))

    st.markdown("### Last saved commentary")
    st.write(row.get("commentary", ""))
    st.caption(f"Last saved at: {row.get('ts', '')}")


def main_page() -> None:
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
        init_df = store.list_initialized()
        if init_df.empty:
            st.caption("None yet.")
        else:
            st.dataframe(init_df.tail(10), use_container_width=True, hide_index=True)

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
            st.info("This is the last saved summary. Click Fetch Live + Save ODME Summary for a fresh Angel read and new comparison.")
        else:
            st.info("No saved ODME summary for this instrument+expiry yet. Click Fetch Live + Save ODME Summary.")

    history = store.load_odme_history(key, limit=20)
    if not history.empty:
        with st.expander("Saved history for selected expiry"):
            cols = ["ts", "odme_tilt", "spot", "option_poc", "ce_wall", "pe_wall", "poc_shift", "ce_wall_shift", "pe_wall_shift", "bullish_score", "bearish_score", "range_score", "expansion_score"]
            st.dataframe(history[[c for c in cols if c in history.columns]].sort_values("ts", ascending=False), use_container_width=True, hide_index=True)


def main() -> None:
    init_session()
    if not st.session_state.logged_in:
        login_page()
    else:
        main_page()


if __name__ == "__main__":
    main()
