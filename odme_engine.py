from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from odme_config import DEFAULT_RELEVANT_RANGE_PCT, RELEVANT_RANGE_PCT


# =============================================================================
# ODME Engine v3
# Focus: compact live ODME summary, previous-vs-current comparison,
# writer-control / writer-defence-under-pressure / writer-failure logic,
# and actionable commentary without full-chain history.
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


def _premium_state(delta_premium: float, premium_pct: float) -> str:
    """Classify premium move with a practical tolerance to avoid tick-noise."""
    if abs(premium_pct) <= 3.0 or abs(delta_premium) <= 1.0:
        return "flat"
    return "up" if delta_premium > 0 else "down"


def _oi_state(delta_oi: float, oi_pct: float) -> str:
    if delta_oi <= 0:
        return "down"
    # Sharp OI addition means writers are still adding/defending. If premium is
    # rising at the same time, that is defence under pressure, not automatic failure.
    if oi_pct >= 12.0 or delta_oi >= 5000:
        return "up_sharp"
    return "up"


def _spot_adjusted_read(side: str, spot_delta: float, oi_delta: float, oi_pct: float, premium_delta: float, premium_pct: float) -> Tuple[str, str, int, int, int, int]:
    """Return: read, action_tag, bullish_pts, bearish_pts, range_pts, expansion_pts.

    v3 logic:
    - Writer control = OI rising + premium flat/falling.
    - Writer defence under pressure = OI rising sharply + premium rising.
      This is not failure yet; use safer strike first, then shift toward active
      wall only if next fetch confirms premium cooling while OI holds.
    - Writer failure = premium rising while OI falls/not holding.
    """
    side = side.upper()
    spot_dir = _direction(spot_delta, 0.0)
    prem = _premium_state(premium_delta, premium_pct)
    oi = _oi_state(oi_delta, oi_pct)
    oi_up = oi in ["up", "up_sharp"]
    oi_sharp = oi == "up_sharp"
    oi_down = oi == "down"

    if spot_dir == "flat":
        if oi_up and prem in ["down", "flat"]:
            return "writers controlling while spot is flat", "writer_control", 0, 0, 12, 0
        if oi_sharp and prem == "up":
            return "writers defending under pressure despite flat spot", "defence_pressure", 0, 0, 4, 10
        if oi_down and prem == "up":
            return "writer exit risk despite flat spot", "writer_failure", 0, 0, 0, 14
        return "quiet / low conviction", "neutral", 0, 0, 4, 0

    if side == "CE":
        if spot_dir == "up":
            if oi_up and prem in ["down", "flat"]:
                return "CE writers are absorbing the rise", "ce_control", 0, 10, 14, 0
            if oi_sharp and prem == "up":
                return "CE writers still defending, but premium is rising: defence under pressure", "ce_defence_pressure", 5, 0, 3, 12
            if oi_up and prem == "up":
                return "CE wall under pressure; writers present but not in clean control", "ce_pressure", 8, 0, 0, 10
            if oi_down and prem == "up":
                return "CE writers covering / wall failing", "ce_failure", 20, 0, 0, 18
            if oi_down and prem in ["down", "flat"]:
                return "CE interest fading during rise", "ce_fading", 5, 0, 3, 0
        else:
            if oi_up and prem in ["down", "flat"]:
                return "fresh CE writing confirms overhead control", "ce_control", 0, 18, 10, 0
            if oi_sharp and prem == "up":
                return "CE writers adding, but premium firmness shows defence under pressure", "ce_defence_pressure", 0, 4, 3, 12
            if oi_up and prem == "up":
                return "abnormal CE premium firmness with fresh OI", "ce_pressure", 0, 0, 0, 12
            if oi_down and prem == "up":
                return "CE writer exit / upside hedge demand", "ce_failure", 10, 0, 0, 14
            if oi_down and prem in ["down", "flat"]:
                return "CE longs unwinding with falling spot", "ce_weak", 0, 8, 5, 0

    if side == "PE":
        if spot_dir == "up":
            if oi_up and prem in ["down", "flat"]:
                return "clean PE writing support", "pe_control", 20, 0, 10, 0
            if oi_sharp and prem == "up":
                return "PE writers adding, but premium is firm: support under pressure", "pe_defence_pressure", 4, 0, 3, 14
            if oi_up and prem == "up":
                return "PE premium firm despite rise; support is not clean", "pe_pressure", 0, 0, 0, 14
            if oi_down and prem == "up":
                return "PE writers exiting while protection demand stays firm", "pe_failure", 0, 18, 0, 18
            if oi_down and prem in ["down", "flat"]:
                return "PE unwinding during rise; rally accepted but support is weaker", "pe_unwind", 6, 0, 3, 0
        else:
            if oi_up and prem in ["down", "flat"]:
                return "PE writers absorbing the fall", "pe_control", 12, 0, 14, 0
            if oi_sharp and prem == "up":
                return "PE writers still defending, but premium is rising: defence under pressure", "pe_defence_pressure", 0, 5, 3, 14
            if oi_up and prem == "up":
                return "PE wall under pressure; writers present but not in clean control", "pe_pressure", 0, 10, 0, 12
            if oi_down and prem == "up":
                return "PE writers covering / wall failing", "pe_failure", 0, 20, 0, 18
            if oi_down and prem in ["down", "flat"]:
                return "PE interest fading during fall", "pe_fading", 0, 5, 3, 0

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
            oi_pct = _pct_change(cur_oi, prev_oi) if previous_keys else 0.0
            generic = classify_matrix(doi, dltp) if previous_keys else "first snapshot baseline"
            if previous_keys:
                adj_read, action_tag, b, br, r, e = _spot_adjusted_read(side.upper(), spot_delta, doi, oi_pct, dltp, dpct)
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
                "oi_change_pct": oi_pct if previous_keys else 0.0,
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


def _side_action(
    side: str,
    wall: float,
    safer: float,
    wall_move: str,
    pressure_count: int,
    control_count: int,
    failure_count: int,
    defence_pressure_count: int = 0,
) -> str:
    side = side.upper()
    if side == "CE":
        if failure_count > 0:
            return f"CE writers are failing/covering at the active wall {_fmt_level(wall)}. Do not sell this wall; use only safer higher CE around {_fmt_level(safer)} or reduce CE risk."
        if defence_pressure_count > 0:
            return f"CE writers are still defending {_fmt_level(wall)}, but premium is rising. Sell only safer higher CE around {_fmt_level(safer)} now; shift down toward {_fmt_level(wall)} only after the next fetch shows premium cooling while OI holds."
        if control_count > 0 and wall_move in ["stable", "lower", "first snapshot"]:
            return f"CE wall {_fmt_level(wall)} is working. Active wall selling is acceptable; if already in safer higher CE, shifting closer can be considered only while premium decay continues."
        if wall_move == "higher":
            return f"CE wall has shifted higher. Do not short the old resistance aggressively; prefer safer higher CE around {_fmt_level(safer)}."
        if pressure_count > control_count:
            return f"CE side has pressure but no clean failure. Keep CE selling conservative at {_fmt_level(safer)} and wait for decay before shifting closer."
        return f"No high-confidence CE action at the active wall; keep CE selling conservative near {_fmt_level(safer)}."
    else:
        if failure_count > 0:
            return f"PE writers are failing/covering at the active wall {_fmt_level(wall)}. Do not sell this wall; use only safer lower PE around {_fmt_level(safer)} or reduce PE risk."
        if defence_pressure_count > 0:
            return f"PE writers are still defending {_fmt_level(wall)}, but premium is rising/not cooling. Sell only safer lower PE around {_fmt_level(safer)} now; shift up toward {_fmt_level(wall)} only after the next fetch shows premium cooling while OI holds."
        if control_count > 0 and wall_move in ["stable", "higher", "first snapshot"]:
            return f"PE wall {_fmt_level(wall)} is working. Active wall selling is acceptable; if already in safer lower PE, shifting closer can be considered only while premium decay continues."
        if wall_move == "lower":
            return f"PE wall has shifted lower. Do not sell the old support aggressively; prefer safer lower PE around {_fmt_level(safer)}."
        if pressure_count > control_count:
            return f"PE side has pressure but no clean failure. Keep PE selling conservative at {_fmt_level(safer)} and wait for decay before shifting closer."
        return f"No high-confidence PE action at the active wall; keep PE selling conservative near {_fmt_level(safer)}."

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

    # Spot-adjusted event counts. v3 separates clean control, defence under pressure, and failure.
    ce_control = _count_tags(matrix, ["ce_control"], "CE")
    ce_defence_pressure = _count_tags(matrix, ["ce_defence_pressure"], "CE")
    ce_pressure = _count_tags(matrix, ["ce_pressure", "ce_defence_pressure"], "CE")
    ce_failure = _count_tags(matrix, ["ce_failure"], "CE")

    pe_control = _count_tags(matrix, ["pe_control"], "PE")
    pe_defence_pressure = _count_tags(matrix, ["pe_defence_pressure"], "PE")
    pe_pressure = _count_tags(matrix, ["pe_pressure", "pe_defence_pressure"], "PE")
    pe_failure = _count_tags(matrix, ["pe_failure"], "PE")

    above_poc = spot > poc if poc else False
    below_poc = spot < poc if poc else False
    stretched = abs(spot - poc) / spot > 0.025 if spot and poc else False

    bullish = _sum_col(matrix, "bullish_pts")
    bearish = _sum_col(matrix, "bearish_pts")
    range_score = _sum_col(matrix, "range_pts")
    expansion = _sum_col(matrix, "expansion_pts")

    # Structure/migration overlay. These are intentionally secondary to premium/OI behaviour.
    bullish += 12 if pe_move == "higher" else 0
    bullish += 8 if ce_move == "higher" else 0
    bullish += 8 if above_poc and poc_move in ["higher", "stable", "first snapshot"] else 0
    bullish += 8 if pe_control > pe_pressure and pe_control > 0 else 0

    bearish += 12 if ce_move == "lower" else 0
    bearish += 8 if pe_move == "lower" else 0
    bearish += 8 if below_poc and poc_move in ["lower", "stable", "first snapshot"] else 0
    bearish += 8 if ce_control > ce_pressure and ce_control > 0 else 0

    range_score += 15 if range_move in ["stable", "narrowing", "first snapshot"] else 0
    range_score += 12 if ce_control > 0 and pe_control > 0 else 0
    range_score += 10 if not stretched and poc_move in ["stable", "first snapshot"] else 0
    range_score += 8 if pe_wall < spot < ce_wall else 0

    expansion += 18 if range_move == "widening" else 0
    expansion += 18 if (ce_pressure > 0 or ce_failure > 0) and (pe_pressure > 0 or pe_failure > 0) else 0
    expansion += 12 if stretched and poc_move not in ["stable", "first snapshot"] else 0
    expansion += 15 if pe_pressure > pe_control and _direction(spot_delta) == "up" else 0  # key correction: PE premium firm during rise = trap risk
    expansion += 15 if ce_pressure > ce_control and _direction(spot_delta) == "down" else 0

    scores = {
        "Bullish": int(max(0, min(100, bullish))),
        "Bearish": int(max(0, min(100, bearish))),
        "Range": int(max(0, min(100, range_score))),
        "Expansion": int(max(0, min(100, expansion))),
    }
    tilt = _decide_tilt(scores)

    hvn = strikes.sort_values("combined_oi", ascending=False).head(5)[["strike", "combined_oi"]].to_dict("records")
    lvn = strikes[strikes["combined_oi"] > 0].sort_values("combined_oi", ascending=True).head(5)[["strike", "combined_oi"]].to_dict("records")

    ce_action = _side_action("CE", ce_wall, safer_ce, ce_move, ce_pressure, ce_control, ce_failure, ce_defence_pressure)
    pe_action = _side_action("PE", pe_wall, safer_pe, pe_move, pe_pressure, pe_control, pe_failure, pe_defence_pressure)
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
        ce_control=ce_control,
        ce_pressure=ce_pressure,
        ce_defence_pressure=ce_defence_pressure,
        ce_failure=ce_failure,
        pe_control=pe_control,
        pe_pressure=pe_pressure,
        pe_defence_pressure=pe_defence_pressure,
        pe_failure=pe_failure,
        stretched=stretched,
        previous_summary=previous_summary,
        matrix=matrix,
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
    ce_control: int,
    ce_pressure: int,
    ce_defence_pressure: int,
    ce_failure: int,
    pe_control: int,
    pe_pressure: int,
    pe_defence_pressure: int,
    pe_failure: int,
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

    ce_key = _nearest_action_rows(matrix, ["ce_pressure", "ce_defence_pressure", "ce_failure"], "CE", 2)
    ce_ctrl = _nearest_action_rows(matrix, ["ce_control"], "CE", 2)
    pe_key = _nearest_action_rows(matrix, ["pe_pressure", "pe_defence_pressure", "pe_failure"], "PE", 2)
    pe_ctrl = _nearest_action_rows(matrix, ["pe_control"], "PE", 2)

    if ce_failure > 0:
        ce_line = f"CE writers are failing at {', '.join(ce_key) if ce_key else _fmt_level(ce_wall)}. {ce_action}"
    elif ce_defence_pressure > 0:
        ce_line = f"CE writers are still defending, but under pressure at {', '.join(ce_key) if ce_key else _fmt_level(ce_wall)}. {ce_action}"
    elif ce_control > 0:
        ce_line = f"CE writers are in control at {', '.join(ce_ctrl) if ce_ctrl else _fmt_level(ce_wall)}. {ce_action}"
    elif ce_pressure > 0:
        ce_line = f"CE side shows pressure but no confirmed failure. {ce_action}"
    else:
        ce_line = f"CE side has no strong confirmation. {ce_action}"

    if pe_failure > 0:
        pe_line = f"PE writers are failing at {', '.join(pe_key) if pe_key else _fmt_level(pe_wall)}. {pe_action}"
    elif pe_defence_pressure > 0:
        pe_line = f"PE writers are still defending, but under pressure at {', '.join(pe_key) if pe_key else _fmt_level(pe_wall)}. {pe_action}"
    elif pe_control > 0:
        pe_line = f"PE writers are in control at {', '.join(pe_ctrl) if pe_ctrl else _fmt_level(pe_wall)}. {pe_action}"
    elif pe_pressure > 0:
        pe_line = f"PE side shows pressure but no confirmed failure. {pe_action}"
    else:
        pe_line = f"PE side has no strong confirmation. {pe_action}"

    spot_dir = _direction(spot_delta, 0.0)
    correction_line = ""
    if spot_dir == "up" and pe_defence_pressure > 0:
        correction_line = "Heads-up: PE writers are still adding/defending, but PE premium has not cooled. Use safer lower PE first; shift up only after the next fetch confirms premium decay with OI holding."
    elif spot_dir == "up" and pe_control > pe_pressure and pe_control > 0:
        correction_line = "PE support is clean because premium is soft/decaying while OI is being added. This supports bullish theta exposure."
    elif spot_dir == "up" and pe_failure > 0:
        correction_line = "Warning: spot is higher but PE premium remains firm while OI is not holding. That is not support; it is hedge demand/writer exit risk."
    elif spot_dir == "down" and ce_defence_pressure > 0:
        correction_line = "Heads-up: CE writers are still adding/defending, but CE premium has not cooled. Use safer higher CE first; shift down only after the next fetch confirms premium decay with OI holding."
    elif spot_dir == "down" and ce_control > ce_pressure and ce_control > 0:
        correction_line = "CE pressure is clean because premium is soft/decaying while OI is being added. This supports bearish theta exposure."
    elif spot_dir == "down" and ce_failure > 0:
        correction_line = "Warning: spot is lower but CE premium remains firm while OI is not holding. That is upside hedge/writer exit risk, not clean bearish control."

    risk_line = ""
    if tilt == "EXPANSION / TRAP RISK":
        risk_line = "Risk is elevated: avoid tight short strikes and avoid averaging into the active wall."
    elif tilt == "MIXED / NO CLEAN EDGE":
        risk_line = "Conviction is insufficient: use the app as a warning monitor, not as a fresh-entry trigger."
    elif tilt == "RANGE-BOUND THETA":
        risk_line = "Theta condition is acceptable only if strikes are kept outside the active walls and POC does not migrate with price."
    elif tilt == "BULLISH POSITIONING":
        risk_line = "Bias favours PE selling or holding bullish theta, but only while PE premium remains soft/decays and PE wall does not shift lower."
    elif tilt == "BEARISH POSITIONING":
        risk_line = "Bias favours CE selling or holding bearish theta, but only while CE premium remains soft/decays and CE wall does not shift higher."

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
