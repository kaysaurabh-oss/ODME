from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from odme_config import DEFAULT_RELEVANT_RANGE_PCT, RELEVANT_RANGE_PCT, is_valid_configured_strike


# =============================================================================
# ODME Engine v2
# Focus: compact live ODME summary, previous-vs-current comparison, actionable
# commentary. No full-chain history is required.
# =============================================================================


def _num(series: Any) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _fmt_level(x: Any) -> str:
    x = _safe_float(x)
    return "NA" if not x else f"{x:,.0f}"


def _fmt_points(x: Any) -> str:
    x = _safe_float(x)
    if abs(x) < 0.01:
        return "0"
    return f"{x:+,.1f}"


def _fmt_pct(x: Any) -> str:
    x = _safe_float(x)
    return f"{x:+.2f}%"


def _pct_change(current: float, previous: float) -> float:
    if not previous:
        return 0.0
    return (current - previous) / abs(previous) * 100.0


def _movement(current: float, previous: float, tolerance_pct: float = 0.001) -> str:
    if not current or not previous:
        return "first snapshot"
    diff = current - previous
    if abs(diff) <= max(abs(current), abs(previous), 1) * tolerance_pct:
        return "stable"
    return "higher" if diff > 0 else "lower"


def _range_shift(current_width: float, previous_width: float) -> str:
    if not current_width or not previous_width:
        return "first snapshot"
    if abs(current_width - previous_width) <= max(current_width, previous_width, 1) * 0.01:
        return "stable"
    return "widening" if current_width > previous_width else "narrowing"


def _direction(value: float, tolerance: float = 0.0) -> str:
    if value > tolerance:
        return "up"
    if value < -tolerance:
        return "down"
    return "flat"


def _clean_chain(chain_df: pd.DataFrame) -> pd.DataFrame:
    df = chain_df.copy()
    for col in ["strike", "ltp", "oi", "volume", "bid", "ask", "open", "high", "low", "close"]:
        if col not in df.columns:
            df[col] = 0
        df[col] = _num(df[col])
    if "option_type" not in df.columns:
        df["option_type"] = ""
    df["option_type"] = df["option_type"].astype(str).str.upper().str.strip()
    return df[df["option_type"].isin(["CE", "PE"])].copy()


def infer_spot_from_chain(df: pd.DataFrame, manual_spot: float | None = None) -> float:
    if manual_spot and manual_spot > 0:
        return float(manual_spot)
    if df.empty:
        return 0.0
    pivot = df.pivot_table(index="strike", columns="option_type", values="ltp", aggfunc="mean").fillna(0)
    if "CE" in pivot.columns and "PE" in pivot.columns:
        x = pivot[(pivot["CE"] > 0) & (pivot["PE"] > 0)].copy()
        if not x.empty:
            x["diff"] = (x["CE"] - x["PE"]).abs()
            return float(x.sort_values("diff").index[0])
    oi = df.groupby("strike", as_index=False)["oi"].sum()
    if not oi.empty and oi["oi"].max() > 0:
        return float(oi.sort_values("oi", ascending=False).iloc[0]["strike"])
    return float(df["strike"].median())


def relevant_range(df: pd.DataFrame, instrument: str, spot: float) -> Tuple[float, float]:
    if not spot:
        if df.empty:
            return 0.0, 0.0
        return float(df["strike"].min()), float(df["strike"].max())
    pct = RELEVANT_RANGE_PCT.get(str(instrument).upper(), DEFAULT_RELEVANT_RANGE_PCT)
    return spot * (1 - pct), spot * (1 + pct)


def build_strike_table(chain_df: pd.DataFrame, instrument: str, manual_spot: float | None = None) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    df = _clean_chain(chain_df)
    if df.empty:
        return pd.DataFrame(), {"spot": 0, "range_low": 0, "range_high": 0, "relevant_rows": 0, "latest_rows": 0}

    # Defensive filter for loaded/cached chain rows. Live Angel rows are already
    # filtered in angel_connector.get_option_rows, but this keeps old saved data
    # from reintroducing skipped strikes. ZINC and unconfigured symbols remain unchanged.
    df = df[df["strike"].apply(lambda x: is_valid_configured_strike(instrument, x))].copy()
    if df.empty:
        return pd.DataFrame(), {"spot": 0, "range_low": 0, "range_high": 0, "relevant_rows": 0, "latest_rows": 0}

    spot = infer_spot_from_chain(df, manual_spot)
    lo, hi = relevant_range(df, instrument, spot)
    rel = df[(df["strike"] >= lo) & (df["strike"] <= hi)].copy()
    if rel.empty:
        rel = df.copy()

    agg = rel.groupby(["strike", "option_type"], as_index=False).agg(
        oi=("oi", "sum"),
        ltp=("ltp", "mean"),
        volume=("volume", "sum"),
        bid=("bid", "max"),
        ask=("ask", "min"),
    )
    ce = agg[agg["option_type"].eq("CE")].drop(columns=["option_type"]).rename(columns={
        "oi": "ce_oi", "ltp": "ce_ltp", "volume": "ce_volume", "bid": "ce_bid", "ask": "ce_ask"
    })
    pe = agg[agg["option_type"].eq("PE")].drop(columns=["option_type"]).rename(columns={
        "oi": "pe_oi", "ltp": "pe_ltp", "volume": "pe_volume", "bid": "pe_bid", "ask": "pe_ask"
    })
    strikes = pd.DataFrame({"strike": sorted(set(rel["strike"].astype(float).tolist()))})
    strikes = strikes.merge(ce, on="strike", how="left").merge(pe, on="strike", how="left")
    for col in strikes.columns:
        if col != "strike":
            strikes[col] = _num(strikes[col])
    strikes["combined_oi"] = strikes.get("ce_oi", 0) + strikes.get("pe_oi", 0)
    strikes["combined_volume"] = strikes.get("ce_volume", 0) + strikes.get("pe_volume", 0)
    strikes["distance_pct"] = (strikes["strike"] - spot).abs() / spot if spot else 0

    def norm(col: str) -> pd.Series:
        s = _num(strikes[col]) if col in strikes.columns else pd.Series(0, index=strikes.index)
        mx = s.max()
        return s / mx if mx > 0 else s * 0

    proximity = (1 - (strikes["distance_pct"] / max(strikes["distance_pct"].max(), 1e-9))).clip(0, 1)
    strikes["ce_wall_score"] = 0.55 * norm("ce_oi") + 0.25 * norm("ce_volume") + 0.20 * proximity
    strikes["pe_wall_score"] = 0.55 * norm("pe_oi") + 0.25 * norm("pe_volume") + 0.20 * proximity

    meta = {"spot": spot, "range_low": lo, "range_high": hi, "latest_rows": len(df), "relevant_rows": len(rel)}
    return strikes.sort_values("strike"), meta


def value_area(strikes: pd.DataFrame, pct: float = 0.70) -> Tuple[float, float]:
    if strikes.empty or strikes["combined_oi"].sum() <= 0:
        return 0.0, 0.0
    ordered = strikes.sort_values("strike").reset_index(drop=True)
    poc = float(strikes.sort_values("combined_oi", ascending=False).iloc[0]["strike"])
    idx = int(ordered.index[ordered["strike"].eq(poc)][0])
    selected = {poc}
    total = float(ordered["combined_oi"].sum())
    selected_oi = float(ordered.loc[idx, "combined_oi"])
    left, right = idx - 1, idx + 1
    while selected_oi < total * pct and (left >= 0 or right < len(ordered)):
        left_oi = float(ordered.loc[left, "combined_oi"]) if left >= 0 else -1
        right_oi = float(ordered.loc[right, "combined_oi"]) if right < len(ordered) else -1
        if right_oi >= left_oi:
            selected.add(float(ordered.loc[right, "strike"])); selected_oi += max(right_oi, 0); right += 1
        else:
            selected.add(float(ordered.loc[left, "strike"])); selected_oi += max(left_oi, 0); left -= 1
    return min(selected), max(selected)


def classify_matrix(delta_oi: float, delta_premium: float) -> str:
    # Generic OI-premium matrix. Final interpretation is spot-adjusted separately.
    if delta_oi > 0 and delta_premium > 0:
        return "fresh buying / stress"
    if delta_oi > 0 and delta_premium <= 0:
        return "writing / control"
    if delta_oi <= 0 and delta_premium > 0:
        return "writer covering / failure risk"
    return "long liquidation / interest fading"


def _extract_prev_key_metrics(previous_summary: Dict[str, Any]) -> Dict[str, Any]:
    val = previous_summary.get("key_strikes", {}) if previous_summary else {}
    if isinstance(val, dict):
        return val
    try:
        return json.loads(str(val))
    except Exception:
        return {}


def _make_key_strikes(strikes: pd.DataFrame, spot: float, poc: float, val: float, vah: float, ce_candidates: pd.DataFrame, pe_candidates: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    levels = set([poc, val, vah])
    levels.update(ce_candidates["strike"].astype(float).tolist())
    levels.update(pe_candidates["strike"].astype(float).tolist())
    # Keep a compact but wider ATM band so significant near-ITM changes can be assessed without storing the full chain.
    near = strikes.assign(dist=(strikes["strike"] - spot).abs()).sort_values("dist").head(15)["strike"].astype(float).tolist()
    levels.update(near)
    out: Dict[str, Dict[str, float]] = {}
    for level in sorted(x for x in levels if x and not pd.isna(x)):
        row = strikes[strikes["strike"].eq(level)]
        if row.empty:
            continue
        r = row.iloc[0]
        out[str(int(round(level)))] = {
            "strike": float(level),
            "ce_oi": _safe_float(r.get("ce_oi")), "pe_oi": _safe_float(r.get("pe_oi")),
            "ce_ltp": _safe_float(r.get("ce_ltp")), "pe_ltp": _safe_float(r.get("pe_ltp")),
            "ce_volume": _safe_float(r.get("ce_volume")), "pe_volume": _safe_float(r.get("pe_volume")),
        }
    return out


def _spot_adjusted_read(side: str, spot_delta: float, oi_delta: float, premium_delta: float, premium_pct: float) -> Tuple[str, str, int, int, int, int]:
    """Return: read, action_tag, bullish_pts, bearish_pts, range_pts, expansion_pts.

    The important correction:
    - Spot rising + PE premium not falling is NOT clean put-writing support.
    - Clean support requires PE OI addition plus PE premium decay/softness.
    - If PE premium stays firm/rises while spot rises, downside demand/IV stress is present.
    Mirrored logic is used for CE on falling spot.
    """
    side = side.upper()
    spot_dir = _direction(spot_delta, 0.0)
    prem_dir = _direction(premium_delta, 0.0)
    oi_dir = _direction(oi_delta, 0.0)
    strong_premium = abs(premium_pct) >= 8.0 or abs(premium_delta) >= 5.0

    if spot_dir == "flat":
        if oi_dir == "up" and prem_dir == "down":
            return "writer control while spot is flat", "control", 0, 0, 12, 0
        if oi_dir == "up" and prem_dir == "up":
            return "premium stress despite flat spot", "stress", 0, 0, 0, 12
        if oi_dir == "down" and prem_dir == "up":
            return "writer exit risk despite flat spot", "failure", 0, 0, 0, 10
        return "quiet / low conviction", "neutral", 0, 0, 4, 0

    # CE side
    if side == "CE":
        if spot_dir == "up":
            if oi_dir == "up" and prem_dir == "down":
                return "call writers absorbing the rise", "ce_defended", 0, 8, 14, 0
            if oi_dir == "up" and prem_dir == "flat":
                return "call writers still absorbing; no clean upside stress", "ce_defended_mild", 0, 4, 10, 0
            if oi_dir == "up" and prem_dir == "up":
                pts = 16 if strong_premium else 9
                return "call-side stress with fresh CE OI", "ce_stress", pts, 0, 0, pts
            if oi_dir == "down" and prem_dir == "up":
                return "call writers covering / CE wall risk", "ce_failure", 18, 0, 0, 16
            if oi_dir == "down" and prem_dir in ["down", "flat"]:
                return "call interest fading during rise", "neutral", 4, 0, 3, 0
        else:  # spot falling
            if oi_dir == "up" and prem_dir == "down":
                return "fresh call writing confirms overhead pressure", "ce_control", 0, 16, 8, 0
            if oi_dir == "up" and prem_dir == "flat":
                return "call writing pressure, but premium not decaying enough", "ce_control_mild", 0, 10, 4, 2
            if oi_dir == "up" and prem_dir == "up":
                return "abnormal CE premium firmness on falling spot", "ce_abnormal", 0, 0, 0, 12
            if oi_dir == "down" and prem_dir == "up":
                return "call writer exit / upside hedge demand", "ce_failure", 8, 0, 0, 12
            if oi_dir == "down" and prem_dir in ["down", "flat"]:
                return "CE longs unwinding with falling spot", "ce_weak", 0, 7, 5, 0

    # PE side
    if side == "PE":
        if spot_dir == "up":
            if oi_dir == "up" and prem_dir == "down":
                return "clean put writing support", "pe_support", 18, 0, 10, 0
            if oi_dir == "up" and prem_dir == "flat":
                return "put writers attempting support, but premium is not decaying cleanly", "pe_support_mild", 8, 0, 4, 5
            if oi_dir == "up" and prem_dir == "up":
                return "put premium firm despite rise: downside demand / trap risk", "pe_trap", 0, 0, 0, 18
            if oi_dir == "down" and prem_dir == "up":
                return "put writers exiting while protection demand stays firm", "pe_failure", 0, 16, 0, 16
            if oi_dir == "down" and prem_dir in ["down", "flat"]:
                return "put unwinding during rise; rally accepted but support is weaker", "pe_unwind", 6, 0, 3, 0
        else:  # spot falling
            if oi_dir == "up" and prem_dir == "down":
                return "put writers absorbing the fall", "pe_defended", 10, 0, 14, 0
            if oi_dir == "up" and prem_dir == "flat":
                return "put writers attempting defence; premium not expanding", "pe_defended_mild", 5, 0, 8, 2
            if oi_dir == "up" and prem_dir == "up":
                pts = 18 if strong_premium else 11
                return "fresh put buying / downside stress", "pe_stress", 0, pts, 0, pts
            if oi_dir == "down" and prem_dir == "up":
                return "put writers covering / PE wall risk", "pe_failure", 0, 18, 0, 16
            if oi_dir == "down" and prem_dir in ["down", "flat"]:
                return "put interest fading during fall", "neutral", 0, 4, 3, 0

    return "neutral / inconclusive", "neutral", 0, 0, 0, 0


def _build_matrix(current_keys: Dict[str, Dict[str, float]], previous_keys: Dict[str, Dict[str, float]], current_spot: float, previous_spot: float) -> pd.DataFrame:
    rows = []
    spot_delta = current_spot - previous_spot if previous_spot else 0.0
    spot_pct = _pct_change(current_spot, previous_spot) if previous_spot else 0.0
    for k, cur in current_keys.items():
        prev = previous_keys.get(k, {}) if previous_keys else {}
        strike = _safe_float(cur.get("strike"), _safe_float(k))
        for side in ["ce", "pe"]:
            cur_oi = _safe_float(cur.get(f"{side}_oi"))
            cur_ltp = _safe_float(cur.get(f"{side}_ltp"))
            prev_oi = _safe_float(prev.get(f"{side}_oi"))
            prev_ltp = _safe_float(prev.get(f"{side}_ltp"))
            if cur_oi <= 0 and cur_ltp <= 0:
                continue
            doi = cur_oi - prev_oi if previous_keys else 0.0
            dltp = cur_ltp - prev_ltp if previous_keys else 0.0
            dpct = _pct_change(cur_ltp, prev_ltp) if previous_keys else 0.0
            generic = classify_matrix(doi, dltp) if previous_keys else "first snapshot baseline"
            if previous_keys:
                adj_read, action_tag, b, br, r, e = _spot_adjusted_read(side.upper(), spot_delta, doi, dltp, dpct)
            else:
                adj_read, action_tag, b, br, r, e = "first snapshot baseline", "baseline", 0, 0, 0, 0
            rows.append({
                "strike": strike,
                "side": side.upper(),
                "current_oi": cur_oi,
                "current_ltp": cur_ltp,
                "delta_oi_vs_previous": doi,
                "delta_premium_vs_previous": dltp,
                "premium_change_pct": dpct,
                "spot_change_points": spot_delta,
                "spot_change_pct": spot_pct,
                "matrix_read": generic,
                "spot_adjusted_read": adj_read,
                "action_tag": action_tag,
                "bullish_pts": b,
                "bearish_pts": br,
                "range_pts": r,
                "expansion_pts": e,
            })
    return pd.DataFrame(rows)


def _count_tags(matrix: pd.DataFrame, tags: List[str], side: str | None = None) -> int:
    if matrix.empty or "action_tag" not in matrix.columns:
        return 0
    x = matrix
    if side:
        x = x[x["side"].eq(side.upper())]
    return int(x["action_tag"].isin(tags).sum())


def _sum_col(matrix: pd.DataFrame, col: str, side: str | None = None) -> int:
    if matrix.empty or col not in matrix.columns:
        return 0
    x = matrix
    if side:
        x = x[x["side"].eq(side.upper())]
    return int(pd.to_numeric(x[col], errors="coerce").fillna(0).sum())


def _rows_for_side(matrix: pd.DataFrame, side: str, tags: List[str]) -> pd.DataFrame:
    if matrix.empty or "action_tag" not in matrix.columns:
        return pd.DataFrame()
    return matrix[(matrix["side"].eq(side.upper())) & (matrix["action_tag"].isin(tags))].copy()

def _actionable_matrix(matrix: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Return ATM/OTM rows only for action decisions.

    CE actionable rows are strikes at/above spot; PE actionable rows are strikes at/below spot.
    ITM rows are deliberately excluded from wall/safe-strike and primary hero logic.
    """
    if matrix is None or matrix.empty or not spot:
        return matrix.copy() if isinstance(matrix, pd.DataFrame) else pd.DataFrame()
    m = matrix.copy()
    st = pd.to_numeric(m.get("strike"), errors="coerce")
    return m[((m["side"].eq("CE")) & (st >= spot)) | ((m["side"].eq("PE")) & (st <= spot))].copy()


def _itm_matrix(matrix: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Return ITM rows only. Used only for a single caution overlay, never for wall/safe-strike selection."""
    if matrix is None or matrix.empty or not spot:
        return pd.DataFrame()
    m = matrix.copy()
    st = pd.to_numeric(m.get("strike"), errors="coerce")
    return m[((m["side"].eq("CE")) & (st < spot)) | ((m["side"].eq("PE")) & (st > spot))].copy()


def _significant_itm_caution(matrix: pd.DataFrame, spot: float) -> str:
    """Single ITM overlay based only on significant OI change, not absolute OI.

    This intentionally does not affect wall/safe-strike selection. It only adds a compact
    caution line to the hero when near-ITM activity has materially changed vs anchor.
    """
    itm = _itm_matrix(matrix, spot)
    if itm.empty or "delta_oi_vs_previous" not in itm.columns:
        return ""

    all_abs = pd.to_numeric(matrix.get("delta_oi_vs_previous"), errors="coerce").abs().replace([np.inf, -np.inf], np.nan).dropna()
    all_abs = all_abs[all_abs > 0]
    if all_abs.empty:
        return ""
    threshold = float(all_abs.quantile(0.75))
    if threshold <= 0:
        return ""

    itm = itm.copy()
    itm["abs_oi_change"] = pd.to_numeric(itm["delta_oi_vs_previous"], errors="coerce").abs().fillna(0)
    itm = itm[itm["abs_oi_change"] >= threshold]
    if itm.empty:
        return ""

    # Pick the single strongest ITM change. Do not rank by absolute OI.
    row = itm.sort_values("abs_oi_change", ascending=False).iloc[0]
    side = str(row.get("side", "")).upper()
    strike = _fmt_level(row.get("strike"))
    oi_delta = _safe_float(row.get("delta_oi_vs_previous"))
    prem_delta = _safe_float(row.get("delta_premium_vs_previous"))
    oi_dir = _direction(oi_delta, 0.0)
    prem_dir = _direction(prem_delta, 0.0)

    if side == "CE":
        if oi_dir == "up" and prem_dir == "up":
            return f"ITM caution: bullish upside risk from {strike} CE — do not treat CE selling as clean."
        if oi_dir == "down" and prem_dir == "up":
            return f"ITM caution: bullish upside risk from {strike} CE covering / premium firmness."
        if oi_dir == "up" and prem_dir in ["down", "flat"]:
            return f"ITM caution: bearish/weak-bullish read from {strike} CE adjustment; upside follow-through is not clean."
        if oi_dir == "down" and prem_dir in ["down", "flat"]:
            return f"ITM caution: bearish/weak-bullish read from {strike} CE unwinding."

    if side == "PE":
        if oi_dir == "up" and prem_dir == "up":
            return f"ITM caution: bearish downside risk from {strike} PE — PE selling needs caution."
        if oi_dir == "down" and prem_dir == "up":
            return f"ITM caution: bearish downside risk from {strike} PE covering / premium firmness."
        if oi_dir == "up" and prem_dir in ["down", "flat"]:
            return f"ITM caution: bullish/weak-bearish read from {strike} PE adjustment; downside follow-through is not clean."
        if oi_dir == "down" and prem_dir in ["down", "flat"]:
            return f"ITM caution: bullish/weak-bearish read from {strike} PE unwinding."

    return ""


def _decide_tilt(scores: Dict[str, int]) -> str:
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ordered or ordered[0][1] < 25:
        return "MIXED / NO CLEAN EDGE"
    if len(ordered) > 1 and ordered[0][1] - ordered[1][1] < 8:
        return "MIXED / NO CLEAN EDGE"
    return {
        "Bullish": "BULLISH POSITIONING",
        "Bearish": "BEARISH POSITIONING",
        "Range": "RANGE-BOUND THETA",
        "Expansion": "EXPANSION / TRAP RISK",
    }.get(ordered[0][0], "MIXED / NO CLEAN EDGE")


def _nearest_action_rows(matrix: pd.DataFrame, tags: List[str], side: str, limit: int = 3) -> List[str]:
    x = _rows_for_side(matrix, side, tags)
    if x.empty:
        return []
    x["abs_premium_move"] = pd.to_numeric(x["delta_premium_vs_previous"], errors="coerce").abs().fillna(0)
    x["abs_oi_move"] = pd.to_numeric(x["delta_oi_vs_previous"], errors="coerce").abs().fillna(0)
    x = x.sort_values(["abs_premium_move", "abs_oi_move"], ascending=False).head(limit)
    return [f"{_fmt_level(r['strike'])} {side.upper()} ({str(r['spot_adjusted_read'])})" for _, r in x.iterrows()]


def _side_action(side: str, wall: float, safer: float, wall_move: str, stress_count: int, control_count: int, failure_count: int, support_count: int = 0) -> str:
    side = side.upper()
    if side == "CE":
        if failure_count > 0 or stress_count > control_count:
            return f"Avoid active CE wall at {_fmt_level(wall)} for fresh selling; use safer higher CE around {_fmt_level(safer)} only if chart supply is also present."
        if control_count > 0 and wall_move in ["stable", "lower", "first snapshot"]:
            return f"CE selling is acceptable near/above {_fmt_level(wall)}; do not chase lower unless premium decay continues."
        if wall_move == "higher":
            return f"CE resistance has shifted higher; prefer {_fmt_level(safer)} rather than selling the old/near wall aggressively."
        return f"No high-confidence CE sale at the active wall; keep CE selling conservative near {_fmt_level(safer)}."
    else:
        if failure_count > 0 or stress_count > support_count + control_count:
            return f"Avoid active PE wall at {_fmt_level(wall)} for fresh selling; use safer lower PE around {_fmt_level(safer)} only if chart demand is also present."
        if support_count > 0 and wall_move in ["stable", "higher", "first snapshot"]:
            return f"PE selling is acceptable near/below {_fmt_level(wall)}; support is valid only while PE premium stays soft."
        if wall_move == "lower":
            return f"PE support has shifted lower; prefer {_fmt_level(safer)} and avoid aggressive higher PE selling."
        return f"No high-confidence PE sale at the active wall; keep PE selling conservative near {_fmt_level(safer)}."


def _final_action(tilt: str, ce_action: str, pe_action: str, scores: Dict[str, int], spot: float, poc: float, poc_move: str) -> str:
    if tilt == "BULLISH POSITIONING":
        return pe_action
    if tilt == "BEARISH POSITIONING":
        return ce_action
    if tilt == "RANGE-BOUND THETA":
        if poc and poc_move == "stable":
            return f"Theta trade is acceptable, but sell away from spot; use {_fmt_level(poc)} as magnet, not as a breakout target."
        return "Theta trade only with wider strikes because POC is not stable enough for aggressive mean reversion."
    if tilt == "EXPANSION / TRAP RISK":
        return "No fresh aggressive short option. First reduce risk or move strikes farther away; premium behaviour is not clean enough."
    return "No clean edge. Hold existing risk light and wait for the next fetch to confirm writer control or failure."




def _move_arrow(move: str) -> str:
    move = str(move or "").lower()
    if move == "higher":
        return "↑"
    if move == "lower":
        return "↓"
    return ""


def _get_strike_row(matrix: pd.DataFrame, strike: float, side: str) -> Dict[str, Any]:
    if matrix is None or matrix.empty:
        return {}
    x = matrix[(matrix["side"].eq(side.upper())) & (pd.to_numeric(matrix["strike"], errors="coerce").round(6).eq(round(float(strike or 0), 6)))]
    if x.empty:
        return {}
    return x.iloc[0].to_dict()


def _add_level_to_keys(key_strikes: Dict[str, Dict[str, float]], strikes: pd.DataFrame, level: float) -> None:
    level = _safe_float(level)
    if not level or strikes is None or strikes.empty:
        return
    row = strikes[pd.to_numeric(strikes["strike"], errors="coerce").round(6).eq(round(level, 6))]
    if row.empty:
        return
    r = row.iloc[0]
    key_strikes[str(int(round(level)))] = {
        "strike": float(level),
        "ce_oi": _safe_float(r.get("ce_oi")), "pe_oi": _safe_float(r.get("pe_oi")),
        "ce_ltp": _safe_float(r.get("ce_ltp")), "pe_ltp": _safe_float(r.get("pe_ltp")),
        "ce_volume": _safe_float(r.get("ce_volume")), "pe_volume": _safe_float(r.get("pe_volume")),
    }


def _wall_card(side: str, wall: float, previous_wall: float, wall_move: str, matrix: pd.DataFrame) -> Dict[str, Any]:
    side = side.upper()
    row = _get_strike_row(matrix, wall, side)
    prev_row = _get_strike_row(matrix, previous_wall, side) if previous_wall else {}
    tag = str(row.get("action_tag", ""))
    prev_tag = str(prev_row.get("action_tag", ""))
    arrow = _move_arrow(wall_move)

    if wall_move == "first snapshot":
        return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": "", "color": "grey", "state": "Observation mode", "message": "No prior anchor for wall quality."}

    failure_tags = {"ce_failure", "pe_failure"}
    stress_tags = {"ce_stress", "ce_abnormal", "pe_stress", "pe_trap"}
    control_tags = {"ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild", "pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"}
    weak_prev_tags = failure_tags | stress_tags | {"ce_weak", "pe_unwind", "neutral"}

    if tag in failure_tags:
        return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": arrow, "color": "red", "state": "Wall failure / covering", "message": "Do not treat as clean writer control."}
    if wall_move not in ["stable", "first snapshot"] and prev_tag in weak_prev_tags:
        return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": arrow, "color": "orange", "state": f"Wall shifted after {_fmt_level(previous_wall)} failure", "message": "New wall is defensive, not clean yet."}
    if tag in stress_tags:
        return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": arrow, "color": "orange", "state": "Wall defence under pressure", "message": "Writers active, but premium pressure is not clean."}
    if tag in control_tags:
        return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": arrow, "color": "green", "state": "Clean wall holding", "message": "Writer control is currently clean."}
    if wall_move not in ["stable", "first snapshot"]:
        return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": arrow, "color": "orange", "state": "Wall shifted", "message": "Treat migration as caution until confirmed."}
    return {"title": f"{side} Wall", "level": f"{_fmt_level(wall)} {side}", "arrow": arrow, "color": "orange", "state": "Not clean", "message": "Wall exists, but writer quality is unclear."}


def _safer_card(side: str, safer: float, previous_safer: float, wall_move: str, matrix: pd.DataFrame) -> Dict[str, Any]:
    side = side.upper()
    row = _get_strike_row(matrix, safer, side)
    tag = str(row.get("action_tag", ""))
    move = _movement(safer, previous_safer)
    arrow = _move_arrow(move)
    if move == "first snapshot":
        arrow = ""
        return {"title": f"Safer {side} Sell", "level": f"{_fmt_level(safer)} {side}", "arrow": arrow, "color": "grey", "state": "Observation mode", "message": "No anchor to confirm safer strike quality."}

    failure_tags = {"ce_failure", "pe_failure"}
    stress_tags = {"ce_stress", "ce_abnormal", "pe_stress", "pe_trap"}
    control_tags = {"ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild", "pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"}

    if tag in failure_tags:
        return {"title": f"Safer {side} Sell", "level": f"{_fmt_level(safer)} {side}", "arrow": arrow, "color": "red", "state": "Avoid", "message": "Writers are covering even at safer strike."}
    if tag in control_tags and wall_move in ["stable", "first snapshot"]:
        return {"title": f"Safer {side} Sell", "level": f"{_fmt_level(safer)} {side}", "arrow": arrow, "color": "green", "state": "Tradable", "message": "Cleaner buffer away from active wall."}
    if tag in control_tags:
        return {"title": f"Safer {side} Sell", "level": f"{_fmt_level(safer)} {side}", "arrow": arrow, "color": "green", "state": "Tradable", "message": "Safer than wall; use EDGE alignment."}
    if tag in stress_tags:
        return {"title": f"Safer {side} Sell", "level": f"{_fmt_level(safer)} {side}", "arrow": arrow, "color": "orange", "state": "Caution", "message": "Premium pressure exists; reduce size or wait."}
    return {"title": f"Safer {side} Sell", "level": f"{_fmt_level(safer)} {side}", "arrow": arrow, "color": "orange", "state": "Caution", "message": "Not clean enough for aggressive selling."}


def _poc_card(spot: float, poc: float, pe_wall: float, ce_wall: float, poc_move: str, ce_defence: int, ce_stress: int, ce_failure: int, pe_support: int, pe_stress: int, pe_failure: int) -> Dict[str, Any]:
    arrow = _move_arrow(poc_move)
    if poc_move == "first snapshot" or not poc or not spot:
        return {"title": "POC", "level": _fmt_level(poc), "arrow": "", "color": "grey", "state": "Observation mode", "message": "No prior anchor for POC behaviour."}
    width = abs(ce_wall - pe_wall) if ce_wall and pe_wall else 0.0
    near = abs(spot - poc) <= max(width * 0.12, spot * 0.002, 1.0)
    if near:
        return {"title": "POC", "level": _fmt_level(poc), "arrow": arrow, "color": "grey", "state": "Theta control zone", "message": "Price near positioning balance."}
    if spot > poc:
        if ce_failure > 0 or ce_stress > ce_defence or poc_move == "higher":
            return {"title": "POC", "level": _fmt_level(poc), "arrow": arrow, "color": "green", "state": "Upside expansion risk", "message": "CE selling needs safer strike only."}
        return {"title": "POC", "level": _fmt_level(poc), "arrow": arrow, "color": "red", "state": "Magnet pull downward", "message": "Price stretched above balance."}
    if spot < poc:
        if pe_failure > 0 or pe_stress > pe_support or poc_move == "lower":
            return {"title": "POC", "level": _fmt_level(poc), "arrow": arrow, "color": "red", "state": "Downside expansion risk", "message": "PE selling needs safer strike only."}
        return {"title": "POC", "level": _fmt_level(poc), "arrow": arrow, "color": "green", "state": "Magnet pull upward", "message": "Price stretched below balance."}
    return {"title": "POC", "level": _fmt_level(poc), "arrow": arrow, "color": "grey", "state": "Theta control zone", "message": "Price near positioning balance."}


def _premium_alert(matrix: pd.DataFrame, spot_delta: float) -> str:
    if matrix is None or matrix.empty:
        return ""
    spot_dir = _direction(spot_delta, 0.0)
    if spot_dir == "flat":
        if _count_tags(matrix, ["ce_stress", "ce_abnormal", "ce_failure", "pe_stress", "pe_trap", "pe_failure"]) > 0:
            return "Premium alert: Premium expanding while spot is flat — theta risk elevated."
        return ""
    if spot_dir == "up" and _count_tags(matrix, ["pe_trap", "pe_failure", "pe_stress"], "PE") > 0:
        return "Premium alert: PE premium firm despite spot rising — downside hedge demand active."
    if spot_dir == "down" and _count_tags(matrix, ["ce_abnormal", "ce_failure", "ce_stress"], "CE") > 0:
        return "Premium alert: CE premium firm despite spot falling — upside premium still active."
    if spot_dir == "up" and _count_tags(matrix, ["ce_defended", "ce_defended_mild"], "CE") > 0:
        return "Premium alert: Spot moved up but CE premium did not expand — upside follow-through weak."
    if spot_dir == "down" and _count_tags(matrix, ["pe_defended", "pe_defended_mild"], "PE") > 0:
        return "Premium alert: Spot moved down but PE premium did not expand — downside follow-through weak."
    return ""


def _median_step(strikes: pd.DataFrame) -> float:
    try:
        vals = sorted(set(float(x) for x in strikes["strike"].dropna().tolist()))
        diffs = [b - a for a, b in zip(vals, vals[1:]) if b > a]
        return float(np.median(diffs)) if diffs else 0.0
    except Exception:
        return 0.0


def _path_speed(strikes: pd.DataFrame, spot: float, wall: float, side: str) -> Dict[str, Any]:
    """Classify the path from spot to active wall as Sharp or Grind.

    This is intentionally neutral: it is an option-buying/navigation aid, not a sell/avoid
    instruction. Sharp means there are fewer meaningful option-positioning clusters between
    spot and the wall. Grind means there are visible clusters before the wall.
    """
    side = side.upper()
    if strikes.empty or not spot or not wall:
        return {"side": "Upside" if side == "CE" else "Downside", "path": "No clear read", "wall": wall, "read": "Insufficient path data."}

    oi_col = "ce_oi" if side == "CE" else "pe_oi"
    vol_col = "ce_volume" if side == "CE" else "pe_volume"
    label = "Upside" if side == "CE" else "Downside"

    if side == "CE":
        path_rows = strikes[(strikes["strike"] > spot) & (strikes["strike"] <= wall)].copy()
        between_rows = strikes[(strikes["strike"] > spot) & (strikes["strike"] < wall)].copy()
    else:
        path_rows = strikes[(strikes["strike"] < spot) & (strikes["strike"] >= wall)].copy()
        between_rows = strikes[(strikes["strike"] < spot) & (strikes["strike"] > wall)].copy()

    wall_row = strikes[strikes["strike"].eq(wall)]
    wall_oi = _safe_float(wall_row.iloc[0].get(oi_col)) if not wall_row.empty else 0.0
    wall_vol = _safe_float(wall_row.iloc[0].get(vol_col)) if not wall_row.empty else 0.0
    max_oi = _safe_float(strikes[oi_col].max()) if oi_col in strikes.columns else 0.0
    max_vol = _safe_float(strikes[vol_col].max()) if vol_col in strikes.columns else 0.0

    step = _median_step(strikes)
    distance = abs(wall - spot)
    dist_txt = f"{distance:,.0f} pts" if distance else "0 pts"

    if path_rows.empty or distance <= max(step * 0.75, spot * 0.001):
        return {"side": label, "path": "Wall nearby", "wall": wall, "read": f"{label} wall {_fmt_level(wall)} is nearby ({dist_txt})."}

    oi_threshold = max(wall_oi * 0.25, max_oi * 0.12, 1.0)
    vol_threshold = max(wall_vol * 0.25, max_vol * 0.12, 1.0)
    clusters = between_rows[(between_rows[oi_col] >= oi_threshold) | (between_rows[vol_col] >= vol_threshold)].copy() if not between_rows.empty else pd.DataFrame()
    intermediate_oi = float(between_rows[oi_col].sum()) if not between_rows.empty and oi_col in between_rows.columns else 0.0
    cluster_count = len(clusters)

    # Grind = meaningful option-positioning clusters are present before the wall.
    grind = cluster_count >= 2 or (cluster_count >= 1 and intermediate_oi >= max(wall_oi * 0.55, max_oi * 0.18, 1.0))

    if grind:
        nearest = float(clusters.sort_values("strike", ascending=(side == "CE")).iloc[0]["strike"]) if not clusters.empty else 0.0
        read = f"{label} path to {_fmt_level(wall)} {side} wall: Grind; positioning clusters visible before wall"
        if nearest:
            read += f" near {_fmt_level(nearest)}"
        read += "."
        return {"side": label, "path": "Grind", "wall": wall, "read": read}

    return {"side": label, "path": "Sharp", "wall": wall, "read": f"{label} path to {_fmt_level(wall)} {side} wall: Sharp; limited positioning clusters before wall."}


def _build_path_risk(strikes: pd.DataFrame, spot: float, ce_wall: float, pe_wall: float) -> Dict[str, Any]:
    up = _path_speed(strikes, spot, ce_wall, "CE")
    down = _path_speed(strikes, spot, pe_wall, "PE")
    return {"upside": up, "downside": down}


def _compact_hero(final_action: str, ce_card: Dict[str, Any], pe_card: Dict[str, Any], safer_ce_card: Dict[str, Any], safer_pe_card: Dict[str, Any], poc_card: Dict[str, Any], first: bool, itm_caution: str = "") -> str:
    if first:
        return "Observation mode only — prior anchor not available. Levels are visible, but ODME should not be used for strong action yet."

    ce_ok = safer_ce_card.get("state") == "Tradable"
    pe_ok = safer_pe_card.get("state") == "Tradable"
    poc_state = str(poc_card.get("state", "")).lower()
    ce_wall_state = str(ce_card.get("state", "")).lower()
    pe_wall_state = str(pe_card.get("state", "")).lower()

    # POC pressure is an ODME range-pressure read, not an EDGE trade direction.
    # Do not allow the hero to suggest a clean short side when the opposite wall is also stressed.
    upward_pressure = "upside expansion" in poc_state or "magnet pull upward" in poc_state
    downward_pressure = "downside expansion" in poc_state or "magnet pull downward" in poc_state
    ce_wall_stressed = any(x in ce_wall_state for x in ["pressure", "failure", "covering", "shifted"])
    pe_wall_stressed = any(x in pe_wall_state for x in ["pressure", "failure", "covering", "shifted"])

    conflict = (downward_pressure and ce_wall_stressed) or (upward_pressure and pe_wall_stressed)
    if conflict:
        action = "Conflicting premium pressure — no clean fresh short option. Wait for EDGE confirmation; avoid aggressive CE/PE selling."
        if downward_pressure:
            reason = f"Reason: POC shows {str(poc_card.get('state','')).lower()}, but CE wall is {str(ce_card.get('state','')).lower()}; this is two-way premium risk, not a clean short-CE setup."
        else:
            reason = f"Reason: POC shows {str(poc_card.get('state','')).lower()}, but PE wall is {str(pe_card.get('state','')).lower()}; this is two-way premium risk, not a clean short-PE setup."
    elif upward_pressure:
        if pe_ok:
            action = f"Prefer short PE at {safer_pe_card.get('level', '')}. Avoid fresh CE selling while POC pressure is upward."
        else:
            action = "Avoid fresh CE selling. PE side is not clean enough yet, so wait or use wider PE only with EDGE support."
        reason = f"Reason: POC shows {str(poc_card.get('state','')).lower()}, CE wall is {str(ce_card.get('state','')).lower()}, and safer PE is {str(safer_pe_card.get('state','')).lower()}."
    elif downward_pressure:
        if ce_ok:
            action = f"Prefer short CE at {safer_ce_card.get('level', '')}. Avoid fresh PE selling while POC pressure is downward."
        else:
            action = "Avoid fresh PE selling. CE side is not clean enough yet, so wait or use wider CE only with EDGE support."
        reason = f"Reason: POC shows {str(poc_card.get('state','')).lower()}, PE wall is {str(pe_card.get('state','')).lower()}, and safer CE is {str(safer_ce_card.get('state','')).lower()}."
    else:
        if ce_ok and not pe_ok:
            action = f"Prefer short CE at {safer_ce_card.get('level', '')}. Avoid fresh PE selling for now."
        elif pe_ok and not ce_ok:
            action = f"Prefer short PE at {safer_pe_card.get('level', '')}. Avoid fresh CE selling for now."
        elif ce_ok and pe_ok:
            action = "Both safer sides are tradable only as theta structures; use EDGE to choose side."
        else:
            action = "No clean fresh short option. Wait or use wider strikes only if EDGE strongly supports it."
        reason = f"Reason: POC shows {str(poc_card.get('state','')).lower()}, CE wall is {str(ce_card.get('state','')).lower()}, and PE wall is {str(pe_card.get('state','')).lower()}."

    hero = f"{action}\n{reason}"
    if itm_caution:
        hero += f"\n{itm_caution}"
    return hero



def _summary_value(summary: Dict[str, Any], *names: str, default: Any = 0.0) -> Any:
    for name in names:
        if isinstance(summary, dict) and summary.get(name) not in [None, ""]:
            return summary.get(name)
    return default


def reconstruct_saved_result(current_summary: Dict[str, Any], previous_summary: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Rebuild the saved-screen cards from compact Google Sheet snapshot rows.

    Full option-chain rows are intentionally not stored. This function uses the
    compact saved key_strikes_json plus the comparison snapshot to recreate the
    same card colours, arrows, premium alert and path assist that were visible
    after the live fetch. It is for display only; it does not fetch live data.
    """
    previous_summary = previous_summary or {}
    cur = current_summary or {}

    spot = _safe_float(_summary_value(cur, "spot"))
    prev_spot = _safe_float(_summary_value(previous_summary, "spot"))
    poc = _safe_float(_summary_value(cur, "poc", "option_poc"))
    val = _safe_float(_summary_value(cur, "value_area_low"))
    vah = _safe_float(_summary_value(cur, "value_area_high"))
    ce_wall = _safe_float(_summary_value(cur, "ce_wall", "active_ce_wall"))
    pe_wall = _safe_float(_summary_value(cur, "pe_wall", "active_pe_wall"))
    safer_ce = _safe_float(_summary_value(cur, "safer_sell_ce"))
    safer_pe = _safe_float(_summary_value(cur, "safer_sell_pe"))

    prev_poc = _safe_float(_summary_value(previous_summary, "poc", "option_poc"))
    prev_ce_wall = _safe_float(_summary_value(previous_summary, "ce_wall", "active_ce_wall"))
    prev_pe_wall = _safe_float(_summary_value(previous_summary, "pe_wall", "active_pe_wall"))
    prev_safer_ce = _safe_float(_summary_value(previous_summary, "safer_sell_ce"))
    prev_safer_pe = _safe_float(_summary_value(previous_summary, "safer_sell_pe"))

    poc_move = str(_summary_value(cur, "poc_move", "poc_shift", default="")) or _movement(poc, prev_poc)
    ce_move = str(_summary_value(cur, "ce_wall_move", "ce_wall_shift", default="")) or _movement(ce_wall, prev_ce_wall)
    pe_move = str(_summary_value(cur, "pe_wall_move", "pe_wall_shift", default="")) or _movement(pe_wall, prev_pe_wall)

    key_strikes = _extract_prev_key_metrics(cur)
    prev_keys = _extract_prev_key_metrics(previous_summary)
    matrix = _build_matrix(key_strikes, prev_keys, spot, prev_spot)
    action_matrix = _actionable_matrix(matrix, spot)

    ce_defence = _count_tags(action_matrix, ["ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild"], "CE")
    ce_stress = _count_tags(action_matrix, ["ce_stress", "ce_failure", "ce_abnormal"], "CE")
    ce_failure = _count_tags(action_matrix, ["ce_failure"], "CE")
    pe_support = _count_tags(action_matrix, ["pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"], "PE")
    pe_stress = _count_tags(action_matrix, ["pe_trap", "pe_failure", "pe_stress"], "PE")
    pe_failure = _count_tags(action_matrix, ["pe_failure"], "PE")

    poc_card = _poc_card(spot, poc, pe_wall, ce_wall, poc_move, ce_defence, ce_stress, ce_failure, pe_support, pe_stress, pe_failure)
    ce_wall_card = _wall_card("CE", ce_wall, prev_ce_wall, ce_move, matrix)
    pe_wall_card = _wall_card("PE", pe_wall, prev_pe_wall, pe_move, matrix)
    safer_ce_card = _safer_card("CE", safer_ce, prev_safer_ce, ce_move, matrix)
    safer_pe_card = _safer_card("PE", safer_pe, prev_safer_pe, pe_move, matrix)

    spot_delta = spot - prev_spot if prev_spot else 0.0
    premium_alert = _premium_alert(action_matrix, spot_delta)
    itm_caution = _significant_itm_caution(matrix, spot)

    # Rebuild a compact strike table from saved key-strikes for path assist.
    strike_rows = []
    for v in (key_strikes or {}).values():
        if not isinstance(v, dict):
            continue
        strike = _safe_float(v.get("strike"))
        if not strike:
            continue
        ce_oi = _safe_float(v.get("ce_oi")); pe_oi = _safe_float(v.get("pe_oi"))
        row = dict(v)
        row["strike"] = strike
        row["ce_oi"] = ce_oi; row["pe_oi"] = pe_oi
        row["ce_volume"] = _safe_float(v.get("ce_volume")); row["pe_volume"] = _safe_float(v.get("pe_volume"))
        row["combined_oi"] = ce_oi + pe_oi
        strike_rows.append(row)
    strikes = pd.DataFrame(strike_rows)
    if not strikes.empty:
        strikes = strikes.sort_values("strike")
    path_risk = _build_path_risk(strikes, spot, ce_wall, pe_wall) if not strikes.empty else {}

    first = not bool(previous_summary) or not prev_spot
    hero_action = _compact_hero("", ce_wall_card, pe_wall_card, safer_ce_card, safer_pe_card, poc_card, first, itm_caution)

    scores = {
        "Bullish": int(_safe_float(_summary_value(cur, "bullish_score", default=0))),
        "Bearish": int(_safe_float(_summary_value(cur, "bearish_score", default=0))),
        "Range": int(_safe_float(_summary_value(cur, "range_score", default=0))),
        "Expansion": int(_safe_float(_summary_value(cur, "expansion_score", default=0))),
    }

    return {
        "tilt": str(_summary_value(cur, "tilt", "odme_tilt", default="")),
        "spot": spot,
        "poc": poc,
        "value_area_low": val,
        "value_area_high": vah,
        "ce_wall": ce_wall,
        "pe_wall": pe_wall,
        "safer_sell_ce": safer_ce,
        "safer_sell_pe": safer_pe,
        "scores": scores,
        "poc_move": poc_move,
        "ce_wall_move": ce_move,
        "pe_wall_move": pe_move,
        "commentary": str(_summary_value(cur, "commentary", default="")),
        "hero_action": hero_action,
        "final_action": hero_action,
        "premium_alert": premium_alert,
        "path_risk": path_risk,
        "cards": {
            "poc": poc_card,
            "ce_wall": ce_wall_card,
            "pe_wall": pe_wall_card,
            "safer_ce": safer_ce_card,
            "safer_pe": safer_pe_card,
        },
    }


def analyze_odme(chain_df: pd.DataFrame, instrument: str, manual_spot: float | None = None, previous_summary: Dict[str, Any] | None = None) -> Dict[str, Any]:
    previous_summary = previous_summary or {}
    strikes, meta = build_strike_table(chain_df, instrument, manual_spot)
    if strikes.empty:
        return {"tilt": "MIXED / NO CLEAN EDGE", "error": "No live option-chain rows available."}

    spot = float(meta["spot"])
    poc = float(strikes.sort_values("combined_oi", ascending=False).iloc[0]["strike"]) if strikes["combined_oi"].max() > 0 else 0.0
    val, vah = value_area(strikes)

    ce_candidates = strikes[strikes["strike"] >= spot].sort_values("ce_wall_score", ascending=False).head(3)
    pe_candidates = strikes[strikes["strike"] <= spot].sort_values("pe_wall_score", ascending=False).head(3)
    ce_wall = float(ce_candidates.iloc[0]["strike"]) if not ce_candidates.empty else 0.0
    pe_wall = float(pe_candidates.iloc[0]["strike"]) if not pe_candidates.empty else 0.0
    safer_ce = float(ce_candidates.sort_values("strike", ascending=False).iloc[0]["strike"]) if not ce_candidates.empty else 0.0
    safer_pe = float(pe_candidates.sort_values("strike", ascending=True).iloc[0]["strike"]) if not pe_candidates.empty else 0.0

    prev_spot = _safe_float(previous_summary.get("spot"))
    prev_poc = _safe_float(previous_summary.get("poc") or previous_summary.get("option_poc"))
    prev_ce_wall = _safe_float(previous_summary.get("ce_wall"))
    prev_pe_wall = _safe_float(previous_summary.get("pe_wall"))
    prev_safer_ce = _safe_float(previous_summary.get("safer_sell_ce"))
    prev_safer_pe = _safe_float(previous_summary.get("safer_sell_pe"))
    spot_delta = spot - prev_spot if prev_spot else 0.0
    spot_pct = _pct_change(spot, prev_spot) if prev_spot else 0.0
    poc_move = _movement(poc, prev_poc)
    ce_move = _movement(ce_wall, prev_ce_wall)
    pe_move = _movement(pe_wall, prev_pe_wall)
    range_move = _range_shift(ce_wall - pe_wall if ce_wall and pe_wall else 0, prev_ce_wall - prev_pe_wall if prev_ce_wall and prev_pe_wall else 0)

    key_strikes = _make_key_strikes(strikes, spot, poc, val, vah, ce_candidates, pe_candidates)
    # Include prior important levels so wall migrations/failures are visible in anchored matrix.
    for _lvl in [prev_poc, prev_ce_wall, prev_pe_wall, _safe_float(previous_summary.get("safer_sell_ce")), _safe_float(previous_summary.get("safer_sell_pe"))]:
        _add_level_to_keys(key_strikes, strikes, _lvl)
    prev_keys = _extract_prev_key_metrics(previous_summary)
    matrix = _build_matrix(key_strikes, prev_keys, spot, prev_spot)
    action_matrix = _actionable_matrix(matrix, spot)
    itm_caution = _significant_itm_caution(matrix, spot)

    # Spot-adjusted event counts. ITM rows are excluded from primary action logic.
    ce_defence = _count_tags(action_matrix, ["ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild"], "CE")
    ce_stress = _count_tags(action_matrix, ["ce_stress", "ce_failure", "ce_abnormal"], "CE")
    ce_failure = _count_tags(action_matrix, ["ce_failure"], "CE")
    pe_support = _count_tags(action_matrix, ["pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"], "PE")
    pe_stress = _count_tags(action_matrix, ["pe_trap", "pe_failure", "pe_stress"], "PE")
    pe_failure = _count_tags(action_matrix, ["pe_failure"], "PE")

    above_poc = spot > poc if poc else False
    below_poc = spot < poc if poc else False
    stretched = abs(spot - poc) / spot > 0.025 if spot and poc else False

    bullish = _sum_col(action_matrix, "bullish_pts")
    bearish = _sum_col(action_matrix, "bearish_pts")
    range_score = _sum_col(action_matrix, "range_pts")
    expansion = _sum_col(action_matrix, "expansion_pts")

    # Structure/migration overlay. These are intentionally secondary to premium/OI behaviour.
    bullish += 12 if pe_move == "higher" else 0
    bullish += 8 if ce_move == "higher" else 0
    bullish += 8 if above_poc and poc_move in ["higher", "stable", "first snapshot"] else 0
    bullish += 8 if pe_support > pe_stress and pe_support > 0 else 0

    bearish += 12 if ce_move == "lower" else 0
    bearish += 8 if pe_move == "lower" else 0
    bearish += 8 if below_poc and poc_move in ["lower", "stable", "first snapshot"] else 0
    bearish += 8 if ce_defence > ce_stress and ce_defence > 0 else 0

    range_score += 15 if range_move in ["stable", "narrowing", "first snapshot"] else 0
    range_score += 12 if ce_defence > 0 and pe_support > 0 else 0
    range_score += 10 if not stretched and poc_move in ["stable", "first snapshot"] else 0
    range_score += 8 if pe_wall < spot < ce_wall else 0

    expansion += 18 if range_move == "widening" else 0
    expansion += 18 if ce_stress > 0 and pe_stress > 0 else 0
    expansion += 12 if stretched and poc_move not in ["stable", "first snapshot"] else 0
    expansion += 15 if pe_stress > pe_support and _direction(spot_delta) == "up" else 0  # key correction: PE premium firm during rise = trap risk
    expansion += 15 if ce_stress > ce_defence and _direction(spot_delta) == "down" else 0

    scores = {
        "Bullish": int(max(0, min(100, bullish))),
        "Bearish": int(max(0, min(100, bearish))),
        "Range": int(max(0, min(100, range_score))),
        "Expansion": int(max(0, min(100, expansion))),
    }
    tilt = _decide_tilt(scores)

    hvn = strikes.sort_values("combined_oi", ascending=False).head(5)[["strike", "combined_oi"]].to_dict("records")
    lvn = strikes[strikes["combined_oi"] > 0].sort_values("combined_oi", ascending=True).head(5)[["strike", "combined_oi"]].to_dict("records")

    ce_action = _side_action("CE", ce_wall, safer_ce, ce_move, ce_stress, ce_defence, ce_failure)
    pe_action = _side_action("PE", pe_wall, safer_pe, pe_move, pe_stress, 0, pe_failure, pe_support)
    final_action = _final_action(tilt, ce_action, pe_action, scores, spot, poc, poc_move)

    poc_card = _poc_card(spot, poc, pe_wall, ce_wall, poc_move, ce_defence, ce_stress, ce_failure, pe_support, pe_stress, pe_failure)
    ce_wall_card = _wall_card("CE", ce_wall, prev_ce_wall, ce_move, matrix)
    pe_wall_card = _wall_card("PE", pe_wall, prev_pe_wall, pe_move, matrix)
    safer_ce_card = _safer_card("CE", safer_ce, prev_safer_ce, ce_move, matrix)
    safer_pe_card = _safer_card("PE", safer_pe, prev_safer_pe, pe_move, matrix)
    premium_alert = _premium_alert(action_matrix, spot_delta)
    path_risk = _build_path_risk(strikes, spot, ce_wall, pe_wall)
    hero_action = _compact_hero(final_action, ce_wall_card, pe_wall_card, safer_ce_card, safer_pe_card, poc_card, poc_move == "first snapshot" or not prev_spot, itm_caution)

    commentary = build_commentary(
        tilt=tilt,
        spot=spot,
        prev_spot=prev_spot,
        spot_delta=spot_delta,
        spot_pct=spot_pct,
        poc=poc,
        val=val,
        vah=vah,
        ce_wall=ce_wall,
        pe_wall=pe_wall,
        safer_ce=safer_ce,
        safer_pe=safer_pe,
        poc_move=poc_move,
        ce_move=ce_move,
        pe_move=pe_move,
        range_move=range_move,
        ce_defence=ce_defence,
        ce_stress=ce_stress,
        pe_support=pe_support,
        pe_stress=pe_stress,
        stretched=stretched,
        previous_summary=previous_summary,
        matrix=action_matrix,
        ce_action=ce_action,
        pe_action=pe_action,
        final_action=final_action,
        scores=scores,
    )

    return {
        "tilt": tilt,
        "spot": spot,
        "poc": poc,
        "value_area_low": val,
        "value_area_high": vah,
        "ce_wall": ce_wall,
        "pe_wall": pe_wall,
        "safer_sell_ce": safer_ce,
        "safer_sell_pe": safer_pe,
        "scores": scores,
        "poc_move": poc_move,
        "ce_wall_move": ce_move,
        "pe_wall_move": pe_move,
        "range_move": range_move,
        "hvn": hvn,
        "lvn": lvn,
        "matrix": matrix,
        "strike_table": strikes,
        "key_strikes": key_strikes,
        "commentary": commentary,
        "final_action": final_action,
        "hero_action": hero_action,
        "premium_alert": premium_alert,
        "itm_caution": itm_caution,
        "path_risk": path_risk,
        "cards": {
            "poc": poc_card,
            "ce_wall": ce_wall_card,
            "pe_wall": pe_wall_card,
            "safer_ce": safer_ce_card,
            "safer_pe": safer_pe_card,
        },
        "ce_action": ce_action,
        "pe_action": pe_action,
        "meta": meta,
    }


def build_commentary(
    *,
    tilt: str,
    spot: float,
    prev_spot: float,
    spot_delta: float,
    spot_pct: float,
    poc: float,
    val: float,
    vah: float,
    ce_wall: float,
    pe_wall: float,
    safer_ce: float,
    safer_pe: float,
    poc_move: str,
    ce_move: str,
    pe_move: str,
    range_move: str,
    ce_defence: int,
    ce_stress: int,
    pe_support: int,
    pe_stress: int,
    stretched: bool,
    previous_summary: Dict[str, Any],
    matrix: pd.DataFrame,
    ce_action: str,
    pe_action: str,
    final_action: str,
    scores: Dict[str, int],
) -> str:
    prev_tilt = previous_summary.get("tilt") or previous_summary.get("odme_tilt") or "No previous snapshot"
    first = poc_move == "first snapshot" or not prev_spot

    if first:
        change_line = "First usable ODME snapshot for this instrument/expiry. The next fetch will give a stronger previous-vs-current read."
    else:
        change_line = f"Spot changed {_fmt_points(spot_delta)} points ({_fmt_pct(spot_pct)}) since the last saved snapshot. ODME tilt changed from {prev_tilt} to {tilt}."

    # POC read, with magnet logic made conditional.
    if poc_move == "stable" and not stretched:
        poc_line = f"POC is stable at {_fmt_level(poc)} and spot is not stretched from it; mean-reversion/theta logic is valid."
    elif poc_move == "stable" and stretched:
        poc_line = f"POC is stable at {_fmt_level(poc)}, but spot is stretched from it; do not sell close strikes blindly."
    elif poc_move == "first snapshot":
        poc_line = f"Tradable option POC is {_fmt_level(poc)}; treat it as baseline only until one more fetch confirms stability."
    else:
        poc_line = f"POC has shifted {poc_move} to {_fmt_level(poc)}; do not fade against this migration aggressively."

    ce_key = _nearest_action_rows(matrix, ["ce_stress", "ce_failure", "ce_abnormal"], "CE", 2)
    ce_ctrl = _nearest_action_rows(matrix, ["ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild"], "CE", 2)
    pe_key = _nearest_action_rows(matrix, ["pe_trap", "pe_failure", "pe_stress"], "PE", 2)
    pe_ctrl = _nearest_action_rows(matrix, ["pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"], "PE", 2)

    if ce_stress > ce_defence:
        ce_line = f"CE side is not clean for aggressive selling. Stress/failure is visible at {', '.join(ce_key) if ce_key else _fmt_level(ce_wall)}. {ce_action}"
    elif ce_defence > 0:
        ce_line = f"CE side shows writer absorption at {', '.join(ce_ctrl) if ce_ctrl else _fmt_level(ce_wall)}. {ce_action}"
    else:
        ce_line = f"CE side has no strong confirmation. {ce_action}"

    if pe_stress > pe_support:
        pe_line = f"PE side is not clean support. Premium/OI behaviour warns at {', '.join(pe_key) if pe_key else _fmt_level(pe_wall)}. {pe_action}"
    elif pe_support > 0:
        pe_line = f"PE side shows usable support at {', '.join(pe_ctrl) if pe_ctrl else _fmt_level(pe_wall)}. {pe_action}"
    else:
        pe_line = f"PE side has no strong confirmation. {pe_action}"

    # Critical correction explicitly inside commentary, but actionable not theoretical.
    spot_dir = _direction(spot_delta, 0.0)
    correction_line = ""
    if spot_dir == "up" and pe_stress > 0:
        correction_line = "Important: spot is higher but PE premium is not decaying cleanly at key strikes, so this is not clean bullish support; treat it as trap/hedge demand until PE premium softens."
    elif spot_dir == "up" and pe_support > pe_stress and pe_support > 0:
        correction_line = "PE support is valid because premium is soft/decaying while OI is being added; this is the cleaner bullish condition."
    elif spot_dir == "down" and ce_stress > 0:
        correction_line = "Important: spot is lower but CE premium is not decaying cleanly at key strikes, so this is not clean bearish control; upside hedge/covering risk remains."
    elif spot_dir == "down" and ce_defence > ce_stress and ce_defence > 0:
        correction_line = "CE pressure is valid because premium is soft/decaying while OI is being added; this is the cleaner bearish condition."

    risk_line = ""
    if tilt == "EXPANSION / TRAP RISK":
        risk_line = "Risk is elevated: avoid tight short strikes and avoid averaging into the active wall."
    elif tilt == "MIXED / NO CLEAN EDGE":
        risk_line = "Conviction is insufficient: use the app as a warning monitor, not as a fresh-entry trigger."
    elif tilt == "RANGE-BOUND THETA":
        risk_line = "Theta condition is acceptable only if strikes are kept outside the active walls and POC does not migrate with price."
    elif tilt == "BULLISH POSITIONING":
        risk_line = "Bias favours PE selling or holding bullish theta, but only while PE premium remains soft and PE wall does not shift lower."
    elif tilt == "BEARISH POSITIONING":
        risk_line = "Bias favours CE selling or holding bearish theta, but only while CE premium remains soft and CE wall does not shift higher."

    return (
        f"ODME Verdict: {tilt}. Scores — Bullish {scores.get('Bullish', 0)}, Bearish {scores.get('Bearish', 0)}, Range {scores.get('Range', 0)}, Expansion {scores.get('Expansion', 0)}.\n\n"
        f"What changed: {change_line}\n\n"
        f"Positioning: spot/future proxy is {_fmt_level(spot)}. Option POC {_fmt_level(poc)}; value area {_fmt_level(val)}–{_fmt_level(vah)}. {poc_line}\n\n"
        f"Walls: active CE wall {_fmt_level(ce_wall)} ({ce_move}); active PE wall {_fmt_level(pe_wall)} ({pe_move}); range is {range_move}.\n\n"
        f"CE Action: {ce_line}\n\n"
        f"PE Action: {pe_line}\n\n"
        f"{correction_line}\n\n" if correction_line else ""
    ) + (
        f"Final Action: {final_action}\n\n"
        f"Risk Note: {risk_line}"
    )
