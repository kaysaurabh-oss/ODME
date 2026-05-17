from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from odme_config import DEFAULT_RELEVANT_RANGE_PCT, RELEVANT_RANGE_PCT


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
    # Light version uses current OI + current volume + proximity. No full chain history needed.
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


def _build_matrix(current_keys: Dict[str, Dict[str, float]], previous_keys: Dict[str, Dict[str, float]]) -> pd.DataFrame:
    rows = []
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
            rows.append({
                "strike": strike,
                "side": side.upper(),
                "current_oi": cur_oi,
                "current_ltp": cur_ltp,
                "delta_oi_vs_previous": doi,
                "delta_premium_vs_previous": dltp,
                "read": classify_matrix(doi, dltp) if previous_keys else "first snapshot baseline",
            })
    return pd.DataFrame(rows)


def _condition_count(matrix: pd.DataFrame, side: str, phrases: List[str]) -> int:
    if matrix.empty:
        return 0
    x = matrix[matrix["side"].eq(side)]
    return int(x["read"].isin(phrases).sum())


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

    prev_poc = _safe_float(previous_summary.get("poc") or previous_summary.get("option_poc"))
    prev_ce_wall = _safe_float(previous_summary.get("ce_wall"))
    prev_pe_wall = _safe_float(previous_summary.get("pe_wall"))
    poc_move = _movement(poc, prev_poc)
    ce_move = _movement(ce_wall, prev_ce_wall)
    pe_move = _movement(pe_wall, prev_pe_wall)
    range_move = _range_shift(ce_wall - pe_wall if ce_wall and pe_wall else 0, prev_ce_wall - prev_pe_wall if prev_ce_wall and prev_pe_wall else 0)

    key_strikes = _make_key_strikes(strikes, spot, poc, val, vah, ce_candidates, pe_candidates)
    prev_keys = _extract_prev_key_metrics(previous_summary)
    matrix = _build_matrix(key_strikes, prev_keys)

    ce_stress = _condition_count(matrix, "CE", ["fresh buying / stress", "writer covering / failure risk"])
    pe_stress = _condition_count(matrix, "PE", ["fresh buying / stress", "writer covering / failure risk"])
    ce_control = _condition_count(matrix, "CE", ["writing / control"])
    pe_control = _condition_count(matrix, "PE", ["writing / control"])

    above_poc = spot > poc if poc else False
    below_poc = spot < poc if poc else False
    stretched = abs(spot - poc) / spot > 0.025 if spot and poc else False

    bullish = 0
    bearish = 0
    range_score = 0
    expansion = 0

    bullish += 20 if pe_wall and pe_move in ["higher", "stable", "first snapshot"] else 0
    bullish += 18 if pe_control >= ce_control and pe_control > 0 else 0
    bullish += 15 if above_poc and poc_move in ["higher", "stable", "first snapshot"] else 0
    bullish += 12 if ce_move == "higher" else 0
    bullish -= 12 if ce_stress > pe_stress else 0

    bearish += 20 if ce_wall and ce_move in ["lower", "stable", "first snapshot"] else 0
    bearish += 18 if ce_control >= pe_control and ce_control > 0 else 0
    bearish += 15 if below_poc and poc_move in ["lower", "stable", "first snapshot"] else 0
    bearish += 12 if pe_move == "lower" else 0
    bearish -= 12 if pe_stress > ce_stress else 0

    range_score += 25 if range_move == "narrowing" else 15 if range_move in ["stable", "first snapshot"] else 0
    range_score += 20 if ce_control > 0 and pe_control > 0 else 0
    range_score += 20 if not stretched and poc_move in ["stable", "first snapshot"] else 0
    range_score += 10 if pe_wall < spot < ce_wall else 0

    expansion += 25 if range_move == "widening" else 0
    expansion += 25 if ce_stress > 0 and pe_stress > 0 else 0
    expansion += 20 if stretched and poc_move not in ["stable", "first snapshot"] else 0
    expansion += 15 if "failure risk" in " ".join(matrix.get("read", pd.Series(dtype=str)).astype(str).tolist()) else 0

    scores = {
        "Bullish": int(max(0, min(100, bullish))),
        "Bearish": int(max(0, min(100, bearish))),
        "Range": int(max(0, min(100, range_score))),
        "Expansion": int(max(0, min(100, expansion))),
    }
    tilt = _decide_tilt(scores)
    hvn = strikes.sort_values("combined_oi", ascending=False).head(5)[["strike", "combined_oi"]].to_dict("records")
    lvn = strikes[strikes["combined_oi"] > 0].sort_values("combined_oi", ascending=True).head(5)[["strike", "combined_oi"]].to_dict("records")

    commentary = build_commentary(
        tilt, spot, poc, val, vah, ce_wall, pe_wall, safer_ce, safer_pe,
        poc_move, ce_move, pe_move, range_move, ce_stress, pe_stress, ce_control, pe_control,
        stretched, hvn, lvn, previous_summary
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
        "meta": meta,
    }


def build_commentary(
    tilt: str, spot: float, poc: float, val: float, vah: float, ce_wall: float, pe_wall: float,
    safer_ce: float, safer_pe: float, poc_move: str, ce_move: str, pe_move: str, range_move: str,
    ce_stress: int, pe_stress: int, ce_control: int, pe_control: int, stretched: bool,
    hvn: List[Dict[str, Any]], lvn: List[Dict[str, Any]], previous_summary: Dict[str, Any]
) -> str:
    hvn_txt = ", ".join(_fmt_level(r.get("strike")) for r in hvn[:5]) or "NA"
    lvn_txt = ", ".join(_fmt_level(r.get("strike")) for r in lvn[:5]) or "NA"
    prev_tilt = previous_summary.get("tilt") or previous_summary.get("odme_tilt") or "No previous snapshot"
    control_txt = "CE writers look more in control." if ce_control > pe_control else "PE writers look more in control." if pe_control > ce_control else "No clear one-sided writer control from the compact comparison."
    stress_txt = "Call-side stress is rising." if ce_stress > pe_stress else "Put-side stress is rising." if pe_stress > ce_stress else "No clear one-sided stress from the compact comparison."
    if poc_move == "first snapshot":
        poc_txt = "This is the first compact ODME snapshot, so POC migration will become meaningful from the next fetch."
    elif poc_move == "stable":
        poc_txt = "POC is stable, so it can act as a positioning magnet."
    else:
        poc_txt = f"POC is migrating {poc_move}; do not fade aggressively against that migration."
    stretch_txt = "Price is stretched from option POC." if stretched else "Price is not materially stretched from option POC."
    return (
        f"ODME reads the market as {tilt}. Previous saved tilt was {prev_tilt}. "
        f"Spot/future proxy is near {_fmt_level(spot)} and tradable option POC is {_fmt_level(poc)}. "
        f"Value area is roughly {_fmt_level(val)}–{_fmt_level(vah)}. {poc_txt} {stretch_txt}\n\n"
        f"CE wall is {_fmt_level(ce_wall)} and has shifted {ce_move}; PE wall is {_fmt_level(pe_wall)} and has shifted {pe_move}. "
        f"The option range is {range_move}. {control_txt} {stress_txt}\n\n"
        f"HVN/friction zones: {hvn_txt}. LVN/vacuum zones: {lvn_txt}. "
        f"Strike guidance: safer Sell CE around {_fmt_level(safer_ce)}, active CE wall {_fmt_level(ce_wall)}; "
        f"safer Sell PE around {_fmt_level(safer_pe)}, active PE wall {_fmt_level(pe_wall)}. "
        f"If chart engine gives a contra short CE near supply while ODME still shows call stress, prefer safer higher CE first. "
        f"Shift down only after writer control and premium decay show up. Reverse the same logic for PE near demand."
    )
