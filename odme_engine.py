from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from odme_config import DEFAULT_RELEVANT_RANGE_PCT, RELEVANT_RANGE_PCT


# =============================================================================
# ODME Engine v5
# Focus: compact live ODME summary, previous-vs-current comparison, actionable
# commentary. Includes full ITM CE/PE positioning overlay matrix without
# polluting actionable CE/PE wall guidance.
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
    near = strikes.assign(dist=(strikes["strike"] - spot).abs()).sort_values("dist").head(5)["strike"].astype(float).tolist()
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
    """Only ATM/OTM side rows are allowed to drive actionable CE/PE wall guidance.

    CE action: strikes at/above spot.
    PE action: strikes at/below spot.
    ITM rows remain useful as background positioning, but they must not be
    described as overhead resistance/support walls.
    """
    if matrix.empty or not spot:
        return matrix.copy()
    x = matrix.copy()
    return x[((x["side"].eq("CE")) & (x["strike"] >= spot)) | ((x["side"].eq("PE")) & (x["strike"] <= spot))].copy()


def _itm_matrix(matrix: pd.DataFrame, spot: float) -> pd.DataFrame:
    if matrix.empty or not spot:
        return pd.DataFrame()
    x = matrix.copy()
    return x[((x["side"].eq("CE")) & (x["strike"] < spot)) | ((x["side"].eq("PE")) & (x["strike"] > spot))].copy()


def _material_rows(x: pd.DataFrame) -> pd.DataFrame:
    if x.empty:
        return x
    y = x.copy()
    y["abs_oi"] = pd.to_numeric(y["delta_oi_vs_previous"], errors="coerce").abs().fillna(0)
    y["abs_prem"] = pd.to_numeric(y["delta_premium_vs_previous"], errors="coerce").abs().fillna(0)
    # Keep any real movement; first snapshots will naturally have no useful previous comparison.
    return y[(y["abs_oi"] > 0) | (y["abs_prem"] > 0)].copy()


def _itm_positioning_overlay(matrix: pd.DataFrame, spot: float) -> Tuple[str, Dict[str, int]]:
    """Convert all material ITM CE/PE OI + premium combinations into a net overlay.

    Important design rule:
    - ITM CE below spot and ITM PE above spot are NOT actionable short-option walls.
    - They are positioning/adjustment evidence that supports or challenges the main read.
    - Actionable CE/PE guidance remains restricted to ATM/OTM side strikes.

    Full matrix handled:
    ITM CE below spot:
      OI up + premium down   = bearish-neutral call writing/adjustment, weak bullish follow-through
      OI up + premium up     = bullish pressure / aggressive ITM call demand, but confirm with POC migration
      OI down + premium up   = bullish covering / short-call pressure, upside risk
      OI down + premium down = long-call unwinding / bullish interest fading, bearish-neutral

    ITM PE above spot:
      OI up + premium down   = bearish exposure losing value / PE writing adjustment, range repair
      OI up + premium up     = bearish stress / hedge buildup, downside risk
      OI down + premium up   = put-writer covering or old bearish exposure reducing while premium stays firm, bearish-risk
      OI down + premium down = bearish exposure unwinding and premium cooling, range repair / bullish-neutral
    """
    itm = _material_rows(_itm_matrix(matrix, spot))
    if itm.empty:
        return "", {"Bullish": 0, "Bearish": 0, "Range": 0, "Expansion": 0}

    ce = itm[itm["side"].eq("CE")].copy()
    pe = itm[itm["side"].eq("PE")].copy()

    def case_mask(df: pd.DataFrame, oi: str, prem: str) -> pd.Series:
        if df.empty:
            return pd.Series(False, index=df.index)
        doi = pd.to_numeric(df["delta_oi_vs_previous"], errors="coerce").fillna(0)
        dpr = pd.to_numeric(df["delta_premium_vs_previous"], errors="coerce").fillna(0)
        oi_ok = doi > 0 if oi == "up" else doi < 0 if oi == "down" else doi == 0
        pr_ok = dpr > 0 if prem == "up" else dpr < 0 if prem == "down" else dpr == 0
        return oi_ok & pr_ok

    def case_strength(df: pd.DataFrame, oi: str, prem: str) -> float:
        if df.empty:
            return 0.0
        m = case_mask(df, oi, prem)
        if not bool(m.any()):
            return 0.0
        x = df.loc[m].copy()
        # Scale by both OI and premium movement so one tiny row does not dominate.
        oi_strength = pd.to_numeric(x["delta_oi_vs_previous"], errors="coerce").abs().fillna(0).sum()
        prem_strength = pd.to_numeric(x["delta_premium_vs_previous"], errors="coerce").abs().fillna(0).sum()
        pct_strength = pd.to_numeric(x["premium_change_pct"], errors="coerce").abs().fillna(0).sum()
        return float(oi_strength * 0.01 + prem_strength + pct_strength * 0.15 + len(x) * 2.0)

    def top_levels(df: pd.DataFrame, oi: str, prem: str, side: str, limit: int = 2) -> str:
        if df.empty:
            return ""
        m = case_mask(df, oi, prem)
        if not bool(m.any()):
            return ""
        x = df.loc[m].copy()
        x["strength"] = (
            pd.to_numeric(x["delta_oi_vs_previous"], errors="coerce").abs().fillna(0) * 0.01
            + pd.to_numeric(x["delta_premium_vs_previous"], errors="coerce").abs().fillna(0)
            + pd.to_numeric(x["premium_change_pct"], errors="coerce").abs().fillna(0) * 0.15
        )
        x = x.sort_values("strength", ascending=False).head(limit)
        return ", ".join([f"{_fmt_level(r['strike'])} {side}" for _, r in x.iterrows()])

    cases = {
        # CE ITM below spot
        "ce_oi_up_prem_down": case_strength(ce, "up", "down"),
        "ce_oi_up_prem_up": case_strength(ce, "up", "up"),
        "ce_oi_down_prem_up": case_strength(ce, "down", "up"),
        "ce_oi_down_prem_down": case_strength(ce, "down", "down"),
        # PE ITM above spot
        "pe_oi_up_prem_down": case_strength(pe, "up", "down"),
        "pe_oi_up_prem_up": case_strength(pe, "up", "up"),
        "pe_oi_down_prem_up": case_strength(pe, "down", "up"),
        "pe_oi_down_prem_down": case_strength(pe, "down", "down"),
    }

    scores = {"Bullish": 0, "Bearish": 0, "Range": 0, "Expansion": 0}
    detail_clauses: List[Tuple[float, str]] = []

    def add(key: str, bull: int, bear: int, rng: int, exp: int, clause: str) -> None:
        strength = cases.get(key, 0.0)
        if strength <= 0:
            return
        # Cap contribution per case; strength adds dominance but prevents runaway scoring.
        mult = 1.0 + min(strength, 30.0) / 60.0
        scores["Bullish"] += int(round(bull * mult))
        scores["Bearish"] += int(round(bear * mult))
        scores["Range"] += int(round(rng * mult))
        scores["Expansion"] += int(round(exp * mult))
        detail_clauses.append((strength, clause))

    ce_ud_levels = top_levels(ce, "up", "down", "CE")
    ce_uu_levels = top_levels(ce, "up", "up", "CE")
    ce_du_levels = top_levels(ce, "down", "up", "CE")
    ce_dd_levels = top_levels(ce, "down", "down", "CE")
    pe_ud_levels = top_levels(pe, "up", "down", "PE")
    pe_uu_levels = top_levels(pe, "up", "up", "PE")
    pe_du_levels = top_levels(pe, "down", "up", "PE")
    pe_dd_levels = top_levels(pe, "down", "down", "PE")

    add(
        "ce_oi_up_prem_down", 0, 8, 5, 0,
        f"ITM CE buildup below spot with premium decay{f' at {ce_ud_levels}' if ce_ud_levels else ''} shows call-side writing/adjustment and weak bullish follow-through."
    )
    add(
        "ce_oi_up_prem_up", 7, 0, 0, 5,
        f"ITM CE OI buildup with premium expansion{f' at {ce_uu_levels}' if ce_uu_levels else ''} shows bullish pressure/ITM call demand, but it needs POC migration higher to become clean breakout validation."
    )
    add(
        "ce_oi_down_prem_up", 8, 0, 0, 6,
        f"ITM CE OI reduction with premium expansion{f' at {ce_du_levels}' if ce_du_levels else ''} suggests short-call covering/upside pressure; upside risk is active."
    )
    add(
        "ce_oi_down_prem_down", 0, 5, 4, 0,
        f"ITM CE OI reduction with premium decay{f' at {ce_dd_levels}' if ce_dd_levels else ''} shows long-call unwinding or bullish interest fading."
    )

    add(
        "pe_oi_up_prem_down", 5, 0, 6, 0,
        f"ITM PE OI buildup with premium decay{f' at {pe_ud_levels}' if pe_ud_levels else ''} shows bearish exposure losing value / PE writing adjustment; this supports range repair if spot reclaims POC."
    )
    add(
        "pe_oi_up_prem_up", 0, 9, 0, 7,
        f"ITM PE OI buildup with premium expansion{f' at {pe_uu_levels}' if pe_uu_levels else ''} shows active downside stress or hedging above spot."
    )
    add(
        "pe_oi_down_prem_up", 0, 9, 0, 6,
        f"ITM PE OI reduction with premium expansion{f' at {pe_du_levels}' if pe_du_levels else ''} means downside premium remains firm while existing put exposure is being reduced/covered; this is bearish-risk, not support."
    )
    add(
        "pe_oi_down_prem_down", 5, 0, 6, 0,
        f"ITM PE OI reduction with premium decay{f' at {pe_dd_levels}' if pe_dd_levels else ''} shows bearish exposure unwinding and premium cooling; this supports range repair / bullish-neutral tone."
    )

    # Combined dominance lines. These are the important high-confidence summaries.
    bearish_combo = cases["ce_oi_up_prem_down"] + cases["ce_oi_down_prem_down"] + cases["pe_oi_up_prem_up"] + cases["pe_oi_down_prem_up"]
    bullish_combo = cases["ce_oi_up_prem_up"] + cases["ce_oi_down_prem_up"] + cases["pe_oi_up_prem_down"] + cases["pe_oi_down_prem_down"]
    two_sided_premium_stress = (cases["ce_oi_up_prem_up"] + cases["ce_oi_down_prem_up"] > 0) and (cases["pe_oi_up_prem_up"] + cases["pe_oi_down_prem_up"] > 0)
    two_sided_premium_decay = (cases["ce_oi_up_prem_down"] + cases["ce_oi_down_prem_down"] > 0) and (cases["pe_oi_up_prem_down"] + cases["pe_oi_down_prem_down"] > 0)

    if two_sided_premium_stress:
        scores["Expansion"] += 10
        detail_clauses.insert(0, (999.0, "Both ITM CE and ITM PE premium are firm/expanding in material pockets; this is two-sided premium stress, so avoid aggressive theta until one side cools."))
    elif two_sided_premium_decay:
        scores["Range"] += 10
        detail_clauses.insert(0, (998.0, "Both ITM CE and ITM PE premium are cooling in material pockets; this supports range/theta unless POC starts migrating with price."))

    if bearish_combo > bullish_combo * 1.25 and bearish_combo > 0:
        scores["Bearish"] += 8
        detail_clauses.insert(0, (1000.0, "Net ITM overlay leans bearish-to-range: upside participation is weak/absorbed while downside premium or hedge pressure remains more relevant."))
    elif bullish_combo > bearish_combo * 1.25 and bullish_combo > 0:
        scores["Bullish"] += 8
        detail_clauses.insert(0, (1000.0, "Net ITM overlay leans bullish-repair: bullish pressure/covering is stronger while bearish premium is cooling or unwinding."))
    elif bearish_combo > 0 and bullish_combo > 0:
        scores["Expansion"] += 5
        detail_clauses.insert(0, (1000.0, "Net ITM overlay is mixed: both bullish-repair and bearish-risk pockets exist, so use it as a caution filter rather than a standalone directional signal."))

    if not detail_clauses:
        return "", scores

    # Keep commentary actionable and not too long: combined read + strongest 1-2 evidence clauses.
    detail_clauses = sorted(detail_clauses, key=lambda x: x[0], reverse=True)
    selected: List[str] = []
    for _, clause in detail_clauses:
        if clause not in selected:
            selected.append(clause)
        if len(selected) >= 3:
            break

    return "ITM positioning overlay: " + " ".join(selected), scores

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


def _nearest_action_rows(matrix: pd.DataFrame, tags: List[str], side: str, limit: int = 3, spot: float | None = None) -> List[str]:
    x = _rows_for_side(matrix, side, tags)
    if x.empty:
        return []
    side_u = side.upper()
    if spot:
        if side_u == "CE":
            x = x[x["strike"] >= spot].copy()
        elif side_u == "PE":
            x = x[x["strike"] <= spot].copy()
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
    spot_delta = spot - prev_spot if prev_spot else 0.0
    spot_pct = _pct_change(spot, prev_spot) if prev_spot else 0.0
    poc_move = _movement(poc, prev_poc)
    ce_move = _movement(ce_wall, prev_ce_wall)
    pe_move = _movement(pe_wall, prev_pe_wall)
    range_move = _range_shift(ce_wall - pe_wall if ce_wall and pe_wall else 0, prev_ce_wall - prev_pe_wall if prev_ce_wall and prev_pe_wall else 0)

    key_strikes = _make_key_strikes(strikes, spot, poc, val, vah, ce_candidates, pe_candidates)
    prev_keys = _extract_prev_key_metrics(previous_summary)
    matrix = _build_matrix(key_strikes, prev_keys, spot, prev_spot)
    action_matrix = _actionable_matrix(matrix, spot)
    itm_overlay_line, itm_scores = _itm_positioning_overlay(matrix, spot)

    # Spot-adjusted event counts for actionable walls only.
    # ITM activity is handled separately as a positioning overlay.
    ce_defence = _count_tags(action_matrix, ["ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild"], "CE")
    ce_stress = _count_tags(action_matrix, ["ce_stress", "ce_failure", "ce_abnormal"], "CE")
    ce_failure = _count_tags(action_matrix, ["ce_failure"], "CE")
    pe_support = _count_tags(action_matrix, ["pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"], "PE")
    pe_stress = _count_tags(action_matrix, ["pe_trap", "pe_failure", "pe_stress"], "PE")
    pe_failure = _count_tags(action_matrix, ["pe_failure"], "PE")

    above_poc = spot > poc if poc else False
    below_poc = spot < poc if poc else False
    stretched = abs(spot - poc) / spot > 0.025 if spot and poc else False

    bullish = _sum_col(action_matrix, "bullish_pts") + int(itm_scores.get("Bullish", 0))
    bearish = _sum_col(action_matrix, "bearish_pts") + int(itm_scores.get("Bearish", 0))
    range_score = _sum_col(action_matrix, "range_pts") + int(itm_scores.get("Range", 0))
    expansion = _sum_col(action_matrix, "expansion_pts") + int(itm_scores.get("Expansion", 0))

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
        matrix=matrix,
        ce_action=ce_action,
        pe_action=pe_action,
        final_action=final_action,
        scores=scores,
        itm_overlay_line=itm_overlay_line,
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
    itm_overlay_line: str = "",
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

    ce_key = _nearest_action_rows(matrix, ["ce_stress", "ce_failure", "ce_abnormal"], "CE", 2, spot)
    ce_ctrl = _nearest_action_rows(matrix, ["ce_defended", "ce_defended_mild", "ce_control", "ce_control_mild"], "CE", 2, spot)
    pe_key = _nearest_action_rows(matrix, ["pe_trap", "pe_failure", "pe_stress"], "PE", 2, spot)
    pe_ctrl = _nearest_action_rows(matrix, ["pe_support", "pe_support_mild", "pe_defended", "pe_defended_mild"], "PE", 2, spot)

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
        f"Positioning: spot/future proxy is {_fmt_level(spot)}. Option POC {_fmt_level(poc)}; value area {_fmt_level(val)}–{_fmt_level(vah)}. {poc_line}"
        + (f" {itm_overlay_line}" if itm_overlay_line else "")
        + "\n\n"
        f"Walls: active CE wall {_fmt_level(ce_wall)} ({ce_move}); active PE wall {_fmt_level(pe_wall)} ({pe_move}); range is {range_move}.\n\n"
        f"CE Action: {ce_line}\n\n"
        f"PE Action: {pe_line}\n\n"
        f"{correction_line}\n\n" if correction_line else ""
    ) + (
        f"Final Action: {final_action}\n\n"
        f"Risk Note: {risk_line}"
    )
