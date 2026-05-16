from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from odme_config import DEFAULT_RELEVANT_RANGE_PCT, RELEVANT_RANGE_PCT


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def infer_spot(chain: pd.DataFrame, manual_spot: float | None = None) -> float:
    if manual_spot and manual_spot > 0:
        return float(manual_spot)
    df = chain.copy()
    if df.empty:
        return 0.0
    df["strike"] = _num(df["strike"])
    df["oi"] = _num(df["oi"])
    # Practical fallback: weighted median of strikes by total OI.
    agg = df.groupby("strike", as_index=False)["oi"].sum().sort_values("strike")
    agg = agg[agg["oi"] > 0]
    if agg.empty:
        return float(df["strike"].median())
    total = agg["oi"].sum()
    csum = agg["oi"].cumsum()
    return float(agg.loc[csum.ge(total / 2).idxmax(), "strike"])


def relevant_range(chain: pd.DataFrame, instrument: str, spot: float) -> Tuple[float, float]:
    pct = RELEVANT_RANGE_PCT.get(str(instrument).upper(), DEFAULT_RELEVANT_RANGE_PCT)
    if spot <= 0:
        strikes = _num(chain.get("strike", pd.Series(dtype=float)))
        if strikes.empty:
            return 0.0, 0.0
        return float(strikes.quantile(0.15)), float(strikes.quantile(0.85))
    return spot * (1 - pct), spot * (1 + pct)


def latest_snapshot(chain_memory: pd.DataFrame) -> pd.DataFrame:
    if chain_memory.empty or "snapshot_id" not in chain_memory.columns:
        return pd.DataFrame()
    sid = chain_memory["snapshot_id"].astype(str).iloc[-1]
    return chain_memory[chain_memory["snapshot_id"].astype(str).eq(sid)].copy()


def first_snapshot(chain_memory: pd.DataFrame) -> pd.DataFrame:
    if chain_memory.empty or "snapshot_id" not in chain_memory.columns:
        return pd.DataFrame()
    sid = chain_memory["snapshot_id"].astype(str).iloc[0]
    return chain_memory[chain_memory["snapshot_id"].astype(str).eq(sid)].copy()


def build_strike_table(chain_memory: pd.DataFrame, instrument: str, manual_spot: float | None = None) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if chain_memory.empty:
        return pd.DataFrame(), {"spot": 0.0, "notes": "No memory available."}
    df = chain_memory.copy()
    for c in ["strike", "ltp", "oi", "volume"]:
        df[c] = _num(df.get(c, pd.Series(dtype=float)))
    df["option_type"] = df["option_type"].astype(str).str.upper()
    latest = latest_snapshot(df)
    first = first_snapshot(df)
    spot = infer_spot(latest, manual_spot)
    lo, hi = relevant_range(latest, instrument, spot)
    latest_rel = latest[(latest["strike"] >= lo) & (latest["strike"] <= hi)].copy()
    first_rel = first[(first["strike"] >= lo) & (first["strike"] <= hi)].copy()

    def agg_side(frame: pd.DataFrame, side: str, prefix: str) -> pd.DataFrame:
        x = frame[frame["option_type"].eq(side)].groupby("strike", as_index=False).agg(
            **{f"{prefix}_oi": ("oi", "sum"), f"{prefix}_ltp": ("ltp", "mean"), f"{prefix}_volume": ("volume", "sum")}
        )
        return x

    ce_l = agg_side(latest_rel, "CE", "ce")
    pe_l = agg_side(latest_rel, "PE", "pe")
    ce_f = agg_side(first_rel, "CE", "first_ce")
    pe_f = agg_side(first_rel, "PE", "first_pe")
    strikes = pd.DataFrame({"strike": sorted(set(latest_rel["strike"].tolist()))})
    for part in [ce_l, pe_l, ce_f, pe_f]:
        strikes = strikes.merge(part, on="strike", how="left")
    for c in strikes.columns:
        if c != "strike":
            strikes[c] = _num(strikes[c])

    strikes["combined_oi"] = strikes["ce_oi"] + strikes["pe_oi"]
    strikes["combined_volume"] = strikes["ce_volume"] + strikes["pe_volume"]
    strikes["ce_buildup"] = strikes["ce_oi"] - strikes["first_ce_oi"]
    strikes["pe_buildup"] = strikes["pe_oi"] - strikes["first_pe_oi"]
    strikes["combined_buildup"] = strikes["ce_buildup"] + strikes["pe_buildup"]
    strikes["distance_pct"] = (strikes["strike"] - spot).abs() / spot if spot else 0

    persistence = df[(df["strike"] >= lo) & (df["strike"] <= hi)].copy()
    persistence["has_oi"] = persistence["oi"] > 0
    pers = persistence.groupby(["strike", "option_type"], as_index=False)["has_oi"].mean()
    ce_p = pers[pers["option_type"].eq("CE")][["strike", "has_oi"]].rename(columns={"has_oi": "ce_persistence"})
    pe_p = pers[pers["option_type"].eq("PE")][["strike", "has_oi"]].rename(columns={"has_oi": "pe_persistence"})
    strikes = strikes.merge(ce_p, on="strike", how="left").merge(pe_p, on="strike", how="left")
    strikes[["ce_persistence", "pe_persistence"]] = strikes[["ce_persistence", "pe_persistence"]].fillna(0)

    # Normalized wall score: OI, buildup, volume, persistence, proximity.
    def norm(col: str) -> pd.Series:
        s = _num(strikes[col])
        mx = s.max()
        return s / mx if mx > 0 else s * 0

    proximity = (1 - (strikes["distance_pct"] / max(strikes["distance_pct"].max(), 1e-9))).clip(0, 1)
    strikes["ce_wall_score"] = (
        0.38 * norm("ce_oi") + 0.22 * norm("ce_buildup").clip(lower=0) +
        0.18 * norm("ce_volume") + 0.12 * strikes["ce_persistence"] + 0.10 * proximity
    )
    strikes["pe_wall_score"] = (
        0.38 * norm("pe_oi") + 0.22 * norm("pe_buildup").clip(lower=0) +
        0.18 * norm("pe_volume") + 0.12 * strikes["pe_persistence"] + 0.10 * proximity
    )

    meta = {"spot": spot, "range_low": lo, "range_high": hi, "latest_rows": len(latest), "relevant_rows": len(latest_rel)}
    return strikes.sort_values("strike"), meta


def value_area(strikes: pd.DataFrame, pct: float = 0.70) -> Tuple[float, float]:
    if strikes.empty or strikes["combined_oi"].sum() <= 0:
        return 0.0, 0.0
    poc = float(strikes.sort_values("combined_oi", ascending=False).iloc[0]["strike"])
    selected = set([poc])
    total = strikes["combined_oi"].sum()
    selected_oi = strikes[strikes["strike"].isin(selected)]["combined_oi"].sum()
    ordered = strikes.copy().sort_values("strike").reset_index(drop=True)
    idx = int(ordered.index[ordered["strike"].eq(poc)][0])
    left, right = idx - 1, idx + 1
    while selected_oi < total * pct and (left >= 0 or right < len(ordered)):
        left_oi = ordered.loc[left, "combined_oi"] if left >= 0 else -1
        right_oi = ordered.loc[right, "combined_oi"] if right < len(ordered) else -1
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


def movement(current: float, previous: float) -> str:
    if not current or not previous:
        return "stable"
    diff = current - previous
    if abs(diff) < max(current, previous) * 0.001:
        return "stable"
    return "higher" if diff > 0 else "lower"


def _poc_from_snapshot(frame: pd.DataFrame, instrument: str, spot: float) -> float:
    if frame.empty:
        return 0.0
    lo, hi = relevant_range(frame, instrument, spot)
    x = frame.copy()
    x["strike"] = _num(x["strike"]); x["oi"] = _num(x["oi"])
    x = x[(x["strike"] >= lo) & (x["strike"] <= hi)]
    agg = x.groupby("strike", as_index=False)["oi"].sum()
    if agg.empty or agg["oi"].max() <= 0:
        return 0.0
    return float(agg.sort_values("oi", ascending=False).iloc[0]["strike"])


def analyze_odme(chain_memory: pd.DataFrame, instrument: str, manual_spot: float | None = None) -> Dict[str, Any]:
    strikes, meta = build_strike_table(chain_memory, instrument, manual_spot)
    if strikes.empty:
        return {"tilt": "MIXED / NO CLEAN EDGE", "error": "No option-chain memory available."}

    spot = meta["spot"]
    poc = float(strikes.sort_values("combined_oi", ascending=False).iloc[0]["strike"]) if strikes["combined_oi"].max() > 0 else 0.0
    vah, val = 0.0, 0.0
    val, vah = value_area(strikes)

    ce_candidates = strikes[strikes["strike"] >= spot].sort_values("ce_wall_score", ascending=False).head(3)
    pe_candidates = strikes[strikes["strike"] <= spot].sort_values("pe_wall_score", ascending=False).head(3)
    ce_wall = float(ce_candidates.iloc[0]["strike"]) if not ce_candidates.empty else 0.0
    pe_wall = float(pe_candidates.iloc[0]["strike"]) if not pe_candidates.empty else 0.0
    safer_ce = float(ce_candidates.sort_values("strike", ascending=False).iloc[0]["strike"]) if not ce_candidates.empty else 0.0
    safer_pe = float(pe_candidates.sort_values("strike", ascending=True).iloc[0]["strike"]) if not pe_candidates.empty else 0.0

    first = first_snapshot(chain_memory)
    latest = latest_snapshot(chain_memory)
    previous = pd.DataFrame()
    if "snapshot_id" in chain_memory.columns:
        ids = chain_memory["snapshot_id"].astype(str).drop_duplicates().tolist()
        if len(ids) >= 2:
            previous = chain_memory[chain_memory["snapshot_id"].astype(str).eq(ids[-2])].copy()
    prev_spot = infer_spot(previous, None) if not previous.empty else spot
    prev_poc = _poc_from_snapshot(previous, instrument, prev_spot) if not previous.empty else poc
    poc_move = movement(poc, prev_poc)

    # Wall migration uses previous scoring snapshot approximated by previous rows.
    prev_strikes, _ = build_strike_table(pd.concat([first, previous], ignore_index=True), instrument, prev_spot) if not previous.empty else (strikes, meta)
    prev_ce_wall = 0.0
    prev_pe_wall = 0.0
    if not prev_strikes.empty:
        pc = prev_strikes[prev_strikes["strike"] >= prev_spot].sort_values("ce_wall_score", ascending=False).head(1)
        pp = prev_strikes[prev_strikes["strike"] <= prev_spot].sort_values("pe_wall_score", ascending=False).head(1)
        prev_ce_wall = float(pc.iloc[0]["strike"]) if not pc.empty else ce_wall
        prev_pe_wall = float(pp.iloc[0]["strike"]) if not pp.empty else pe_wall

    ce_move = movement(ce_wall, prev_ce_wall)
    pe_move = movement(pe_wall, prev_pe_wall)
    range_width = ce_wall - pe_wall if ce_wall and pe_wall else 0
    prev_width = prev_ce_wall - prev_pe_wall if prev_ce_wall and prev_pe_wall else range_width
    if abs(range_width - prev_width) < max(range_width, prev_width, 1) * 0.01:
        range_move = "stable"
    else:
        range_move = "widening" if range_width > prev_width else "narrowing"

    # Key-strike matrix.
    key_strikes = sorted(set(
        ce_candidates["strike"].astype(float).tolist() + pe_candidates["strike"].astype(float).tolist() +
        [poc, val, vah] + _atm_nearby_strikes(strikes, spot, count=2)
    ))
    matrix_rows = []
    first_key = first.copy(); latest_key = latest.copy()
    for k in key_strikes:
        for side in ["CE", "PE"]:
            f = first_key[(pd.to_numeric(first_key["strike"], errors="coerce").eq(k)) & (first_key["option_type"].astype(str).str.upper().eq(side))]
            l = latest_key[(pd.to_numeric(latest_key["strike"], errors="coerce").eq(k)) & (latest_key["option_type"].astype(str).str.upper().eq(side))]
            if l.empty:
                continue
            doi = _safe_float(l["oi"].sum()) - _safe_float(f["oi"].sum() if not f.empty else 0)
            dltp = _safe_float(l["ltp"].mean()) - _safe_float(f["ltp"].mean() if not f.empty else 0)
            matrix_rows.append({"strike": k, "side": side, "delta_oi": doi, "delta_premium": dltp, "read": classify_matrix(doi, dltp)})
    matrix = pd.DataFrame(matrix_rows)

    ce_stress = _side_condition_score(matrix, "CE", ["fresh buying / stress", "writer covering / failure risk"])
    pe_stress = _side_condition_score(matrix, "PE", ["fresh buying / stress", "writer covering / failure risk"])
    ce_control = _side_condition_score(matrix, "CE", ["writing / control"])
    pe_control = _side_condition_score(matrix, "PE", ["writing / control"])

    above_poc = spot > poc if poc else False
    below_poc = spot < poc if poc else False
    stretched = abs(spot - poc) / spot > 0.025 if spot and poc else False

    bullish = 0
    bearish = 0
    range_score = 0
    expansion = 0

    bullish += 20 if pe_wall and pe_move in ["higher", "stable"] else 0
    bullish += 18 if pe_control >= ce_control else 0
    bullish += 15 if above_poc and poc_move in ["higher", "stable"] else 0
    bullish += 12 if ce_move == "higher" else 0
    bullish -= 15 if ce_stress > pe_stress else 0

    bearish += 20 if ce_wall and ce_move in ["lower", "stable"] else 0
    bearish += 18 if ce_control >= pe_control else 0
    bearish += 15 if below_poc and poc_move in ["lower", "stable"] else 0
    bearish += 12 if pe_move == "lower" else 0
    bearish -= 15 if pe_stress > ce_stress else 0

    range_score += 25 if range_move == "narrowing" else 10 if range_move == "stable" else 0
    range_score += 20 if ce_control > 0 and pe_control > 0 else 0
    range_score += 20 if not stretched and poc_move == "stable" else 0
    range_score += 10 if pe_wall < spot < ce_wall else 0

    expansion += 25 if range_move == "widening" else 0
    expansion += 25 if ce_stress > 0 and pe_stress > 0 else 0
    expansion += 20 if stretched and poc_move != "stable" else 0
    expansion += 15 if "failure risk" in " ".join(matrix.get("read", pd.Series(dtype=str)).astype(str).tolist()) else 0

    scores = {
        "Bullish": max(0, min(100, bullish)),
        "Bearish": max(0, min(100, bearish)),
        "Range": max(0, min(100, range_score)),
        "Expansion": max(0, min(100, expansion)),
    }
    tilt = _decide_tilt(scores)
    hvn = strikes.sort_values("combined_oi", ascending=False).head(5)[["strike", "combined_oi"]].to_dict("records")
    low = strikes[strikes["combined_oi"] > 0].sort_values("combined_oi", ascending=True).head(5)[["strike", "combined_oi"]].to_dict("records")

    commentary = build_commentary(
        tilt, spot, poc, val, vah, ce_wall, pe_wall, safer_ce, safer_pe,
        poc_move, ce_move, pe_move, range_move, ce_stress, pe_stress, ce_control, pe_control, stretched, hvn, low
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
        "lvn": low,
        "matrix": matrix,
        "strike_table": strikes,
        "commentary": commentary,
        "meta": meta,
    }


def _atm_nearby_strikes(strikes: pd.DataFrame, spot: float, count: int = 2) -> List[float]:
    s = strikes.copy()
    s["dist"] = (s["strike"] - spot).abs()
    return s.sort_values("dist").head(count * 2 + 1)["strike"].astype(float).tolist()


def _side_condition_score(matrix: pd.DataFrame, side: str, reads: List[str]) -> float:
    if matrix.empty:
        return 0.0
    x = matrix[matrix["side"].eq(side) & matrix["read"].isin(reads)]
    return float(len(x))


def _decide_tilt(scores: Dict[str, int]) -> str:
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ordered or ordered[0][1] < 25:
        return "MIXED / NO CLEAN EDGE"
    if len(ordered) > 1 and ordered[0][1] - ordered[1][1] < 8:
        return "MIXED / NO CLEAN EDGE"
    top = ordered[0][0]
    return {
        "Bullish": "BULLISH POSITIONING",
        "Bearish": "BEARISH POSITIONING",
        "Range": "RANGE-BOUND THETA",
        "Expansion": "EXPANSION / TRAP RISK",
    }.get(top, "MIXED / NO CLEAN EDGE")


def _fmt_level(x: float) -> str:
    return "NA" if not x else f"{x:,.0f}"


def build_commentary(
    tilt: str, spot: float, poc: float, val: float, vah: float, ce_wall: float, pe_wall: float,
    safer_ce: float, safer_pe: float, poc_move: str, ce_move: str, pe_move: str, range_move: str,
    ce_stress: float, pe_stress: float, ce_control: float, pe_control: float, stretched: bool,
    hvn: List[Dict[str, Any]], lvn: List[Dict[str, Any]]
) -> str:
    hvn_txt = ", ".join(_fmt_level(_safe_float(r.get("strike"))) for r in hvn[:5]) or "NA"
    lvn_txt = ", ".join(_fmt_level(_safe_float(r.get("strike"))) for r in lvn[:5]) or "NA"
    control_txt = "CE writers look more in control." if ce_control > pe_control else "PE writers look more in control." if pe_control > ce_control else "Both sides show similar writer control."
    stress_txt = "Call-side stress is rising." if ce_stress > pe_stress else "Put-side stress is rising." if pe_stress > ce_stress else "No clear one-sided stress is visible."
    poc_txt = "POC is stable, so it can act as a positioning magnet." if poc_move == "stable" else f"POC is migrating {poc_move}; avoid fading aggressively against that migration."
    stretch_txt = "Price is stretched from option POC." if stretched else "Price is not materially stretched from option POC."
    return (
        f"ODME reads the market as {tilt}. Spot/future proxy is near {_fmt_level(spot)} and tradable option POC is {_fmt_level(poc)}. "
        f"Value area is roughly {_fmt_level(val)}–{_fmt_level(vah)}. {poc_txt} {stretch_txt}\n\n"
        f"CE wall is {_fmt_level(ce_wall)} and has shifted {ce_move}; PE wall is {_fmt_level(pe_wall)} and has shifted {pe_move}. "
        f"The option range is {range_move}. {control_txt} {stress_txt}\n\n"
        f"HVN/friction zones: {hvn_txt}. LVN/vacuum zones: {lvn_txt}. "
        f"Strike guidance: safer Sell CE around {_fmt_level(safer_ce)}, active CE wall {_fmt_level(ce_wall)}; "
        f"safer Sell PE around {_fmt_level(safer_pe)}, active PE wall {_fmt_level(pe_wall)}. "
        f"If chart engine gives a contra short CE near supply while ODME still shows call stress, prefer safer higher CE first. "
        f"Shift down only after writer control and premium decay show up. Reverse the same logic for PE near demand."
    )
