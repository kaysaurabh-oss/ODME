from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from angel_connector import AngelConnector, load_angel_credentials
from data_store import BaseStore, make_key
from scan_service import run_odme_scan


def _norm(value: Any) -> str:
    return str(value or "").upper().strip()


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


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


def prepare_superbrain_scan(store: BaseStore, instrument: str) -> Dict[str, Any]:
    """Phase-1 orchestration packet for Ask SuperBrain.

    This does not generate trade decisions yet. It proves the instrument bridge,
    refreshes ODME when the exact matching instrument is enabled for scanning,
    and returns the freshest inputs to the Streamlit terminal.
    """
    instrument = _norm(instrument)
    mapping = build_instrument_map(store)
    matched = mapping[mapping["instrument"].eq(instrument)] if not mapping.empty else pd.DataFrame()
    if matched.empty:
        raise ValueError(f"{instrument} is not present in TV_TEST_CURRENT.")
    map_row = matched.iloc[0].to_dict()

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

    return {
        "instrument": instrument,
        "mode": "TV + ODME" if odme_live else "TV only",
        "tv_rows": tv_rows,
        "tv_sources": sources,
        "mapping": map_row,
        "odme_live": odme_live,
        "odme_outcome": odme_outcome,
        "latest_odme": latest_odme,
        "odme_error": odme_error,
    }
