"""Core ODME option-positioning engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from odme_config import RELEVANT_RANGE_PCT


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def _nearest_step(strikes: pd.Series) -> float:
    vals = np.sort(pd.Series(strikes).dropna().unique())
    if len(vals) < 2:
        return 1.0
    diffs = np.diff(vals)
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if len(diffs) else 1.0


def _norm(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").fillna(0.0)
    mx = s.max()
    if mx <= 0:
        return pd.Series(0.0, index=s.index)
    return s / mx


def _migration(values: List[float], step: float) -> str:
    clean = [v for v in values if v is not None and not pd.isna(v)]
    if len(clean) < 2:
        return "stable"
    delta = clean[-1] - clean[0]
    if abs(delta) < max(step * 0.75, 1e-9):
        return "stable"
    return "higher" if delta > 0 else "lower"


def _matrix_label(oi_change: float, premium_change: float) -> str:
    if oi_change > 0 and premium_change > 0:
        return "fresh buying / stress"
    if oi_change > 0 and premium_change <= 0:
        return "writing / control"
    if oi_change <= 0 and premium_change > 0:
        return "writer covering / failure risk"
    return "long liquidation / interest fading"


@dataclass
class ODMEAnalysis:
    decision: str
    tilt: str
    spot: float
    poc: float
    value_area_low: float
    value_area_high: float
    ce_wall: float
    pe_wall: float
    safer_sell_ce: float
    safer_sell_pe: float
    scores: Dict[str, float]
    commentary: List[str]
    tables: Dict[str, pd.DataFrame]
    meta: Dict[str, Any]


def analyze_memory(memory: pd.DataFrame, underlying: str) -> ODMEAnalysis:
    if memory is None or memory.empty:
        raise ValueError("No memory found. Initialize the expiry first.")

    df = memory.copy()
    required = ["snapshot_ts", "strike", "option_type", "ltp", "volume", "oi", "spot"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Memory missing required columns: {missing}")

    df["snapshot_ts"] = pd.to_datetime(df["snapshot_ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["snapshot_ts", "strike", "option_type"])
    for c in ["strike", "ltp", "volume", "oi", "spot"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    if df.empty:
        raise ValueError("Memory has no valid option rows.")

    latest_ts = df["snapshot_ts"].max()
    first_ts = df["snapshot_ts"].min()
    latest = df[df["snapshot_ts"].eq(latest_ts)].copy()
    first = df[df["snapshot_ts"].eq(first_ts)].copy()
    spot = _safe_float(latest["spot"].replace(0, np.nan).dropna().iloc[-1] if not latest["spot"].replace(0, np.nan).dropna().empty else latest["strike"].median())
    step = _nearest_step(latest["strike"])

    range_pct = RELEVANT_RANGE_PCT.get(underlying.upper(), 0.10)
    min_width = step * 8
    lo = min(spot * (1 - range_pct), spot - min_width)
    hi = max(spot * (1 + range_pct), spot + min_width)
    rel = latest[(latest["strike"] >= lo) & (latest["strike"] <= hi)].copy()
    if rel.empty:
        rel = latest.copy()

    latest_piv = rel.pivot_table(index="strike", columns="option_type", values=["oi", "volume", "ltp"], aggfunc="sum").fillna(0.0)
    latest_piv.columns = [f"{a}_{b}" for a, b in latest_piv.columns]
    for col in ["oi_CE", "oi_PE", "volume_CE", "volume_PE", "ltp_CE", "ltp_PE"]:
        if col not in latest_piv.columns:
            latest_piv[col] = 0.0
    latest_piv = latest_piv.reset_index()
    latest_piv["combined_oi"] = latest_piv["oi_CE"] + latest_piv["oi_PE"]
    latest_piv["combined_volume"] = latest_piv["volume_CE"] + latest_piv["volume_PE"]

    raw_full = latest.pivot_table(index="strike", columns="option_type", values="oi", aggfunc="sum").fillna(0.0)
    raw_full["combined_oi"] = raw_full.sum(axis=1)
    raw_full_poc = float(raw_full["combined_oi"].idxmax()) if not raw_full.empty and raw_full["combined_oi"].max() > 0 else float("nan")

    poc = float(latest_piv.loc[latest_piv["combined_oi"].idxmax(), "strike"]) if latest_piv["combined_oi"].max() > 0 else float(latest_piv["strike"].median())
    value_low, value_high = _value_area(latest_piv, poc, target_share=0.70)

    scored = _score_walls(df, latest, first, spot, step, lo, hi)
    ce_walls = scored[(scored["option_type"] == "CE") & (scored["strike"] >= spot - step)].sort_values("wall_score", ascending=False)
    pe_walls = scored[(scored["option_type"] == "PE") & (scored["strike"] <= spot + step)].sort_values("wall_score", ascending=False)
    ce_wall = float(ce_walls.iloc[0]["strike"]) if not ce_walls.empty else float("nan")
    pe_wall = float(pe_walls.iloc[0]["strike"]) if not pe_walls.empty else float("nan")
    safer_sell_ce = _safer_strike(ce_walls, spot, step, "CE")
    safer_sell_pe = _safer_strike(pe_walls, spot, step, "PE")

    hvn, lvn = _hvn_lvn(latest_piv)
    migrations = _migration_table(df, underlying, lo, hi, step)
    matrix = _matrix_for_key_strikes(df, latest_ts, first_ts, key_strikes=_key_strikes(ce_walls, pe_walls, poc, value_low, value_high, spot, step))
    scores = _decision_scores(latest_piv, matrix, migrations, spot, poc, value_low, value_high)
    decision, tilt = _decision_from_scores(scores)

    commentary = _commentary(decision, tilt, spot, poc, value_low, value_high, ce_wall, pe_wall, safer_sell_ce, safer_sell_pe, hvn, lvn, migrations, matrix, scores, step)

    return ODMEAnalysis(
        decision=decision,
        tilt=tilt,
        spot=spot,
        poc=poc,
        value_area_low=float(value_low),
        value_area_high=float(value_high),
        ce_wall=ce_wall,
        pe_wall=pe_wall,
        safer_sell_ce=safer_sell_ce,
        safer_sell_pe=safer_sell_pe,
        scores=scores,
        commentary=commentary,
        tables={
            "latest_profile": latest_piv.sort_values("strike"),
            "wall_scores": scored.sort_values("wall_score", ascending=False),
            "key_matrix": matrix,
            "hvn": hvn,
            "lvn": lvn,
            "migration": migrations,
        },
        meta={
            "latest_snapshot": str(latest_ts),
            "first_snapshot": str(first_ts),
            "snapshots": int(df["snapshot_ts"].nunique()),
            "rows": int(len(df)),
            "strike_step": float(step),
            "relevant_range_low": float(lo),
            "relevant_range_high": float(hi),
            "raw_full_chain_poc": raw_full_poc,
        },
    )


def _value_area(profile: pd.DataFrame, poc: float, target_share: float = 0.70) -> Tuple[float, float]:
    p = profile[["strike", "combined_oi"]].sort_values("strike").copy()
    if p.empty or p["combined_oi"].sum() <= 0:
        return float(poc), float(poc)
    total = p["combined_oi"].sum()
    p["dist"] = (p["strike"] - poc).abs()
    chosen = p.sort_values(["dist", "combined_oi"], ascending=[True, False]).copy()
    chosen["cum"] = chosen["combined_oi"].cumsum()
    selected = chosen[chosen["cum"] <= target_share * total]
    if selected.empty:
        selected = chosen.head(1)
    return float(selected["strike"].min()), float(selected["strike"].max())


def _score_walls(df: pd.DataFrame, latest: pd.DataFrame, first: pd.DataFrame, spot: float, step: float, lo: float, hi: float) -> pd.DataFrame:
    rel_latest = latest[(latest["strike"] >= lo) & (latest["strike"] <= hi)].copy()
    first_key = first.groupby(["strike", "option_type"], as_index=False).agg(first_oi=("oi", "sum"), first_ltp=("ltp", "mean"))
    latest_key = rel_latest.groupby(["strike", "option_type"], as_index=False).agg(current_oi=("oi", "sum"), volume=("volume", "sum"), ltp=("ltp", "mean"))
    counts = df[(df["strike"] >= lo) & (df["strike"] <= hi)].groupby(["strike", "option_type"], as_index=False).agg(
        snapshots_present=("oi", lambda s: int((s > 0).sum())), total_snapshots=("snapshot_ts", "nunique")
    )
    out = latest_key.merge(first_key, on=["strike", "option_type"], how="left").merge(counts, on=["strike", "option_type"], how="left")
    out["first_oi"] = out["first_oi"].fillna(0)
    out["buildup"] = (out["current_oi"] - out["first_oi"]).clip(lower=0)
    out["persistence"] = (out["snapshots_present"] / out["total_snapshots"].replace(0, np.nan)).fillna(0)
    out["proximity"] = 1 / (1 + ((out["strike"] - spot).abs() / max(step, 1.0)))
    out["wall_score"] = (
        0.36 * _norm(out["current_oi"])
        + 0.24 * _norm(out["buildup"])
        + 0.16 * _norm(out["volume"])
        + 0.14 * out["persistence"]
        + 0.10 * out["proximity"]
    ) * 100
    return out


def _safer_strike(walls: pd.DataFrame, spot: float, step: float, side: str) -> float:
    if walls.empty:
        return float("nan")
    if side == "CE":
        candidates = walls[walls["strike"] >= spot + 2 * step].sort_values(["strike", "wall_score"], ascending=[True, False])
    else:
        candidates = walls[walls["strike"] <= spot - 2 * step].sort_values(["strike", "wall_score"], ascending=[False, False])
    if candidates.empty:
        return float(walls.iloc[0]["strike"])
    top = candidates.sort_values("wall_score", ascending=False).head(5)
    # Choose a strong but slightly safer wall, not blindly the farthest strike.
    return float(top.iloc[0]["strike"])


def _hvn_lvn(profile: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    p = profile[["strike", "combined_oi", "combined_volume"]].copy()
    if p.empty:
        return p, p
    q80 = p["combined_oi"].quantile(0.80)
    q25 = p["combined_oi"].quantile(0.25)
    hvn = p[p["combined_oi"] >= q80].sort_values("combined_oi", ascending=False).head(8)
    lvn = p[p["combined_oi"] <= q25].sort_values("combined_oi", ascending=True).head(8)
    return hvn, lvn


def _migration_table(df: pd.DataFrame, underlying: str, lo: float, hi: float, step: float) -> pd.DataFrame:
    rows = []
    for ts, snap in df.groupby("snapshot_ts"):
        rel = snap[(snap["strike"] >= lo) & (snap["strike"] <= hi)].copy()
        if rel.empty:
            continue
        prof = rel.pivot_table(index="strike", columns="option_type", values="oi", aggfunc="sum").fillna(0.0)
        for c in ["CE", "PE"]:
            if c not in prof.columns:
                prof[c] = 0.0
        prof["combined"] = prof["CE"] + prof["PE"]
        ce_above = prof[prof.index >= _safe_float(snap["spot"].median()) - step]
        pe_below = prof[prof.index <= _safe_float(snap["spot"].median()) + step]
        rows.append({
            "snapshot_ts": ts,
            "spot": _safe_float(snap["spot"].median()),
            "poc": float(prof["combined"].idxmax()) if prof["combined"].max() > 0 else np.nan,
            "ce_wall": float(ce_above["CE"].idxmax()) if not ce_above.empty and ce_above["CE"].max() > 0 else np.nan,
            "pe_wall": float(pe_below["PE"].idxmax()) if not pe_below.empty and pe_below["PE"].max() > 0 else np.nan,
        })
    out = pd.DataFrame(rows).sort_values("snapshot_ts")
    if out.empty:
        return out
    out.attrs["poc_migration"] = _migration(out["poc"].tolist(), step)
    out.attrs["ce_migration"] = _migration(out["ce_wall"].tolist(), step)
    out.attrs["pe_migration"] = _migration(out["pe_wall"].tolist(), step)
    first_width = abs(out["ce_wall"].iloc[0] - out["pe_wall"].iloc[0]) if len(out) else np.nan
    last_width = abs(out["ce_wall"].iloc[-1] - out["pe_wall"].iloc[-1]) if len(out) else np.nan
    if pd.isna(first_width) or pd.isna(last_width) or abs(last_width - first_width) < step:
        out.attrs["range_migration"] = "stable"
    else:
        out.attrs["range_migration"] = "widening" if last_width > first_width else "narrowing"
    return out


def _key_strikes(ce_walls: pd.DataFrame, pe_walls: pd.DataFrame, poc: float, value_low: float, value_high: float, spot: float, step: float) -> List[float]:
    vals = []
    vals += ce_walls.head(3)["strike"].tolist() if not ce_walls.empty else []
    vals += pe_walls.head(3)["strike"].tolist() if not pe_walls.empty else []
    vals += [poc, value_low, value_high]
    vals += [round((spot + i * step) / step) * step for i in range(-2, 3)]
    return sorted({float(v) for v in vals if v is not None and not pd.isna(v)})


def _matrix_for_key_strikes(df: pd.DataFrame, latest_ts: pd.Timestamp, first_ts: pd.Timestamp, key_strikes: List[float]) -> pd.DataFrame:
    first = df[df["snapshot_ts"].eq(first_ts)].groupby(["strike", "option_type"], as_index=False).agg(first_oi=("oi", "sum"), first_ltp=("ltp", "mean"))
    latest = df[df["snapshot_ts"].eq(latest_ts)].groupby(["strike", "option_type"], as_index=False).agg(current_oi=("oi", "sum"), current_ltp=("ltp", "mean"), volume=("volume", "sum"))
    out = latest.merge(first, on=["strike", "option_type"], how="left")
    out = out[out["strike"].isin(key_strikes)].copy()
    out["first_oi"] = out["first_oi"].fillna(0)
    out["first_ltp"] = out["first_ltp"].fillna(out["current_ltp"])
    out["oi_change"] = out["current_oi"] - out["first_oi"]
    out["premium_change"] = out["current_ltp"] - out["first_ltp"]
    out["matrix"] = [_matrix_label(a, b) for a, b in zip(out["oi_change"], out["premium_change"])]
    return out.sort_values(["strike", "option_type"])


def _decision_scores(profile: pd.DataFrame, matrix: pd.DataFrame, migrations: pd.DataFrame, spot: float, poc: float, val_low: float, val_high: float) -> Dict[str, float]:
    ce_oi = profile["oi_CE"].sum()
    pe_oi = profile["oi_PE"].sum()
    total = max(ce_oi + pe_oi, 1.0)
    pcr = pe_oi / max(ce_oi, 1.0)
    # High PE walling below + controlled CE premium tends bullish/range; high CE walling above tends bearish/range.
    ce_control = len(matrix[(matrix["option_type"] == "CE") & (matrix["matrix"] == "writing / control")])
    pe_control = len(matrix[(matrix["option_type"] == "PE") & (matrix["matrix"] == "writing / control")])
    ce_stress = len(matrix[(matrix["option_type"] == "CE") & (matrix["matrix"].isin(["fresh buying / stress", "writer covering / failure risk"]))])
    pe_stress = len(matrix[(matrix["option_type"] == "PE") & (matrix["matrix"].isin(["fresh buying / stress", "writer covering / failure risk"]))])
    poc_mig = migrations.attrs.get("poc_migration", "stable") if migrations is not None else "stable"
    ce_mig = migrations.attrs.get("ce_migration", "stable") if migrations is not None else "stable"
    pe_mig = migrations.attrs.get("pe_migration", "stable") if migrations is not None else "stable"
    range_mig = migrations.attrs.get("range_migration", "stable") if migrations is not None else "stable"
    stretch = abs(spot - poc) / max((val_high - val_low), 1.0)

    bullish = 45 + 25 * min(max((pcr - 0.8) / 0.8, -1), 1) + 6 * pe_control - 7 * pe_stress
    bearish = 45 + 25 * min(max((1.2 - pcr) / 0.8, -1), 1) + 6 * ce_control - 7 * ce_stress
    if poc_mig == "higher":
        bullish += 10
    elif poc_mig == "lower":
        bearish += 10
    if pe_mig == "higher":
        bullish += 6
    if ce_mig == "lower":
        bearish += 6

    range_score = 50 + 8 * ce_control + 8 * pe_control - 10 * (range_mig == "widening") - 8 * min(stretch, 2)
    expansion = 30 + 10 * (range_mig == "widening") + 8 * (poc_mig != "stable") + 5 * (ce_stress + pe_stress) + 8 * min(stretch, 2)

    return {
        "Bullish": float(np.clip(bullish, 0, 100)),
        "Bearish": float(np.clip(bearish, 0, 100)),
        "Range": float(np.clip(range_score, 0, 100)),
        "Expansion": float(np.clip(expansion, 0, 100)),
        "PCR_OI": float(pcr),
        "Stretch_From_POC": float(stretch),
    }


def _decision_from_scores(scores: Dict[str, float]) -> Tuple[str, str]:
    core = {k: scores[k] for k in ["Bullish", "Bearish", "Range", "Expansion"]}
    best = max(core, key=core.get)
    sorted_scores = sorted(core.items(), key=lambda x: x[1], reverse=True)
    if sorted_scores[0][1] - sorted_scores[1][1] < 7:
        return "MIXED / NO CLEAN EDGE", "Neutral"
    mapping = {
        "Bullish": ("BULLISH POSITIONING", "Bullish"),
        "Bearish": ("BEARISH POSITIONING", "Bearish"),
        "Range": ("RANGE-BOUND THETA", "Range"),
        "Expansion": ("EXPANSION / TRAP RISK", "Expansion"),
    }
    return mapping[best]


def _commentary(decision, tilt, spot, poc, val_low, val_high, ce_wall, pe_wall, safer_ce, safer_pe, hvn, lvn, migrations, matrix, scores, step) -> List[str]:
    poc_mig = migrations.attrs.get("poc_migration", "stable") if migrations is not None else "stable"
    ce_mig = migrations.attrs.get("ce_migration", "stable") if migrations is not None else "stable"
    pe_mig = migrations.attrs.get("pe_migration", "stable") if migrations is not None else "stable"
    range_mig = migrations.attrs.get("range_migration", "stable") if migrations is not None else "stable"
    hvn_txt = ", ".join([str(int(x)) if float(x).is_integer() else f"{x:.2f}" for x in hvn["strike"].head(5).tolist()]) if not hvn.empty else "not clear"
    lvn_txt = ", ".join([str(int(x)) if float(x).is_integer() else f"{x:.2f}" for x in lvn["strike"].head(5).tolist()]) if not lvn.empty else "not clear"
    control = matrix[matrix["matrix"].eq("writing / control")]
    stress = matrix[matrix["matrix"].isin(["fresh buying / stress", "writer covering / failure risk"])]
    stretched = abs(spot - poc) > max(step * 2, (val_high - val_low) * 0.45)

    lines = []
    lines.append(f"ODME reads this expiry as {decision}. Current tilt is {tilt.lower()} with spot/future near {spot:.2f} and tradable option POC at {poc:.2f}.")
    lines.append(f"Option POC is shifting {poc_mig}; CE wall is shifting {ce_mig}; PE wall is shifting {pe_mig}; overall range is {range_mig}.")
    lines.append(f"Value area is {val_low:.2f} to {val_high:.2f}. HVN/friction is concentrated near {hvn_txt}. LVN/vacuum pockets are near {lvn_txt}.")
    if not control.empty:
        lines.append(f"Writers are controlling {len(control)} key strike-side combinations. That supports theta decay only while price stays inside value area and POC is not migrating aggressively.")
    if not stress.empty:
        lines.append(f"Stress/covering is visible on {len(stress)} key strike-side combinations. Do not blindly short the active wall if premium is rising with OI or writers are covering.")
    lines.append("POC can be treated as a positioning magnet only when it is stable. Since this engine tracks migration, a moving POC should be followed, not faded aggressively.")
    if stretched:
        lines.append("Price is stretched from option POC. That increases snap-back risk if POC is stable, but increases trend-continuation risk if POC keeps migrating with price.")
    lines.append(f"Strike guidance: safer Sell CE {safer_ce:.2f}, active CE wall {ce_wall:.2f}; safer Sell PE {safer_pe:.2f}, active PE wall {pe_wall:.2f}.")
    lines.append("For chart-engine contra trades: if short CE near supply but ODME shows CE stress rising, start with the safer higher CE. Shift down only after supply confirms and ODME shows writer control/premium decay. Reverse the same logic for PE near demand.")
    return lines
