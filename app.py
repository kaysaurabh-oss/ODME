from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from angel_connector import AngelConfigError, AngelConnector
from data_store import (
    append_snapshot,
    is_initialized,
    list_initialized,
    load_memory,
    register_initialized,
    reset_memory,
    should_refresh,
)
from odme_config import APP_NAME, ALL_UNDERLYINGS, REFRESH_MINUTES
from odme_engine import analyze_memory

st.set_page_config(page_title="ODME Angel", page_icon="📈", layout="wide")


def css() -> None:
    st.markdown(
        """
<style>
.block-container {padding-top: 1.4rem; padding-bottom: 2rem;}
.metric-card {border:1px solid rgba(128,128,128,0.22); border-radius:16px; padding:14px 16px; background:rgba(128,128,128,0.06);}
.good {color:#0a8f3c; font-weight:700;}
.bad {color:#c62828; font-weight:700;}
.warn {color:#b26a00; font-weight:700;}
.small {font-size:0.85rem; opacity:0.8;}
</style>
        """,
        unsafe_allow_html=True,
    )


def get_connector() -> AngelConnector:
    if "angel" not in st.session_state:
        st.session_state.angel = AngelConnector()
    return st.session_state.angel


@st.cache_data(ttl=3600, show_spinner=False)
def load_master(force_refresh: bool = False) -> pd.DataFrame:
    return AngelConnector.load_instrument_master(force_refresh=force_refresh)


def login_page() -> None:
    st.title(APP_NAME)
    st.subheader("Angel login")
    st.caption("Only current Angel TOTP is entered here. API key, client ID and PIN are read from local config/angel_credentials.json.")

    try:
        connector = get_connector()
    except AngelConfigError as e:
        st.error(str(e))
        st.code('Copy config/angel_credentials_TEMPLATE.json to config/angel_credentials.json and fill it locally.', language="text")
        st.stop()

    with st.form("login_form", clear_on_submit=False):
        totp = st.text_input("Current Angel TOTP", type="password", placeholder="6-digit TOTP")
        submitted = st.form_submit_button("Login", type="primary")
    if submitted:
        try:
            with st.spinner("Logging in to Angel SmartAPI..."):
                info = connector.login(totp)
                st.session_state.logged_in = True
                st.session_state.client_id = info.get("client_id")
            st.success("Angel login successful.")
            st.rerun()
        except Exception as e:
            st.error(f"Login failed: {e}")


def refresh_initialized(connector: AngelConnector, master: pd.DataFrame) -> None:
    refreshed = []
    skipped = []
    for item in list_initialized():
        symbol = item.get("symbol")
        expiry = item.get("expiry")
        if not symbol or not expiry:
            continue
        if should_refresh(symbol, expiry, REFRESH_MINUTES):
            try:
                chain, spot, meta = connector.fetch_option_chain(master, symbol, expiry)
                append_snapshot(symbol, expiry, chain, spot)
                refreshed.append(f"{symbol} {expiry} ({meta.get('non_zero_oi', 0)} non-zero OI)")
            except Exception as e:
                skipped.append(f"{symbol} {expiry}: {e}")
    if refreshed:
        st.toast("Refreshed: " + "; ".join(refreshed[:3]))
    if skipped:
        with st.expander("Some initialized expiries could not refresh", expanded=False):
            for x in skipped:
                st.warning(x)


def sidebar_controls(master: pd.DataFrame):
    st.sidebar.header("Instrument")
    symbol = st.sidebar.selectbox("Underlying", ALL_UNDERLYINGS, index=0)
    expiries = AngelConnector.expiries(master, symbol)
    if not expiries:
        st.sidebar.error(f"No Angel expiries found for {symbol}.")
        st.stop()
    expiry = st.sidebar.selectbox("Expiry", expiries, index=0)
    st.sidebar.divider()
    force = st.sidebar.button("Force refresh selected expiry")
    reset = st.sidebar.button("Reset selected memory", help="Deletes local memory for this instrument + expiry only.")
    return symbol, expiry, force, reset


def initialize_or_refresh(connector: AngelConnector, master: pd.DataFrame, symbol: str, expiry: str, force: bool, reset: bool) -> bool:
    initialized = is_initialized(symbol, expiry)
    if reset:
        reset_memory(symbol, expiry)
        st.warning(f"Local memory reset for {symbol} {expiry}.")
        initialized = False

    if not initialized:
        st.info(f"{symbol} {expiry} is not initialized yet. Click Initialize Expiry to start memory from the current Angel chain.")
        if st.button("Initialize Expiry", type="primary"):
            try:
                with st.spinner("Fetching full current option chain from Angel and starting memory..."):
                    chain, spot, meta = connector.fetch_option_chain(master, symbol, expiry)
                    non_zero = int((chain["oi"] > 0).sum())
                    if non_zero <= 0:
                        st.error("Selected expiry has no usable OI. Choose another active expiry.")
                        return False
                    rows = append_snapshot(symbol, expiry, chain, spot)
                    register_initialized(symbol, expiry, {"spot_at_init": spot, **meta})
                st.success(f"Initialized {symbol} {expiry}: {rows} rows saved, {non_zero} contracts with non-zero OI.")
                st.rerun()
            except Exception as e:
                st.error(f"Initialization failed: {e}")
                return False
        return False

    if force or should_refresh(symbol, expiry, REFRESH_MINUTES):
        try:
            with st.spinner("Fetching latest selected-expiry snapshot from Angel..."):
                chain, spot, meta = connector.fetch_option_chain(master, symbol, expiry)
                non_zero = int((chain["oi"] > 0).sum())
                if non_zero <= 0:
                    st.error("Selected expiry has no usable OI. Choose another active expiry.")
                    return True
                rows = append_snapshot(symbol, expiry, chain, spot)
            st.success(f"Snapshot appended: {rows} rows, {non_zero} contracts with non-zero OI.")
        except Exception as e:
            st.warning(f"Refresh failed. Existing memory will still be analyzed. Error: {e}")
    return True


def badge(decision: str) -> str:
    if "BULLISH" in decision:
        return "good"
    if "BEARISH" in decision or "TRAP" in decision:
        return "bad"
    if "RANGE" in decision:
        return "warn"
    return ""


def render_dashboard(symbol: str, expiry: str) -> None:
    memory = load_memory(symbol, expiry)
    if memory.empty:
        st.warning("No local memory found for this selection.")
        return
    analysis = analyze_memory(memory, symbol)
    st.markdown(f"## <span class='{badge(analysis.decision)}'>{analysis.decision}</span>", unsafe_allow_html=True)
    st.caption(f"Analyzing cumulative memory for {symbol} {expiry}. Latest snapshot: {analysis.meta['latest_snapshot']}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("ODME Tilt", analysis.tilt)
    c2.metric("Spot/Future", f"{analysis.spot:,.2f}")
    c3.metric("Tradable Option POC", f"{analysis.poc:,.2f}")
    c4.metric("Option Value Area", f"{analysis.value_area_low:,.2f} – {analysis.value_area_high:,.2f}")

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Bullish Score", f"{analysis.scores['Bullish']:.0f}")
    s2.metric("Bearish Score", f"{analysis.scores['Bearish']:.0f}")
    s3.metric("Range Score", f"{analysis.scores['Range']:.0f}")
    s4.metric("Expansion Score", f"{analysis.scores['Expansion']:.0f}")

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Safer Sell CE", f"{analysis.safer_sell_ce:,.2f}")
    g2.metric("Active CE Wall", f"{analysis.ce_wall:,.2f}")
    g3.metric("Safer Sell PE", f"{analysis.safer_sell_pe:,.2f}")
    g4.metric("Active PE Wall", f"{analysis.pe_wall:,.2f}")

    st.subheader("Plain-English ODME Commentary")
    for line in analysis.commentary:
        st.write("• " + line)

    with st.expander("Profile chart: combined OI by strike", expanded=True):
        prof = analysis.tables["latest_profile"].copy()
        chart_df = prof.set_index("strike")[["combined_oi", "oi_CE", "oi_PE"]]
        st.bar_chart(chart_df)
        st.caption(f"Raw full-chain max OI background only: {analysis.meta.get('raw_full_chain_poc')}")

    t1, t2 = st.tabs(["Key Strike Matrix", "Wall / HVN / LVN Details"])
    with t1:
        df = analysis.tables["key_matrix"].copy()
        if not df.empty:
            show = df[["strike", "option_type", "current_oi", "oi_change", "current_ltp", "premium_change", "volume", "matrix"]]
            st.dataframe(show, use_container_width=True, hide_index=True)
        else:
            st.info("Not enough history for key-strike matrix yet. It becomes stronger after more snapshots.")
    with t2:
        st.write("Top wall scores")
        walls = analysis.tables["wall_scores"][["strike", "option_type", "current_oi", "buildup", "volume", "persistence", "proximity", "wall_score"]].head(20)
        st.dataframe(walls, use_container_width=True, hide_index=True)
        c1, c2 = st.columns(2)
        with c1:
            st.write("HVN / friction")
            st.dataframe(analysis.tables["hvn"], use_container_width=True, hide_index=True)
        with c2:
            st.write("LVN / vacuum")
            st.dataframe(analysis.tables["lvn"], use_container_width=True, hide_index=True)

    with st.expander("Local memory status", expanded=False):
        st.json(analysis.meta)
        st.write(f"Snapshots saved: {memory['snapshot_ts'].nunique()} | Rows: {len(memory):,}")


def main() -> None:
    css()
    if not st.session_state.get("logged_in"):
        login_page()
        return

    st.title(APP_NAME)
    st.caption(f"Logged in as {st.session_state.get('client_id', 'Angel user')}. Data source: Angel SmartAPI only. No NSE scraping.")

    connector = get_connector()
    try:
        master = load_master(force_refresh=False)
    except Exception as e:
        st.error(f"Could not load Angel instrument master: {e}")
        if st.button("Retry instrument master"):
            load_master.clear()
            st.rerun()
        st.stop()

    refresh_initialized(connector, master)
    symbol, expiry, force, reset = sidebar_controls(master)
    ok = initialize_or_refresh(connector, master, symbol, expiry, force, reset)
    if ok:
        render_dashboard(symbol, expiry)

    st.sidebar.divider()
    if st.sidebar.button("Reload Angel instrument master"):
        load_master.clear()
        AngelConnector.load_instrument_master(force_refresh=True)
        st.rerun()
    st.sidebar.caption(f"Hourly refresh rule: app appends a new snapshot when the last saved snapshot is older than {REFRESH_MINUTES} minutes. Streamlit does not run when closed.")


if __name__ == "__main__":
    main()
