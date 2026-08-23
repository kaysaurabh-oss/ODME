from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

try:
    import streamlit as st
except Exception:  # pragma: no cover
    st = None


def get_secret(name: str, default: Optional[Any] = None) -> Any:
    """Read a setting from Streamlit Secrets first, then environment variables."""
    if st is not None:
        try:
            value = st.secrets.get(name, None)
            if value is not None:
                return value
        except Exception:
            pass
    return os.environ.get(name, default)


def get_bool(name: str, default: bool = False) -> bool:
    value = get_secret(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def get_list(name: str) -> List[str]:
    value = get_secret(name, [])
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [x.strip() for x in text.replace(";", ",").split(",") if x.strip()]


def get_gcp_service_account_info() -> Dict[str, Any]:
    if st is not None:
        try:
            section = st.secrets.get("gcp_service_account", None)
            if section:
                return dict(section)
        except Exception:
            pass

    raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as exc:
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
