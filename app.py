from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

from angel_connector import AngelConnector, load_angel_credentials
from data_store import get_store, make_key, make_snapshot_id, utc_now_iso
from odme_config import APP_NAME, MCX_SYMBOLS, REFRESH_INTERVAL_SECONDS, SUPPORTED_INSTRUMENTS
from odme_engine import analyze_odme

st.set_page_config(page_title="ODME Angel", layout="wide")


def init_session() -> None:
    defaults = {
        "logged_in": False,
        "angel": None,
        "master": None,
        "last_refresh_by_key": {},
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

    with st.expander("Deployment check"):
        st.markdown(
            """
            For URL deployment, keep real credentials only in **Streamlit Secrets**:

            ```toml
            ANGEL_API_KEY = "..."
            ANGEL_CLIENT_ID = "..."
            ANGEL_PIN = "..."
            ```

            For local testing, create `config/angel_credentials.json` from the template.
            """
        )


def app_header(store) -> None:
    left, right = st.columns([3, 1])
    with left:
        st.title(APP_NAME)
    with right:
        if st.button("Logout"):
            for k in ["logged_in", "angel", "master"]:
                st.session_state[k] = False if k == "logged_in" else None
            st.rerun()
    mode = "Google Sheets" if store.__class__.__name__ == "GoogleSheetStore" else "Local files"
    st.caption(f"Memory mode: {mode}. Streamlit refreshes only while the app is open.")


def initialize_or_refresh(store, angel: AngelConnector, master: pd.DataFrame, instrument: str, expiry: str, force: bool = False) -> Optional[Dict[str, Any]]:
    key = make_key(instrument, expiry)
    now = datetime.now(timezone.utc)
    last_map = st.session_state.get("last_refresh_by_key", {})
    last = last_map.get(key)
    if (not force) and last:
        age = (now - last).total_seconds()
        if age < REFRESH_INTERVAL_SECONDS:
            return None

    chain, info = angel.fetch_option_chain_snapshot(master, instrument, expiry)
    snapshot_id = make_snapshot_id(key)
    ts = utc_now_iso()
    usable = int((pd.to_numeric(chain.get("oi", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum())
    snapshot = {
        "snapshot_id": snapshot_id,
        "key": key,
        "ts": ts,
        "instrument": instrument,
        "exchange": info.get("exchange", ""),
        "expiry": expiry,
        "spot": "",
        "future": "",
        "source": "Angel SmartAPI FULL",
        "chain_rows": len(chain),
        "usable_oi_count": usable,
        "notes": f"unfetched={info.get('unfetched_count', 0)}",
    }
    store.append_snapshot(snapshot, chain)
    status = "ACTIVE" if usable > 0 else "NO_USABLE_OI"
    notes = "OK" if usable > 0 else "Selected expiry has no usable OI. Choose another active expiry."
    store.upsert_initialized({
        "key": key,
        "instrument": instrument,
        "exchange": info.get("exchange", ""),
        "expiry": expiry,
        "initialized_at": ts if not store.is_initialized(key) else "",
        "last_fetch_at": ts,
        "option_count": len(chain),
        "usable_oi_count": usable,
        "status": status,
        "notes": notes,
    })
    last_map[key] = now
    st.session_state.last_refresh_by_key = last_map
    return {"snapshot": snapshot, "info": info, "usable": usable}


def render_dashboard(result: Dict[str, Any]) -> None:
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

    tab1, tab2, tab3 = st.tabs(["Key Matrix", "Strike Table", "HVN / LVN"])
    with tab1:
        matrix = result.get("matrix", pd.DataFrame())
        if matrix.empty:
            st.info("Matrix will build after at least one saved snapshot. More useful after multiple snapshots.")
        else:
            st.dataframe(matrix, use_container_width=True, hide_index=True)
    with tab2:
        cols = ["strike", "combined_oi", "ce_oi", "pe_oi", "ce_buildup", "pe_buildup", "combined_volume", "ce_wall_score", "pe_wall_score"]
        table = result.get("strike_table", pd.DataFrame())
        st.dataframe(table[[c for c in cols if c in table.columns]].sort_values("strike"), use_container_width=True, hide_index=True)
    with tab3:
        st.write("HVN/friction zones")
        st.dataframe(pd.DataFrame(result.get("hvn", [])), use_container_width=True, hide_index=True)
        st.write("LVN/vacuum zones")
        st.dataframe(pd.DataFrame(result.get("lvn", [])), use_container_width=True, hide_index=True)


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
        init = st.button("Initialize Expiry / Fetch Snapshot", type="primary")
        refresh = st.button("Manual Refresh Snapshot")
        st.divider()
        st.write("Initialized expiries")
        init_df = store.list_initialized()
        if init_df.empty:
            st.caption("None yet.")
        else:
            st.dataframe(init_df.tail(10), use_container_width=True, hide_index=True)

    if init or refresh:
        with st.spinner("Fetching Angel option-chain FULL data and saving snapshot..."):
            try:
                res = initialize_or_refresh(store, angel, master, instrument, expiry, force=True)
                if res and res["usable"] > 0:
                    st.success(f"Snapshot saved. Usable OI contracts: {res['usable']}.")
                elif res:
                    st.warning("Snapshot saved, but this expiry has no usable OI. Select another active expiry.")
            except Exception as exc:
                st.error(str(exc))

    # Auto hourly refresh for initialized selected key while app is active.
    if store.is_initialized(key):
        try:
            auto_res = initialize_or_refresh(store, angel, master, instrument, expiry, force=False)
            if auto_res:
                st.toast("Hourly ODME snapshot refreshed.")
        except Exception as exc:
            st.warning(f"Auto refresh failed: {exc}")

    st.markdown("---")
    st.subheader(f"Selected: {instrument} / {expiry}")
    if not store.is_initialized(key):
        st.info("This instrument+expiry is not initialized yet. Click Initialize Expiry / Fetch Snapshot.")
        return

    chain_memory = store.load_chain_memory(key)
    snapshots = store.load_snapshots(key)
    if chain_memory.empty:
        st.warning("No chain memory rows found yet. Fetch a snapshot again.")
        return
    st.caption(f"Memory rows: {len(chain_memory):,} | Snapshots: {len(snapshots):,}")
    result = analyze_odme(chain_memory, instrument, manual_spot if manual_spot > 0 else None)
    render_dashboard(result)


def main() -> None:
    init_session()
    if not st.session_state.logged_in:
        login_page()
    else:
        main_page()


if __name__ == "__main__":
    main()
