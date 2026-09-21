from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

REASONER_VERSION = "SB3.5_FINAL_POLISH_LIVE_TEST"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or _s(v) == "":
            return None
        return float(v)
    except Exception:
        return None


def _i(v: Any) -> Optional[int]:
    try:
        if v is None or _s(v) == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return _u(v) in {"TRUE", "1", "YES", "Y", "ON"}


def _fmt(v: Any) -> str:
    x = _f(v)
    if x is None:
        return "?"
    if abs(x) >= 1000:
        s = f"{x:,.2f}"
    else:
        s = f"{x:.4f}"
    return s.rstrip("0").rstrip(".")


def _source_map(evidence: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in evidence.get("tv", []) or []:
        if not isinstance(row, dict):
            continue
        src = _u(row.get("source"))
        if src:
            out[src] = row
    return out


def _prev_source_map(previous_evidence: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(previous_evidence, dict):
        return {}
    return _source_map(previous_evidence)


def _direction(v: Any) -> str:
    s = _u(v)
    if "BULL" in s or s == "UP":
        return "BULLISH"
    if "BEAR" in s or s == "DOWN":
        return "BEARISH"
    return "NEUTRAL"


def _opposite(direction: str) -> str:
    return "BEARISH" if direction == "BULLISH" else "BULLISH" if direction == "BEARISH" else "NEUTRAL"


def _aurora_read(state: Any) -> Dict[str, str]:
    s = _u(state)
    # Current colour keeps the locked execution meaning. Sequence interpretation
    # is handled separately in _aurora_transition; do not infer acceleration or
    # deceleration from Pink/Yellow in isolation.
    mapping = {
        "GREEN": ("BULLISH", "ACTIVE", "bullish trend state is active"),
        "YELLOW": ("BULLISH", "SLOWING", "bull-side transitional/slowing state"),
        "WHITE": ("NEUTRAL", "BALANCE", "confirmed balance state"),
        "PINK": ("BEARISH", "SLOWING", "bear-side transitional/slowing state"),
        "RED": ("BEARISH", "ACTIVE", "bearish trend state is active"),
    }
    d, phase, text = mapping.get(s, ("NEUTRAL", "UNKNOWN", "momentum state is unavailable"))
    return {"state": s, "direction": d, "phase": phase, "text": text}


def _aurora_transition(current: Dict[str, Any], previous: Dict[str, Any]) -> str:
    """Interpret AURORA as a sequence, never from colour alone.

    The live AURORA feed already carries aurora_prev + aurora_change. Prefer that
    exact Pine transition on FRESH_TURN. SuperBrain's previous scan is only a
    fallback when a genuinely newer AURORA bar/state is observed.
    """
    cur_state = _u(current.get("aurora_state"))
    feed_prev = _u(current.get("aurora_prev"))
    change = _u(current.get("aurora_change"))

    prev_state = ""
    if change == "FRESH_TURN" and feed_prev and feed_prev != cur_state:
        prev_state = feed_prev
    else:
        scan_prev = _u(previous.get("aurora_state"))
        cur_bar = _i(current.get("bar_time"))
        prev_bar = _i(previous.get("bar_time"))
        if scan_prev and scan_prev != cur_state and (cur_bar is None or prev_bar is None or cur_bar > prev_bar):
            prev_state = scan_prev

    if not cur_state:
        return "UNAVAILABLE"
    if not prev_state:
        return "ESTABLISHED"

    if cur_state == "WHITE":
        return "MOMENTUM_BALANCED"
    if cur_state == "GREEN":
        return "BULLISH_MOMENTUM_REACCELERATING" if prev_state == "YELLOW" else "BULLISH_MOMENTUM_PICKING_UP"
    if cur_state == "YELLOW":
        return "BULLISH_MOMENTUM_WEAKENING" if prev_state == "GREEN" else "BULLISH_MOMENTUM_PICKING_UP_EARLY"
    if cur_state == "RED":
        return "BEARISH_MOMENTUM_REACCELERATING" if prev_state == "PINK" else "BEARISH_MOMENTUM_PICKING_UP"
    if cur_state == "PINK":
        return "BEARISH_MOMENTUM_WEAKENING" if prev_state == "RED" else "BEARISH_MOMENTUM_PICKING_UP_EARLY"
    return "MOMENTUM_REGIME_CHANGED"

def _zone(edge: Dict[str, Any], role: str) -> Dict[str, Any]:
    p = "defender" if role == "DEFENDER" else "challenger"
    return {
        "role": role,
        "tf": _s(edge.get(f"{p}_tf")),
        "side": _u(edge.get(f"{p}_side")),
        "low": _f(edge.get(f"{p}_low")),
        "high": _f(edge.get(f"{p}_high")),
        "state": _u(edge.get(f"{p}_state")),
    }


def _zone_valid(z: Dict[str, Any]) -> bool:
    lo, hi = z.get("low"), z.get("high")
    return z.get("side") in {"DEMAND", "SUPPLY"} and lo is not None and hi is not None and hi >= lo


def _zone_relation(z: Dict[str, Any], price: Optional[float]) -> str:
    if not _zone_valid(z) or price is None:
        return "UNAVAILABLE"
    lo, hi, side = z["low"], z["high"], z["side"]
    if lo <= price <= hi:
        return "INSIDE"
    if side == "DEMAND":
        return "NORMAL_SIDE" if price > hi else "DISTAL_SIDE"
    return "NORMAL_SIDE" if price < lo else "DISTAL_SIDE"


def _normal_distance(z: Dict[str, Any], price: Optional[float]) -> Optional[float]:
    if not _zone_valid(z) or price is None:
        return None
    if z["side"] == "DEMAND" and price > z["high"]:
        return price - z["high"]
    if z["side"] == "SUPPLY" and price < z["low"]:
        return z["low"] - price
    return None


def _same_role_lineage(cur: Dict[str, Any], prev: Dict[str, Any]) -> bool:
    return bool(cur.get("tf") and cur.get("tf") == prev.get("tf") and cur.get("side") == prev.get("side"))


def _role_state(
    role: str,
    current_zone: Dict[str, Any],
    previous_zone: Dict[str, Any],
    current_price: Optional[float],
    previous_price: Optional[float],
    current_edge_bar: Optional[int],
    previous_edge_bar: Optional[int],
) -> Dict[str, Any]:
    relation = _zone_relation(current_zone, current_price)
    prev_relation = _zone_relation(previous_zone, previous_price)
    new_edge_bar = (
        current_edge_bar is not None
        and previous_edge_bar is not None
        and current_edge_bar > previous_edge_bar
    )
    lineage = _same_role_lineage(current_zone, previous_zone)

    if relation == "UNAVAILABLE":
        condition = "UNAVAILABLE"
        transition = "NONE"
    elif role == "DEFENDER":
        condition = {"NORMAL_SIDE": "PROTECTING", "INSIDE": "UNDER_TEST", "DISTAL_SIDE": "BREACHED"}[relation]
        transition = condition
        if lineage and new_edge_bar:
            if relation == "NORMAL_SIDE" and prev_relation == "INSIDE":
                transition = "HELD_AND_REJECTED"
            elif relation == "NORMAL_SIDE" and prev_relation == "DISTAL_SIDE":
                transition = "RECLAIMED"
            elif relation == "INSIDE" and prev_relation == "DISTAL_SIDE":
                transition = "RECLAIMING_INSIDE"
            elif relation == "INSIDE" and prev_relation == "NORMAL_SIDE":
                transition = "ENTERED_TEST"
            elif relation == "DISTAL_SIDE" and prev_relation != "DISTAL_SIDE":
                transition = "FRESH_CONFIRMED_BREACH"
            elif relation == "DISTAL_SIDE" and prev_relation == "DISTAL_SIDE":
                transition = "ACCEPTED_BEYOND"
            elif relation == "NORMAL_SIDE" and prev_relation == "NORMAL_SIDE":
                cd = _normal_distance(current_zone, current_price)
                pd = _normal_distance(previous_zone, previous_price)
                if cd is not None and pd is not None:
                    transition = "MOVING_AWAY" if cd > pd else "APPROACHING" if cd < pd else "STABLE"
        elif previous_zone and not lineage and new_edge_bar:
            transition = "ROLE_MIGRATED"
    else:
        condition = {"NORMAL_SIDE": "AHEAD", "INSIDE": "UNDER_TEST", "DISTAL_SIDE": "CROSSED"}[relation]
        transition = condition
        if lineage and new_edge_bar:
            if relation == "NORMAL_SIDE" and prev_relation == "INSIDE":
                transition = "REJECTED"
            elif relation == "NORMAL_SIDE" and prev_relation == "DISTAL_SIDE":
                transition = "RECLAIMED"
            elif relation == "INSIDE" and prev_relation == "DISTAL_SIDE":
                transition = "RECLAIMING_INSIDE"
            elif relation == "INSIDE" and prev_relation == "NORMAL_SIDE":
                transition = "ENTERED_TEST"
            elif relation == "DISTAL_SIDE" and prev_relation != "DISTAL_SIDE":
                transition = "FRESH_CONFIRMED_CROSS"
            elif relation == "DISTAL_SIDE" and prev_relation == "DISTAL_SIDE":
                transition = "ACCEPTED_BEYOND"
            elif relation == "NORMAL_SIDE" and prev_relation == "NORMAL_SIDE":
                cd = _normal_distance(current_zone, current_price)
                pd = _normal_distance(previous_zone, previous_price)
                if cd is not None and pd is not None:
                    transition = "APPROACHING" if cd < pd else "MOVING_AWAY" if cd > pd else "STABLE"
        elif previous_zone and not lineage and new_edge_bar:
            transition = "ROLE_MIGRATED"

    # A repeated user scan of the same EDGE bar is not new confirmation.
    if lineage and not new_edge_bar and previous_edge_bar is not None:
        if role == "DEFENDER" and relation == "DISTAL_SIDE":
            transition = "BREACH_PRESENT_AWAIT_NEXT_EDGE_BAR"
        elif role == "CHALLENGER" and relation == "DISTAL_SIDE":
            transition = "CROSS_PRESENT_AWAIT_NEXT_EDGE_BAR"

    return {
        **current_zone,
        "relation": relation,
        "condition": condition,
        "transition": transition,
        "previous_relation": prev_relation,
        "new_edge_bar": new_edge_bar,
        "same_lineage": lineage,
    }


def _fork_state(structure: Dict[str, Any]) -> Dict[str, Any]:
    pos = _u(structure.get("fork_position"))
    reclaim = _b(structure.get("fork_reclaimed_2sd"))
    reclaim_side = _u(structure.get("fork_reclaim_side"))
    return {
        "valid": _b(structure.get("fork_valid")),
        "position": pos,
        "slope": _u(structure.get("fork_slope")),
        "reclaimed_2sd": reclaim,
        "reclaim_side": reclaim_side,
        "long_stretched": pos in {"UPPER_1SD_TO_2SD", "ABOVE_UPPER_2SD"},
        "short_stretched": pos in {"LOWER_2SD_TO_1SD", "BELOW_LOWER_2SD"},
        "long_reversal_location": pos == "LOWER_2SD_TO_1SD" or (reclaim and reclaim_side == "LOWER"),
        "short_reversal_location": pos == "UPPER_1SD_TO_2SD" or (reclaim and reclaim_side == "UPPER"),
        "long_extreme_unreclaimed": pos == "BELOW_LOWER_2SD" and not (reclaim and reclaim_side == "LOWER"),
        "short_extreme_unreclaimed": pos == "ABOVE_UPPER_2SD" and not (reclaim and reclaim_side == "UPPER"),
    }


def _overlaps(low1: Optional[float], high1: Optional[float], low2: Optional[float], high2: Optional[float]) -> bool:
    if None in {low1, high1, low2, high2}:
        return False
    return max(low1, low2) <= min(high1, high2)


def _fp(edge: Dict[str, Any], side: str) -> Dict[str, Any]:
    p = "fp_d" if side == "DEMAND" else "fp_s"
    valid = _b(edge.get(f"{p}_valid"))
    low = _f(edge.get(f"{p}_low"))
    high = _f(edge.get(f"{p}_high"))
    strength = _s(edge.get(f"{p}_strength"))
    strength_score = _f(edge.get(f"{p}_strength_score"))
    hold = _f(edge.get(f"{p}_hold"))
    status = _u(edge.get(f"{p}_status"))
    context = _u(edge.get(f"{p}_context_state"))
    interacting = _b(edge.get(f"{p}_interacting"))
    waiting = _b(edge.get(f"{p}_waiting_confirmation"))
    breach_pending = _b(edge.get(f"{p}_breach_pending"))
    strong = _u(strength) in {"STRONG", "STRONGEST"} or (strength_score is not None and strength_score >= 74)
    held = hold is not None and hold >= 70
    active = status != "RUN" and context != "RUN" and not breach_pending
    live_location = interacting or waiting or context in {"INTERACTING_NOW", "WAITING_CONFIRM"}
    return {
        "side": side,
        "valid": valid,
        "zone_id": _s(edge.get(f"{p}_zone_id")),
        "low": low,
        "high": high,
        "strength": strength,
        "strength_score": strength_score,
        "hold": hold,
        "status": status,
        "context": context,
        "interacting": interacting,
        "waiting_confirmation": waiting,
        "breach_pending": breach_pending,
        "strong": strong,
        "held": held,
        "active": active,
        "live_location": live_location,
        "qualified": valid and strong and held and active,
        "event_type": _u(edge.get(f"{p}_event_type")),
        "event_score": _f(edge.get(f"{p}_event_score")),
    }


def _parent_for_fp(fp: Dict[str, Any], defender: Dict[str, Any], challenger: Dict[str, Any]) -> str:
    in_def = _overlaps(fp.get("low"), fp.get("high"), defender.get("low"), defender.get("high"))
    in_ch = _overlaps(fp.get("low"), fp.get("high"), challenger.get("low"), challenger.get("high"))
    if in_def and in_ch:
        return "DEFENDER+CHALLENGER"
    if in_def:
        return "DEFENDER"
    if in_ch:
        return "CHALLENGER"
    return "NONE"


def _liquidity(liq: Dict[str, Any], prev_liq: Dict[str, Any]) -> Dict[str, Any]:
    event = _u(liq.get("liq_event1_type") or liq.get("liq_last_event"))
    level = _f(liq.get("liq_event1_level") or liq.get("liq_event_level"))
    confirm_time = _i(liq.get("liq_event1_confirm_time"))
    prev_event = _u(prev_liq.get("liq_event1_type") or prev_liq.get("liq_last_event"))
    prev_time = _i(prev_liq.get("liq_event1_confirm_time"))
    is_new = bool(event) and (event != prev_event or (confirm_time is not None and prev_time is not None and confirm_time > prev_time))
    directional = "NEUTRAL"
    meaning = ""
    if event.startswith("BUY_") and event.endswith("_RUN"):
        directional, meaning = "BULLISH", "liquidity above was crossed and price remained above it"
    elif event.startswith("SELL_") and event.endswith("_RUN"):
        directional, meaning = "BEARISH", "liquidity below was crossed and price remained below it"
    elif event.startswith("BUY_") and event.endswith("_SWEEP"):
        directional, meaning = "BEARISH", "liquidity above was swept and price reclaimed back below"
    elif event.startswith("SELL_") and event.endswith("_SWEEP"):
        directional, meaning = "BULLISH", "liquidity below was swept and price reclaimed back above"
    return {
        "event": event,
        "level": level,
        "confirm_time": confirm_time,
        "is_new": is_new,
        "directional_read": directional,
        "meaning": meaning,
        "above_level": _f(liq.get("liq_buy_level")),
        "above_count": _i(liq.get("liq_buy_count")),
        "above_distance": _f(liq.get("liq_buy_distance")),
        "below_level": _f(liq.get("liq_sell_level")),
        "below_count": _i(liq.get("liq_sell_count")),
        "below_distance": _f(liq.get("liq_sell_distance")),
        "pending_count": _i(liq.get("liq_pending_count")),
    }

def _odme_direction(odme: Dict[str, Any]) -> str:
    tilt = _u(odme.get("odme_tilt"))
    if tilt == "BULLISH POSITIONING":
        return "BULLISH"
    if tilt == "BEARISH POSITIONING":
        return "BEARISH"
    return "NEUTRAL"


def _battlefield_half(edge: Dict[str, Any], price: Optional[float], macro: str) -> str:
    raw = edge.get("raw_json") if isinstance(edge.get("raw_json"), dict) else {}
    divider = _f(raw.get("battlefield_divider"))
    if price is None or divider is None or macro == "NEUTRAL":
        return "UNAVAILABLE"
    if macro == "BULLISH":
        return "DEFENDER_HALF" if price <= divider else "CHALLENGER_HALF"
    return "DEFENDER_HALF" if price >= divider else "CHALLENGER_HALF"


def _gate() -> Dict[str, Any]:
    return {"state": "ALLOW", "reasons": [], "supports": [], "cautions": []}


def _wait(g: Dict[str, Any], reason: str) -> None:
    if g["state"] != "BLOCK":
        g["state"] = "WAIT"
    if reason and reason not in g["reasons"]:
        g["reasons"].append(reason)


def _block(g: Dict[str, Any], reason: str) -> None:
    g["state"] = "BLOCK"
    if reason and reason not in g["reasons"]:
        g["reasons"].append(reason)


def _support(g: Dict[str, Any], reason: str) -> None:
    if reason and reason not in g["supports"]:
        g["supports"].append(reason)



def _caution(g: Dict[str, Any], reason: str) -> None:
    if reason and reason not in g["cautions"]:
        g["cautions"].append(reason)

def _battlefield_context(
    gate: Dict[str, Any], intended: str, macro: str, defender: Dict[str, Any], challenger: Dict[str, Any], battlefield_half: str
) -> None:
    """Add EDGE battlefield context without overriding the locked trade hierarchy.

    Only a broken strategic parent of an actual qualified POI is a hard veto; that
    check lives in _directional_gate. Everything here is support/caution context
    for later borderline handling, commentary, risk and eventual add/reduce logic.
    """
    macro_trade = macro != "NEUTRAL" and intended == macro

    if macro_trade:
        if defender.get("transition") in {"HELD_AND_REJECTED", "RECLAIMED", "MOVING_AWAY"}:
            _support(gate, "Defender context is supportive and price is moving away from strategic support/resistance")
        elif defender.get("condition") == "UNDER_TEST":
            _caution(gate, "Defender is under test; this weakens location quality but does not override the locked setup rules")
        elif defender.get("condition") == "BREACHED":
            _caution(gate, "Defender is breached; the old battlefield is weaker, but this is not by itself a trade-rule veto outside a dependent POI")

        if challenger.get("condition") == "UNDER_TEST":
            _caution(gate, "Challenger is under test; opposing control is nearby")
        elif challenger.get("transition") in {"FRESH_CONFIRMED_CROSS", "CROSS_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            _caution(gate, "Challenger has just been crossed; first-break chase risk is elevated until acceptance/reclaim clarifies")
        elif challenger.get("transition") in {"REJECTED", "RECLAIMED", "RECLAIMING_INSIDE"}:
            _caution(gate, "Challenger has rejected/reclaimed, so opposing control remains relevant")
        elif challenger.get("transition") == "ACCEPTED_BEYOND":
            _support(gate, "Challenger has accepted beyond on a later confirmed EDGE bar")

        if battlefield_half == "CHALLENGER_HALF" and challenger.get("condition") == "AHEAD":
            _caution(gate, "price is in the Challenger half of the battlefield; location is later, not automatically invalid")
    else:
        if defender.get("condition") == "BREACHED":
            _caution(gate, "the opposite macro Defender has failed; this helps reversal context but is not reversal permission on its own")
        if challenger.get("transition") == "ACCEPTED_BEYOND":
            _caution(gate, "the opposite macro Challenger has been accepted beyond; EDGE role reconfiguration still matters")


def _finalize_context(gate: Dict[str, Any]) -> None:
    has_support = bool(gate.get("supports"))
    has_caution = bool(gate.get("cautions"))
    if has_support and has_caution:
        gate["context_state"] = "MIXED"
    elif has_support:
        gate["context_state"] = "SUPPORTIVE"
    elif has_caution:
        gate["context_state"] = "CAUTION"
    else:
        gate["context_state"] = "NEUTRAL"

def _directional_gate(
    intended: str,
    mode: str,
    macro: str,
    odme_dir: str,
    exec_dir: str,
    aurora: Dict[str, str],
    fork: Dict[str, Any],
    poi: Optional[Dict[str, Any]],
    defender: Dict[str, Any],
    challenger: Dict[str, Any],
    battlefield_half: str,
    edge_ok: bool,
) -> Dict[str, Any]:
    g = _gate()
    if not edge_ok:
        _block(g, "authoritative EDGE execution state is not active")
        return g

    # Locked decision rules come first. Battlefield and stretch outside their
    # explicit rule branches are context, not automatic vetoes.
    _battlefield_context(g, intended, macro, defender, challenger, battlefield_half)

    has_odme = mode == "TV + ODME" and odme_dir in {"BULLISH", "BEARISH"}
    poi_intended = poi and poi.get("intended") == intended and poi.get("qualified")

    # Strategic location veto first.
    if poi_intended:
        if poi.get("parent_broken"):
            _block(g, "the strategic EDGE parent of the Footprint POI is breaking/accepted beyond")
            return g
        _support(g, "a qualified Strong/Strongest Footprint POI is active at a strategic location")

        # Preserve the locked manual hierarchy.
        if has_odme:
            votes = [macro, exec_dir, odme_dir]
            support = sum(v == intended for v in votes)
            oppose = sum(v == _opposite(intended) for v in votes)
            if support >= 2 and odme_dir == _opposite(intended):
                _wait(g, "ODME directly opposes the POI even though the other directional inputs lean with it")
            elif oppose >= 2:
                _block(g, "Macro / ODME / execution control are predominantly against this POI")
            elif support < 2:
                _wait(g, "directional permission is not established yet")
        else:
            if macro == _opposite(intended):
                _block(g, "Macro EDGE direction is directly against this POI and ODME is unavailable")
            elif macro == "NEUTRAL" and exec_dir != intended:
                _wait(g, "strategic direction is not established")

        if g["state"] == "BLOCK":
            return g

        # Exec OF is authority; AURORA times rather than reverses it.
        if exec_dir == intended:
            if aurora.get("direction") == intended and aurora.get("phase") == "ACTIVE":
                _support(g, "execution OF is aligned and AURORA has active momentum in the same direction")
            else:
                _wait(g, "location and execution control are valid, but AURORA timing is not in the active aligned state")
        elif exec_dir == "NEUTRAL":
            if aurora.get("direction") == intended and aurora.get("phase") == "ACTIVE":
                _support(g, "AURORA has transferred at the POI while execution OF is neutral")
            else:
                _wait(g, "the POI is armed, but momentum transfer has not completed")
        else:
            reversal_ok = fork.get("long_reversal_location") if intended == "BULLISH" else fork.get("short_reversal_location")
            extreme_bad = fork.get("long_extreme_unreclaimed") if intended == "BULLISH" else fork.get("short_extreme_unreclaimed")
            if extreme_bad:
                _wait(g, "price is beyond 2SD and has not reclaimed; no early reversal permission")
            elif not reversal_ok:
                _wait(g, "execution OF opposes and Pitchfork stretch/reclaim does not permit an early reversal")
            elif aurora.get("direction") == intended and aurora.get("phase") == "ACTIVE":
                _support(g, "qualified POI + Pitchfork stretch/reclaim + AURORA permit an early reversal against opposing OF")
            else:
                _wait(g, "reversal location is valid, but AURORA has not transferred into the intended direction")
        return g

    # No qualified POI: continuation only. No reversal privilege against OF.
    if has_odme:
        if odme_dir != intended:
            _block(g, "ODME does not provide this direction in no-man's-land")
            return g
        if exec_dir == _opposite(intended):
            _block(g, "ODME and execution OF conflict in no-man's-land")
            return g
        if aurora.get("direction") != intended or aurora.get("phase") != "ACTIVE":
            _wait(g, "direction exists, but AURORA timing is not in the active aligned state")
        if exec_dir == intended:
            _support(g, "ODME and execution OF align for continuation")
        else:
            _wait(g, "ODME points this way but execution OF is still neutral")
    else:
        if exec_dir == "NEUTRAL":
            _block(g, "without ODME or a qualified POI, neutral execution OF provides no directional authority")
            return g
        if exec_dir != intended:
            _block(g, "execution OF points the other way")
            return g
        if aurora.get("direction") == intended and aurora.get("phase") == "ACTIVE":
            _support(g, "execution OF and active AURORA momentum align")
        else:
            _wait(g, "execution OF has direction, but AURORA timing is not actively aligned")

    return g


def _poi(edge: Dict[str, Any], defender: Dict[str, Any], challenger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    demand = _fp(edge, "DEMAND")
    supply = _fp(edge, "SUPPLY")
    candidates = []
    for fp in [demand, supply]:
        if not (fp.get("valid") and fp.get("live_location")):
            continue
        parent = _parent_for_fp(fp, defender, challenger)
        if parent == "DEFENDER":
            parent_state = defender.get("condition", "")
            parent_transition = defender.get("transition", "")
        elif parent == "CHALLENGER":
            parent_state = challenger.get("condition", "")
            parent_transition = challenger.get("transition", "")
        elif parent == "DEFENDER+CHALLENGER":
            parent_state = "OVERLAP"
            parent_transition = f"{defender.get('transition','')}|{challenger.get('transition','')}"
        else:
            parent_state = "NONE"
            parent_transition = "NONE"
        parent_broken = False
        if parent in {"DEFENDER", "DEFENDER+CHALLENGER"}:
            parent_broken = parent_broken or defender.get("condition") == "BREACHED" or defender.get("transition") in {"FRESH_CONFIRMED_BREACH", "ACCEPTED_BEYOND", "BREACH_PRESENT_AWAIT_NEXT_EDGE_BAR"}
        if parent in {"CHALLENGER", "DEFENDER+CHALLENGER"}:
            parent_broken = parent_broken or challenger.get("condition") == "CROSSED" or challenger.get("transition") in {"FRESH_CONFIRMED_CROSS", "ACCEPTED_BEYOND", "CROSS_PRESENT_AWAIT_NEXT_EDGE_BAR"}
        q = bool(fp.get("qualified") and parent != "NONE")
        candidates.append({
            **fp,
            "parent": parent,
            "parent_state": parent_state,
            "parent_transition": parent_transition,
            "parent_broken": parent_broken,
            "qualified": q,
            "intended": "BULLISH" if fp.get("side") == "DEMAND" else "BEARISH",
        })
    if not candidates:
        return None
    qualified = [x for x in candidates if x.get("qualified")]
    if len(qualified) == 1:
        return qualified[0]
    if len(qualified) > 1:
        return {"qualified": False, "conflict": True, "intended": "NEUTRAL", "parent": "MULTIPLE", "parent_state": "CONFLICT"}
    # Return the live but unqualified POI so commentary can explain why it is not usable.
    return candidates[0]


def _posture(long_gate: Dict[str, Any], short_gate: Dict[str, Any]) -> str:
    ls, ss = long_gate.get("state"), short_gate.get("state")
    if ls == "ALLOW" and ss != "ALLOW":
        return "LONG_ELIGIBLE"
    if ss == "ALLOW" and ls != "ALLOW":
        return "SHORT_ELIGIBLE"
    if ls == "ALLOW" and ss == "ALLOW":
        return "CONFLICT_WAIT"
    if ls == "BLOCK" and ss == "BLOCK":
        return "NO_TRADE"
    return "WAIT"


_SGT = ZoneInfo("Asia/Singapore")


def _dt_from_any(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            sv = _s(value)
            if sv.replace(".", "", 1).isdigit():
                n = float(sv)
                if n > 10_000_000_000:
                    n /= 1000.0
                dt = datetime.fromtimestamp(n, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(sv.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_SGT)
    except Exception:
        return None


def _clock(value: Any) -> str:
    dt = _dt_from_any(value)
    return dt.strftime("%H:%M") if dt else "?"


def _freshness_line(evidence: Dict[str, Any], current: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    labels = [
        ("AURORA", "momentum"),
        ("STRUCTURE", "structure"),
        ("LIQUIDITY", "liquidity"),
        ("EDGE", "location/order-flow"),
    ]
    for src, label in labels:
        row = current.get(src, {})
        if row:
            parts.append(f"{label} {_clock(row.get('bar_time') or row.get('updated_at'))}")
    odme = evidence.get("odme", {}) or {}
    if evidence.get("odme_live") and odme:
        parts.append(f"options positioning {_clock(odme.get('ts'))}")
    scan = _clock(evidence.get("scanned_at"))
    detail = " | ".join(parts)
    return f"Basis latest confirmed market conditions — scan {scan} SGT" + (f" | {detail}." if detail else ".")


def _option_note(evidence: Dict[str, Any]) -> str:
    mapping = evidence.get("mapping", {}) or {}
    if not bool(mapping.get("odme_scan_enabled")):
        instrument = _s(evidence.get("instrument")) or "This instrument"
        return f"No options-positioning layer is available for {instrument} in this terminal, so this view is based on price, structure, momentum, order-flow and liquidity only."
    return ""


def _hurdle_book(edge: Dict[str, Any], defender: Dict[str, Any], challenger: Dict[str, Any], price: Optional[float]) -> List[Dict[str, Any]]:
    raw = edge.get("raw_json") if isinstance(edge.get("raw_json"), dict) else {}
    book = raw.get("fp_hurdle_book") if isinstance(raw.get("fp_hurdle_book"), dict) else {}
    hurdles = book.get("hurdles") if isinstance(book.get("hurdles"), list) else []
    out: List[Dict[str, Any]] = []
    for h in hurdles:
        if not isinstance(h, dict):
            continue
        low, high = _f(h.get("low")), _f(h.get("high"))
        side = _u(h.get("side"))
        if low is None or high is None or side not in {"DEMAND", "SUPPLY"}:
            continue
        if _u(h.get("status")) == "RUN" or _u(h.get("context")) == "RUN" or _b(h.get("breach_pending")):
            continue
        if not _b(h.get("entry_qualified")):
            continue
        fp = {"low": low, "high": high}
        parent = _parent_for_fp(fp, defender, challenger)
        if price is None:
            distance = None
        elif side == "SUPPLY":
            distance = max(0.0, low - price)
        else:
            distance = max(0.0, price - high)
        out.append({
            "side": side,
            "low": low,
            "high": high,
            "parent": parent,
            "strength": _s(h.get("strength")),
            "hold": _f(h.get("hold")),
            "status": _u(h.get("status")),
            "distance": distance,
            "position": _u(h.get("position")),
            "zone_id": _s(h.get("zone_id")),
        })
    out.sort(key=lambda x: (float("inf") if x.get("distance") is None else x.get("distance"), 0 if x.get("side") == "SUPPLY" else 1))
    return out


def _momentum_sentence(transition: str, state: str, exec_dir: str) -> str:
    control = (
        "Short-term buying pressure is present" if exec_dir == "BULLISH"
        else "Short-term selling pressure is present" if exec_dir == "BEARISH"
        else "Short-term directional pressure is neutral"
    )
    if transition in {"BULLISH_MOMENTUM_PICKING_UP", "BULLISH_MOMENTUM_REACCELERATING"}:
        return f"{control}, and bullish momentum is now active."
    if transition == "BULLISH_MOMENTUM_PICKING_UP_EARLY":
        return f"{control}, while bullish momentum is beginning to build but is not yet in the active confirmation state."
    if transition == "BULLISH_MOMENTUM_WEAKENING":
        return f"{control}, but bullish momentum is fading."
    if transition in {"BEARISH_MOMENTUM_PICKING_UP", "BEARISH_MOMENTUM_REACCELERATING"}:
        return f"{control}, and bearish momentum is now active."
    if transition == "BEARISH_MOMENTUM_PICKING_UP_EARLY":
        return f"{control}, while bearish momentum is beginning to build but is not yet in the active confirmation state."
    if transition == "BEARISH_MOMENTUM_WEAKENING":
        return f"{control}, but bearish momentum is fading."
    if state == "WHITE" or transition == "MOMENTUM_BALANCED":
        suffix = " That makes an immediate directional entry premature rather than automatically invalid." if exec_dir != "NEUTRAL" else ""
        return f"{control}, but momentum is currently balanced rather than actively directional.{suffix}"
    if state == "GREEN":
        return f"{control}, with bullish momentum active."
    if state == "RED":
        return f"{control}, with bearish momentum active."
    if state == "YELLOW":
        return f"{control}; momentum is on the bullish side but transitional rather than fully active."
    if state == "PINK":
        return f"{control}; momentum is on the bearish side but transitional, and its meaning depends on the preceding state."
    return f"{control}; momentum confirmation is not currently clear."


def _odme_lines(odme: Dict[str, Any]) -> List[str]:
    if not odme:
        return []
    lines: List[str] = []
    tilt = _u(odme.get("odme_tilt"))
    if tilt == "BULLISH POSITIONING":
        opening = "Options positioning leans bullish"
    elif tilt == "BEARISH POSITIONING":
        opening = "Options positioning leans bearish"
    else:
        opening = "Options positioning is mixed"

    ce = _f(odme.get("active_ce_wall") or odme.get("ce_wall"))
    pe = _f(odme.get("active_pe_wall") or odme.get("pe_wall"))
    safe_ce = _f(odme.get("safer_sell_ce"))
    safe_pe = _f(odme.get("safer_sell_pe"))
    anchors = []
    if ce is not None:
        anchors.append(f"call-side resistance around {_fmt(ce)}")
    if pe is not None:
        anchors.append(f"put-side support around {_fmt(pe)}")
    if anchors:
        opening += ", with " + " and ".join(anchors)
    lines.append(opening + ".")

    range_score = _f(odme.get("range_score"))
    expansion_score = _f(odme.get("expansion_score"))
    if range_score is not None and expansion_score is not None:
        if expansion_score > range_score:
            lines.append("The options map currently favors expansion more than a contained range, so premium-selling ideas need more distance and stronger non-arrival protection.")
        elif range_score > expansion_score:
            lines.append("The options map is more compatible with a contained range than expansion, which is supportive for non-arrival/theta only if the sold strike remains beyond the relevant danger levels.")
        else:
            lines.append("The options map is balanced between range and expansion; it is not giving a clean volatility preference yet.")

    safe = []
    if safe_ce is not None:
        safe.append(f"call-side safer boundary {_fmt(safe_ce)}")
    if safe_pe is not None:
        safe.append(f"put-side safer boundary {_fmt(safe_pe)}")
    if safe:
        lines.append("For non-arrival risk, the current options-derived reference levels are " + " and ".join(safe) + ".")
    return lines


def _market_lines(
    price: Optional[float], macro: str, defender: Dict[str, Any], challenger: Dict[str, Any], battlefield_half: str,
    aurora: Dict[str, str], aurora_transition: str, exec_dir: str, fork: Dict[str, Any], liquidity: Dict[str, Any],
    poi: Optional[Dict[str, Any]], hurdles: List[Dict[str, Any]], odme: Dict[str, Any], posture: str,
) -> List[str]:
    lines: List[str] = []
    ptxt = _fmt(price)

    if _zone_valid(defender):
        z = f"{_fmt(defender['low'])}–{_fmt(defender['high'])}"
        noun = "support" if defender.get("side") == "DEMAND" else "resistance"
        if defender.get("transition") in {"HELD_AND_REJECTED", "MOVING_AWAY"}:
            lines.append(f"Price has moved away from the major {noun} area at {z}; that earlier location has done its job and now acts as background support for the move rather than a fresh entry location.")
        elif defender.get("condition") == "UNDER_TEST":
            lines.append(f"Price is testing the major {noun} area at {z}. The location is still live, but it is under pressure and deserves caution until the reaction is clearer.")
        elif defender.get("transition") in {"FRESH_CONFIRMED_BREACH", "BREACH_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            lines.append(f"The major {noun} area at {z} has been breached. Any setup that depends on that location should be treated cautiously until price reclaims it or a new market structure forms.")
        elif defender.get("transition") == "ACCEPTED_BEYOND":
            lines.append(f"Price has remained beyond the former {noun} area at {z} on a later confirmed bar; the old location should no longer be treated as intact.")
        elif defender.get("transition") == "RECLAIMED":
            lines.append(f"The major {noun} area at {z} has been reclaimed after a prior breach, improving the location again but not by itself creating an entry.")

    if _zone_valid(challenger):
        z = f"{_fmt(challenger['low'])}–{_fmt(challenger['high'])}"
        noun = "resistance" if challenger.get("side") == "SUPPLY" else "support"
        if challenger.get("condition") == "UNDER_TEST":
            lines.append(f"Price is now inside the broader {noun} area at {z}; this is an active decision zone rather than a clean continuation area.")
        elif challenger.get("transition") in {"FRESH_CONFIRMED_CROSS", "CROSS_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            lines.append(f"Price has just crossed the broader {noun} area at {z}; the first break is not chased until later price action shows acceptance or reclaim.")
        elif challenger.get("transition") == "ACCEPTED_BEYOND":
            lines.append(f"Price has remained beyond the broader {noun} area at {z} on a later confirmed bar, so that barrier is no longer treated as intact.")
        elif challenger.get("transition") in {"REJECTED", "RECLAIMED", "RECLAIMING_INSIDE"}:
            lines.append(f"The broader {noun} area at {z} has rejected the attempt through it and remains relevant.")
        elif battlefield_half == "CHALLENGER_HALF":
            side = "upper" if challenger.get("side") == "SUPPLY" else "lower"
            lines.append(f"Price is trading in the {side} part of the current range, with broader {noun} still ahead at {z}. That makes chasing the move less attractive and increases the importance of the next demand/supply reaction.")

    lines.append(_momentum_sentence(aurora_transition, aurora.get("state", ""), exec_dir))

    if fork.get("position") == "ABOVE_UPPER_2SD":
        lines.append("Price is structurally extended above its normal path. Fresh longs here would be stretched; the extension can make overhead supply more relevant, but it does not trigger a short by itself.")
    elif fork.get("position") == "BELOW_LOWER_2SD":
        lines.append("Price is structurally extended below its normal path. Fresh shorts here would be stretched; the extension can make lower demand more relevant, but it does not trigger a long by itself.")
    elif fork.get("reclaimed_2sd"):
        lines.append("Price has reclaimed back inside an extreme structural boundary, which improves reversal location if the remaining entry conditions also line up.")

    above = liquidity.get("above_level")
    below = liquidity.get("below_level")
    if above is not None and price is not None and above > price:
        lines.append(f"Nearby liquidity sits above around {_fmt(above)}, so price can still probe higher before the next meaningful decision area.")
    elif below is not None and price is not None and below < price:
        lines.append(f"Nearby liquidity sits below around {_fmt(below)}, so price can still probe lower before the next meaningful decision area.")
    if liquidity.get("is_new") and liquidity.get("meaning"):
        lines.append(f"A fresh liquidity event near {_fmt(liquidity.get('level'))} shows that {liquidity.get('meaning')}. It is useful context, not a standalone trade signal.")

    future = None
    if posture in {"WAIT", "SHORT_ELIGIBLE", "NO_TRADE"}:
        future = next((h for h in hurdles if h.get("side") == "SUPPLY" and (price is None or h.get("low", 0) >= price)), None)
    if future is None and posture in {"WAIT", "LONG_ELIGIBLE", "NO_TRADE"}:
        future = next((h for h in hurdles if h.get("side") == "DEMAND" and (price is None or h.get("high", 0) <= price)), None)
    if future:
        z = f"{_fmt(future['low'])}–{_fmt(future['high'])}"
        side = "supply" if future.get("side") == "SUPPLY" else "demand"
        parent = ""
        if future.get("parent") == "CHALLENGER":
            parent = " inside the broader opposing decision area"
        elif future.get("parent") == "DEFENDER":
            parent = " inside the major supporting decision area"
        lines.append(f"A qualified {side} pocket is waiting at {z}{parent}. That is a more precise location to watch for the next reaction than chasing price in between levels.")

    lines.extend(_odme_lines(odme))
    return lines[:8]


def _translate_reason(reason: str) -> str:
    r = reason.lower()
    if "timing is not in the active aligned state" in r or "momentum transfer has not completed" in r or "has not transferred" in r:
        return "wait for momentum to become actively aligned with the intended direction"
    if "execution of points the other way" in r:
        return "wait for short-term directional control to stop opposing the setup"
    if "neutral execution of" in r:
        return "wait for short-term directional control to establish a clear side"
    if "directional permission is not established" in r or "strategic direction is not established" in r:
        return "wait for the broader structure and short-term pressure to establish directional permission"
    if "odme" in r and "opposes" in r:
        return "wait for options positioning to stop opposing the location-based setup"
    if "odme and execution of conflict" in r:
        return "wait for options positioning and short-term directional pressure to stop conflicting"
    if "pitchfork stretch/reclaim" in r or "2sd" in r:
        return "wait for a proper structural stretch/reclaim or for short-term directional control to neutralize"
    if "strategic edge parent" in r:
        return "wait for the demand/supply location to regain structural support or for a new valid location to form"
    if "authoritative edge execution state" in r:
        return "wait for the market-state feed to return to an active execution state"
    return "wait for the remaining directional conditions to align"


def _waiting_for(
    price: Optional[float], posture: str, exec_dir: str, aurora: Dict[str, str], poi: Optional[Dict[str, Any]],
    hurdles: List[Dict[str, Any]], challenger: Dict[str, Any], long_gate: Dict[str, Any], short_gate: Dict[str, Any], mode: str,
) -> List[str]:
    if posture == "LONG_ELIGIBLE":
        return ["The long-entry conditions are already satisfied; the next step is execution and risk placement rather than waiting for another signal."]
    if posture == "SHORT_ELIGIBLE":
        return ["The short-entry conditions are already satisfied; the next step is execution and risk placement rather than waiting for another signal."]

    waits: List[str] = []
    supply = next((h for h in hurdles if h.get("side") == "SUPPLY" and (price is None or h.get("low", 0) >= price)), None)
    demand = next((h for h in hurdles if h.get("side") == "DEMAND" and (price is None or h.get("high", 0) <= price)), None)

    if supply:
        z = f"{_fmt(supply['low'])}–{_fmt(supply['high'])}"
        parent = " inside the broader resistance area" if supply.get("parent") == "CHALLENGER" else ""
        waits.append(f"A probable short-location setup can develop if price trades into the qualified supply pocket at {z}{parent}, that supply holds/rejects, and bearish momentum becomes active while short-term selling pressure remains supportive of the short.")
    if demand:
        z = f"{_fmt(demand['low'])}–{_fmt(demand['high'])}"
        parent = " inside the broader support area" if demand.get("parent") == "CHALLENGER" else ""
        waits.append(f"A probable long-location setup can develop if price trades into the qualified demand pocket at {z}{parent}, that demand holds/rejects, and bullish momentum becomes active while short-term buying pressure remains supportive of the long.")

    if exec_dir == "BEARISH" and not (aurora.get("direction") == "BEARISH" and aurora.get("phase") == "ACTIVE"):
        waits.append("A continuation short can also qualify earlier if bearish momentum becomes fully active while selling pressure remains in control, even before price reaches the overhead supply pocket, provided the continuation conditions remain satisfied.")
    elif exec_dir == "BULLISH" and not (aurora.get("direction") == "BULLISH" and aurora.get("phase") == "ACTIVE"):
        waits.append("A continuation long can also qualify earlier if bullish momentum becomes fully active while buying pressure remains in control, even before price reaches the lower demand pocket, provided the continuation conditions remain satisfied.")

    if not waits:
        reasons = []
        for g in (long_gate, short_gate):
            reasons.extend(g.get("reasons", []) or [])
        for r in reasons[:2]:
            tr = _translate_reason(r)
            if tr and tr not in waits:
                waits.append(tr[0].upper() + tr[1:] + ".")
    return waits[:3]


def _view_change_line(challenger: Dict[str, Any], defender: Dict[str, Any], hurdles: List[Dict[str, Any]], posture: str) -> str:
    if posture == "WAIT":
        supply = next((h for h in hurdles if h.get("side") == "SUPPLY"), None)
        if supply and _zone_valid(challenger) and challenger.get("side") == "SUPPLY":
            return f"If price pushes through {_fmt(challenger.get('high'))} and then remains accepted above that broader resistance on a later confirmed bar, the current short-location idea should be abandoned."
        demand = next((h for h in hurdles if h.get("side") == "DEMAND"), None)
        if demand and _zone_valid(challenger) and challenger.get("side") == "DEMAND":
            return f"If price pushes through {_fmt(challenger.get('low'))} and then remains accepted below that broader support on a later confirmed bar, the current long-location idea should be abandoned."
    if _zone_valid(defender) and defender.get("condition") == "BREACHED":
        return "The earlier supporting location has failed; do not rely on the old market structure until it is reclaimed or replaced by a new valid structure."
    return ""

def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Deterministic market-state reasoning with locked-rule priority.

    Internal engine names remain internal. Visible commentary is expressed in
    trader language: support/resistance, demand/supply, momentum, directional
    pressure, liquidity, stretch, options positioning, and explicit conditions
    that would activate or invalidate the next setup.
    """
    current = _source_map(evidence)
    previous = _prev_source_map(previous_evidence or {})
    edge = current.get("EDGE", {})
    prev_edge = previous.get("EDGE", {})
    aur = current.get("AURORA", {})
    prev_aur = previous.get("AURORA", {})
    structure = current.get("STRUCTURE", {})
    liq = current.get("LIQUIDITY", {})
    prev_liq = previous.get("LIQUIDITY", {})

    price = _f(edge.get("close"))
    if price is None:
        for src in (aur, structure, liq):
            price = _f(src.get("close"))
            if price is not None:
                break

    macro = _direction(edge.get("macro"))
    exec_dir = _direction(edge.get("exec_of"))
    edge_ok = _u(edge.get("edge_tracking_state")) == "ACTIVE" and not _b(edge.get("edge_needs_alert_change"))

    defender_z = _zone(edge, "DEFENDER")
    challenger_z = _zone(edge, "CHALLENGER")
    prev_defender_z = _zone(prev_edge, "DEFENDER")
    prev_challenger_z = _zone(prev_edge, "CHALLENGER")
    prev_price = _f(prev_edge.get("close"))
    edge_bar = _i(edge.get("bar_time"))
    prev_edge_bar = _i(prev_edge.get("bar_time"))

    defender = _role_state("DEFENDER", defender_z, prev_defender_z, price, prev_price, edge_bar, prev_edge_bar)
    challenger = _role_state("CHALLENGER", challenger_z, prev_challenger_z, price, prev_price, edge_bar, prev_edge_bar)

    aurora = _aurora_read(aur.get("aurora_state"))
    aurora_transition = _aurora_transition(aur, prev_aur)
    fork = _fork_state(structure)
    liquidity = _liquidity(liq, prev_liq)
    battlefield_half = _battlefield_half(edge, price, macro)
    poi = _poi(edge, defender, challenger)
    odme = evidence.get("odme", {}) or {}
    odme_dir = _odme_direction(odme)
    mode = _s(evidence.get("mode")) or "TV only"

    long_gate = _directional_gate(
        "BULLISH", mode, macro, odme_dir, exec_dir, aurora, fork, poi,
        defender, challenger, battlefield_half, edge_ok,
    )
    short_gate = _directional_gate(
        "BEARISH", mode, macro, odme_dir, exec_dir, aurora, fork, poi,
        defender, challenger, battlefield_half, edge_ok,
    )
    _finalize_context(long_gate)
    _finalize_context(short_gate)
    posture = _posture(long_gate, short_gate)

    role_mismatch = False
    if macro == "BULLISH":
        role_mismatch = defender.get("side") not in {"", "DEMAND"} or challenger.get("side") not in {"", "SUPPLY"}
    elif macro == "BEARISH":
        role_mismatch = defender.get("side") not in {"", "SUPPLY"} or challenger.get("side") not in {"", "DEMAND"}
    if role_mismatch:
        _block(long_gate, "EDGE role sides are inconsistent with the declared macro direction")
        _block(short_gate, "EDGE role sides are inconsistent with the declared macro direction")
        posture = "NO_TRADE"

    hurdles = _hurdle_book(edge, defender, challenger, price)
    lines = _market_lines(
        price, macro, defender, challenger, battlefield_half, aurora, aurora_transition,
        exec_dir, fork, liquidity, poi, hurdles, odme if mode == "TV + ODME" else {}, posture,
    )
    waiting_for = _waiting_for(
        price, posture, exec_dir, aurora, poi, hurdles, challenger, long_gate, short_gate, mode,
    )
    view_change = _view_change_line(challenger, defender, hurdles, posture)

    existing = []
    for t in open_trades or []:
        existing.append({
            "trade_id": _s(t.get("trade_id")),
            "strategy_type": _s(t.get("strategy_type")),
            "direction": _s(t.get("direction")),
            "status": _s(t.get("status")),
        })

    return {
        "reasoner_version": REASONER_VERSION,
        "freshness_line": _freshness_line(evidence, current),
        "option_note": _option_note(evidence),
        "price": price,
        "macro": macro,
        "exec_of": exec_dir,
        "odme_direction": odme_dir,
        "edge_active": edge_ok,
        "battlefield_half": battlefield_half,
        "defender": defender,
        "challenger": challenger,
        "aurora": {**aurora, "transition": aurora_transition},
        "fork": fork,
        "liquidity": liquidity,
        "poi": poi or {},
        "hurdles": hurdles,
        "long_gate": long_gate,
        "short_gate": short_gate,
        "posture": posture,
        "existing_exposure": existing,
        "lines": lines,
        "waiting_for": waiting_for,
        "view_change": view_change,
    }


# =============================================================================
# SB3.3 scan-action / exposure layer
# =============================================================================
# SB3.2 above remains the locked market-state authority. This layer adds
# scan-timing tolerance, ODME path/strike use, exposure follow-on management,
# and one concise trader-language narrative. No Pine formula is recreated here.

_analyze_market_sb32 = analyze_market
REASONER_VERSION = "SB3.5_FINAL_POLISH_LIVE_TEST"


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        import json as _json
        obj = _json.loads(str(value or ""))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _momentum_supports(direction: str, aurora: Dict[str, Any]) -> Tuple[bool, bool]:
    state = _u(aurora.get("state"))
    transition = _u(aurora.get("transition"))
    if direction == "BULLISH":
        return state == "GREEN", transition == "BULLISH_MOMENTUM_PICKING_UP_EARLY"
    return state == "RED", transition == "BEARISH_MOMENTUM_PICKING_UP_EARLY"


def _nearest_hurdle(hurdles: List[Dict[str, Any]], side: str, price: Optional[float]) -> Optional[Dict[str, Any]]:
    xs = []
    for h in hurdles or []:
        if _u(h.get("side")) != side:
            continue
        lo, hi = _f(h.get("low")), _f(h.get("high"))
        if lo is None or hi is None:
            continue
        if price is not None:
            if side == "SUPPLY" and hi < price:
                continue
            if side == "DEMAND" and lo > price:
                continue
        xs.append(h)
    if not xs:
        return None
    return sorted(xs, key=lambda h: abs((_f(h.get("low")) or price or 0.0) - (price or 0.0)))[0]


def _eta_context(evidence: Dict[str, Any], direction: str, price: Optional[float]) -> Dict[str, Any]:
    current = _source_map(evidence)
    aur = current.get("AURORA", {})
    odme = evidence.get("odme", {}) or {}
    if direction == "BULLISH":
        level = _f(aur.get("eta_up_level")); bars = _i(aur.get("eta_up_locked_remaining_bars")); regime = _u(aur.get("eta_up_regime"))
        wall = _f(odme.get("active_ce_wall") or odme.get("ce_wall"))
        path_key = "upside"
    else:
        level = _f(aur.get("eta_down_level")); bars = _i(aur.get("eta_down_locked_remaining_bars")); regime = _u(aur.get("eta_down_regime"))
        wall = _f(odme.get("active_pe_wall") or odme.get("pe_wall"))
        path_key = "downside"
    if price is not None and level is not None:
        if direction == "BULLISH" and level <= price:
            level, bars = None, None
        if direction == "BEARISH" and level >= price:
            level, bars = None, None
    path = ""
    pr = odme.get("path_risk")
    if isinstance(pr, dict) and isinstance(pr.get(path_key), dict):
        path = _s(pr[path_key].get("path"))
    text = ""
    if level is not None and bars is not None and bars > 0:
        text = f"The current momentum path points toward {_fmt(level)} over roughly {bars} confirmed bars"
        if regime:
            text += f" on a {regime.lower()} path"
        if wall is not None and price is not None:
            valid_wall = wall > price if direction == "BULLISH" else wall < price
            before = wall < level if direction == "BULLISH" else wall > level
            if valid_wall:
                text += f"; options positioning places a meaningful {'call-side resistance' if direction == 'BULLISH' else 'put-side support'} around {_fmt(wall)}"
                if before:
                    text += " before that destination, so arrival is conditional rather than assumed"
        if path:
            text += f". The options path is currently {path.lower()}"
        text += "."
    elif wall is not None and price is not None:
        if direction == "BULLISH" and wall > price:
            text = f"The next options-positioning hurdle on the upside sits around {_fmt(wall)}."
        elif direction == "BEARISH" and wall < price:
            text = f"The next options-positioning support on the downside sits around {_fmt(wall)}."
    return {"level": level, "bars": bars, "regime": regime, "wall": wall, "path": path, "text": text}


def _candidate_levels(base: Dict[str, Any], evidence: Dict[str, Any], direction: str) -> Dict[str, Any]:
    price = _f(base.get("price"))
    hurdles = base.get("hurdles", []) or []
    liq = base.get("liquidity", {}) or {}
    defender = base.get("defender", {}) or {}
    challenger = base.get("challenger", {}) or {}
    odme = evidence.get("odme", {}) or {}
    current = _source_map(evidence)
    aur = current.get("AURORA", {})

    if direction == "BULLISH":
        pull = _nearest_hurdle(hurdles, "DEMAND", price)
        invs = []
        for x in [pull.get("low") if pull else None,
                  challenger.get("low") if challenger.get("side") == "DEMAND" else None,
                  defender.get("low") if defender.get("side") == "DEMAND" else None]:
            v = _f(x)
            if v is not None and (price is None or v < price): invs.append(v)
        invalidation = max(invs) if invs else None
        targets = []
        liq_above = liq.get("above_level") if (_i(liq.get("above_count")) or 0) >= 2 else None
        for x in [liq_above, odme.get("active_ce_wall") or odme.get("ce_wall"), aur.get("eta_up_level"),
                  challenger.get("low") if challenger.get("side") == "SUPPLY" else None]:
            v = _f(x)
            if v is not None and price is not None and v > price: targets.append(v)
        target = min(targets) if targets else None
    else:
        pull = _nearest_hurdle(hurdles, "SUPPLY", price)
        invs = []
        for x in [pull.get("high") if pull else None,
                  challenger.get("high") if challenger.get("side") == "SUPPLY" else None,
                  defender.get("high") if defender.get("side") == "SUPPLY" else None]:
            v = _f(x)
            if v is not None and (price is None or v > price): invs.append(v)
        invalidation = min(invs) if invs else None
        targets = []
        liq_below = liq.get("below_level") if (_i(liq.get("below_count")) or 0) >= 2 else None
        for x in [liq_below, odme.get("active_pe_wall") or odme.get("pe_wall"), aur.get("eta_down_level"),
                  challenger.get("high") if challenger.get("side") == "DEMAND" else None]:
            v = _f(x)
            if v is not None and price is not None and v < price: targets.append(v)
        target = max(targets) if targets else None

    risk = reward = rr = None
    if price is not None and invalidation is not None:
        risk = abs(price - invalidation)
    if price is not None and target is not None:
        reward = abs(target - price)
    if risk and reward is not None and risk > 0:
        rr = reward / risk
    return {
        "target": target, "invalidation": invalidation, "risk": risk, "reward": reward, "rr": rr,
        "pullback_low": _f(pull.get("low")) if pull else None,
        "pullback_high": _f(pull.get("high")) if pull else None,
    }


def _premium_for(odme: Dict[str, Any], strike: float, option: str) -> Optional[float]:
    kp = odme.get("key_premiums") if isinstance(odme.get("key_premiums"), dict) else {}
    row = kp.get(str(int(round(strike))), {}) if kp else {}
    return _f(row.get("ce_ltp" if option == "CE" else "pe_ltp")) if isinstance(row, dict) else None


def _odme_option_strategy(base: Dict[str, Any], evidence: Dict[str, Any], direction: str, has_location: bool) -> Dict[str, Any]:
    if _s(evidence.get("mode")) != "TV + ODME":
        return {}
    odme = evidence.get("odme", {}) or {}
    price = _f(base.get("price"))
    if price is None or not odme:
        return {}
    tilt = _u(odme.get("odme_tilt"))
    range_score = _f(odme.get("range_score")) or 0.0
    expansion_score = _f(odme.get("expansion_score")) or 0.0
    if "EXPANSION" in tilt or expansion_score > range_score:
        return {}
    if direction == "BEARISH" and tilt == "BEARISH POSITIONING":
        wall = _f(odme.get("active_ce_wall") or odme.get("ce_wall")); safe = _f(odme.get("safer_sell_ce"))
        strike = wall if has_location and wall is not None and wall > price else safe
        if strike is not None and strike > price:
            leg = {"side": "SELL", "option": "CE", "strike": strike}
            prem = _premium_for(odme, strike, "CE")
            if prem is not None and prem > 0: leg["premium"] = prem
            return {"strategy_type": "SHORT_CE", "legs": [leg]}
    if direction == "BULLISH" and tilt == "BULLISH POSITIONING":
        wall = _f(odme.get("active_pe_wall") or odme.get("pe_wall")); safe = _f(odme.get("safer_sell_pe"))
        strike = wall if has_location and wall is not None and wall < price else safe
        if strike is not None and strike < price:
            leg = {"side": "SELL", "option": "PE", "strike": strike}
            prem = _premium_for(odme, strike, "PE")
            if prem is not None and prem > 0: leg["premium"] = prem
            return {"strategy_type": "SHORT_PE", "legs": [leg]}
    return {}


def _sale_action_ok(odme: Dict[str, Any], side: str) -> bool:
    side = _u(side)
    key = "ce_action" if side == "CE" else "pe_action"
    text = " ".join([_s(odme.get(key)), _s(odme.get("final_action")), _s(odme.get("hero_action"))]).lower()
    if not text:
        return False
    if "avoid active" in text or "no high-confidence" in text or "no fresh aggressive short option" in text:
        return False
    return f"{side.lower()} selling is acceptable" in text or "selling is acceptable" in text


def _nonarrival_theta_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Check one-sided non-arrival theta even when no directional entry is released.

    ODME remains authority for whether the option side is sellable. Market state is
    used only to reject a sale when price/momentum is actively accelerating toward
    the proposed sold strike. No premium or Greek assumption is required.
    """
    if _s(evidence.get("mode")) != "TV + ODME":
        return {}
    odme = evidence.get("odme", {}) or {}
    price = _f(base.get("price"))
    if price is None or not odme:
        return {}
    tilt = _u(odme.get("odme_tilt"))
    range_score = _f(odme.get("range_score")) or 0.0
    expansion_score = _f(odme.get("expansion_score")) or 0.0
    if "EXPANSION" in tilt or expansion_score > range_score:
        return {}
    exec_dir = _u(base.get("exec_of"))
    aur = base.get("aurora", {}) or {}
    aur_state = _u(aur.get("state"))
    posture = _u(base.get("posture"))

    if tilt == "BEARISH POSITIONING" and _sale_action_ok(odme, "CE"):
        # Do not sell calls into an already-qualified bullish continuation or
        # active bullish pressure/momentum accelerating toward the strike.
        if posture == "LONG_ELIGIBLE" or (exec_dir == "BULLISH" and aur_state == "GREEN"):
            return {}
        strike = _f(odme.get("safer_sell_ce") or odme.get("active_ce_wall") or odme.get("ce_wall"))
        if strike is not None and strike > price:
            leg = {"side": "SELL", "option": "CE", "strike": strike}
            prem = _premium_for(odme, strike, "CE")
            if prem is not None and prem > 0:
                leg["premium"] = prem
            eta = _eta_context(evidence, "BULLISH", price)
            return {
                "kind": "NEW", "action": "ENTER", "direction": "BEARISH",
                "strategy_type": "SHORT_CE", "legs": [leg],
                "entry_reference": price, "target": None, "invalidation": None,
                "expected_eta": ("Upside arrival risk: " + eta.get("text", "")) if eta.get("text") else "Non-arrival thesis; reassess if upside pressure strengthens or call-side protection shifts higher.",
                "late": False, "entry_quality": "CURRENT_NONARRIVAL",
                "reason": f"call-side resistance and options positioning currently protect the {_fmt(strike)} CE while price is not in an active bullish continuation",
            }

    if tilt == "BULLISH POSITIONING" and _sale_action_ok(odme, "PE"):
        if posture == "SHORT_ELIGIBLE" or (exec_dir == "BEARISH" and aur_state == "RED"):
            return {}
        strike = _f(odme.get("safer_sell_pe") or odme.get("active_pe_wall") or odme.get("pe_wall"))
        if strike is not None and strike < price:
            leg = {"side": "SELL", "option": "PE", "strike": strike}
            prem = _premium_for(odme, strike, "PE")
            if prem is not None and prem > 0:
                leg["premium"] = prem
            eta = _eta_context(evidence, "BEARISH", price)
            return {
                "kind": "NEW", "action": "ENTER", "direction": "BULLISH",
                "strategy_type": "SHORT_PE", "legs": [leg],
                "entry_reference": price, "target": None, "invalidation": None,
                "expected_eta": ("Downside arrival risk: " + eta.get("text", "")) if eta.get("text") else "Non-arrival thesis; reassess if downside pressure strengthens or put-side protection shifts lower.",
                "late": False, "entry_quality": "CURRENT_NONARRIVAL",
                "reason": f"put-side support and options positioning currently protect the {_fmt(strike)} PE while price is not in an active bearish continuation",
            }
    return {}


def _theta_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    if _s(evidence.get("mode")) != "TV + ODME":
        return {}
    odme = evidence.get("odme", {}) or {}
    price = _f(base.get("price"))
    if price is None or not odme:
        return {}
    tilt = _u(odme.get("odme_tilt")); rs = _f(odme.get("range_score")) or 0.0; es = _f(odme.get("expansion_score")) or 0.0
    safe_ce = _f(odme.get("safer_sell_ce")); safe_pe = _f(odme.get("safer_sell_pe"))
    aur = base.get("aurora", {}) or {}; exec_dir = _u(base.get("exec_of"))
    if tilt == "RANGE-BOUND THETA" and rs > es and exec_dir == "NEUTRAL" and _u(aur.get("state")) == "WHITE" and safe_ce and safe_pe and safe_pe < price < safe_ce:
        ce = {"side": "SELL", "option": "CE", "strike": safe_ce}; pe = {"side": "SELL", "option": "PE", "strike": safe_pe}
        cp = _premium_for(odme, safe_ce, "CE"); pp = _premium_for(odme, safe_pe, "PE")
        if cp is not None and cp > 0: ce["premium"] = cp
        if pp is not None and pp > 0: pe["premium"] = pp
        return {"kind": "NEW", "action": "ENTER", "direction": "NEUTRAL", "strategy_type": "SHORT_STRANGLE", "legs": [ce, pe],
                "entry_reference": price, "target": None, "invalidation": None, "expected_eta": "Range/non-arrival thesis; reassess on expansion or wall failure.",
                "late": False, "entry_quality": "CURRENT", "reason": "range-compatible options positioning with balanced directional pressure and momentum"}
    return {}


def _late_entry_plan(base: Dict[str, Any], evidence: Dict[str, Any], direction: str) -> Dict[str, Any]:
    if direction not in {"BULLISH", "BEARISH"}:
        return {}
    gate = base.get("long_gate" if direction == "BULLISH" else "short_gate", {}) or {}
    if gate.get("state") != "WAIT":
        return {}
    reasons = " ".join(gate.get("reasons", []) or []).lower()
    timing_only = any(x in reasons for x in ["aurora timing", "momentum transfer", "has not transferred", "timing is not in the active aligned state"])
    if not timing_only or _u(base.get("exec_of")) != direction:
        return {}
    active, early = _momentum_supports(direction, base.get("aurora", {}) or {})
    if not (active or early):
        return {}
    odme_dir = _u(base.get("odme_direction"))
    if _s(evidence.get("mode")) == "TV + ODME" and odme_dir in {"BULLISH", "BEARISH"} and odme_dir != direction:
        return {}
    levels = _candidate_levels(base, evidence, direction); price = _f(base.get("price"))
    lo, hi = levels.get("pullback_low"), levels.get("pullback_high")
    if price is None or lo is None or hi is None:
        return {}
    moved_away = price < lo if direction == "BEARISH" else price > hi
    if not moved_away:
        return {}
    # Positive remaining reward/risk is the minimum late-entry allowance; if it
    # has deteriorated below 1:1, call it missed rather than chasing it.
    if levels.get("rr") is not None and levels.get("rr") <= 1.0:
        return {}
    opt = _odme_option_strategy(base, evidence, direction, True)
    strategy = opt.get("strategy_type") or ("FUTURES_LONG" if direction == "BULLISH" else "FUTURES_SHORT")
    eta = _eta_context(evidence, direction, price)
    return {"kind": "NEW", "action": "ENTER", "direction": direction, "strategy_type": strategy, "legs": opt.get("legs", []),
            "entry_reference": price, "target": levels.get("target"), "invalidation": levels.get("invalidation"),
            "risk": levels.get("risk"), "reward": levels.get("reward"), "rr": levels.get("rr"), "pullback_low": lo, "pullback_high": hi,
            "expected_eta": eta.get("text", ""), "late": True, "entry_quality": "LATE_BUT_VALID",
            "reason": "the cleaner entry was earlier, but current directional pressure, momentum and remaining path still support entry without chasing beyond the first objective"}


def _new_entry_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    posture = _u(base.get("posture")); direction = "BULLISH" if posture == "LONG_ELIGIBLE" else "BEARISH" if posture == "SHORT_ELIGIBLE" else ""
    if direction:
        levels = _candidate_levels(base, evidence, direction); price = _f(base.get("price")); poi = base.get("poi", {}) or {}
        opt = _odme_option_strategy(base, evidence, direction, bool(poi.get("qualified")))
        strategy = opt.get("strategy_type") or ("FUTURES_LONG" if direction == "BULLISH" else "FUTURES_SHORT")
        # Futures need enough remaining path to justify the recorded invalidation.
        # Short-option structures are evaluated on non-arrival protection instead,
        # so underlying first-target R/R is not used as their veto.
        if strategy.startswith("FUTURES") and levels.get("rr") is not None and levels.get("rr") <= 1.0:
            return {"kind": "NONE", "action": "WAIT", "reason": "the directional setup qualifies, but the present entry has less remaining objective distance than invalidation risk",
                    "direction": direction, "pullback_low": levels.get("pullback_low"), "pullback_high": levels.get("pullback_high")}
        eta = _eta_context(evidence, direction, price)
        return {"kind": "NEW", "action": "ENTER", "direction": direction, "strategy_type": strategy, "legs": opt.get("legs", []),
                "entry_reference": price, "target": levels.get("target"), "invalidation": levels.get("invalidation"),
                "risk": levels.get("risk"), "reward": levels.get("reward"), "rr": levels.get("rr"),
                "pullback_low": levels.get("pullback_low"), "pullback_high": levels.get("pullback_high"),
                "expected_eta": eta.get("text", ""), "late": False, "entry_quality": "CURRENT", "reason": "price location, directional pressure and momentum satisfy the entry conditions on the current scan"}
    exec_dir = _u(base.get("exec_of")); late = _late_entry_plan(base, evidence, exec_dir)
    if late:
        return late
    nonarrival = _nonarrival_theta_plan(base, evidence)
    if nonarrival:
        return nonarrival
    return _theta_plan(base, evidence)


def _json_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    try:
        import json as _json
        obj = _json.loads(str(value or ""))
        return [x for x in obj if isinstance(x, dict)] if isinstance(obj, list) else []
    except Exception:
        return []


def _sold_strike(trade: Dict[str, Any], option: str) -> Optional[float]:
    for leg in _json_list(trade.get("legs_json")):
        if _u(leg.get("side")) == "SELL" and _u(leg.get("option")) == _u(option):
            return _f(leg.get("strike"))
    return None


def _manage_existing(base: Dict[str, Any], evidence: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not open_trades:
        return {}
    t = open_trades[0]; tid = _s(t.get("trade_id")); strategy = _u(t.get("strategy_type")); direction = _u(t.get("direction"))
    if direction not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        direction = "BULLISH" if ("LONG" in strategy or strategy == "SHORT_PE") else "BEARISH" if ("SHORT" in strategy or strategy == "SHORT_CE") else "NEUTRAL"
    price = _f(base.get("price")); target = _f(t.get("target")); inv = _f(t.get("invalidation")); meta = _json_obj(t.get("metadata_json"))
    pull_lo, pull_hi = _f(meta.get("pullback_low")), _f(meta.get("pullback_high"))
    if price is not None and inv is not None and direction in {"BULLISH", "BEARISH"}:
        bad = price <= inv if direction == "BULLISH" else price >= inv
        if bad: return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "INVALIDATED", "direction": direction, "strategy_type": strategy, "reason": "the recorded invalidation has been breached", "fresh_entry_now": False}
    if price is not None and target is not None and direction in {"BULLISH", "BEARISH"}:
        hit = price >= target if direction == "BULLISH" else price <= target
        if hit: return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "TARGET", "direction": direction, "strategy_type": strategy, "reason": "the first recorded objective has been reached", "fresh_entry_now": False}
    posture = _u(base.get("posture"))
    if (direction == "BULLISH" and posture == "SHORT_ELIGIBLE") or (direction == "BEARISH" and posture == "LONG_ELIGIBLE"):
        return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "CLOSED", "direction": direction, "strategy_type": strategy, "reason": "the market now qualifies the opposite directional setup", "fresh_entry_now": False}
    if strategy in {"SHORT_CE", "SHORT_PE"}:
        odme = evidence.get("odme", {}) or {}
        # If fresh options positioning is temporarily unavailable, keep the
        # model exposure but reduce conviction rather than inventing an exit.
        if _s(evidence.get("mode")) != "TV + ODME" or not odme:
            return {"kind": "MANAGE", "trade_id": tid, "action": "REDUCE", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "fresh options-positioning confirmation is unavailable on this scan", "fresh_entry_now": False}
        rs = _f(odme.get("range_score")) or 0.0; es = _f(odme.get("expansion_score")) or 0.0
        tilt = _u(odme.get("odme_tilt"))
        if strategy == "SHORT_CE":
            strike = _sold_strike(t, "CE")
            safe = _f(odme.get("safer_sell_ce")); adverse = tilt == "BULLISH POSITIONING"
            if price is not None and strike is not None and price >= strike:
                return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "CLOSED", "direction": direction, "strategy_type": strategy, "reason": "spot has reached or crossed the sold call strike", "fresh_entry_now": False}
            if "EXPANSION" in tilt or (adverse and _u(base.get("exec_of")) == "BULLISH" and _u((base.get("aurora") or {}).get("state")) == "GREEN"):
                return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "CLOSED", "direction": direction, "strategy_type": strategy, "reason": "upside arrival risk has materially increased against the sold call", "fresh_entry_now": False}
            if (strike is not None and safe is not None and safe > strike) or es > rs:
                return {"kind": "MANAGE", "trade_id": tid, "action": "REDUCE", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "call-side protection has weakened or expansion risk has increased", "fresh_entry_now": False}
            if _sale_action_ok(odme, "CE"):
                return {"kind": "MANAGE", "trade_id": tid, "action": "HOLD", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "call-side non-arrival protection remains intact", "fresh_entry_now": True}
        else:
            strike = _sold_strike(t, "PE")
            safe = _f(odme.get("safer_sell_pe")); adverse = tilt == "BEARISH POSITIONING"
            if price is not None and strike is not None and price <= strike:
                return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "CLOSED", "direction": direction, "strategy_type": strategy, "reason": "spot has reached or crossed the sold put strike", "fresh_entry_now": False}
            if "EXPANSION" in tilt or (adverse and _u(base.get("exec_of")) == "BEARISH" and _u((base.get("aurora") or {}).get("state")) == "RED"):
                return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "CLOSED", "direction": direction, "strategy_type": strategy, "reason": "downside arrival risk has materially increased against the sold put", "fresh_entry_now": False}
            if (strike is not None and safe is not None and safe < strike) or es > rs:
                return {"kind": "MANAGE", "trade_id": tid, "action": "REDUCE", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "put-side protection has weakened or expansion risk has increased", "fresh_entry_now": False}
            if _sale_action_ok(odme, "PE"):
                return {"kind": "MANAGE", "trade_id": tid, "action": "HOLD", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "put-side non-arrival protection remains intact", "fresh_entry_now": True}

    if direction == "NEUTRAL" and strategy == "SHORT_STRANGLE":
        odme = evidence.get("odme", {}) or {}; rs = _f(odme.get("range_score")) or 0.0; es = _f(odme.get("expansion_score")) or 0.0
        if es > rs or "EXPANSION" in _u(odme.get("odme_tilt")):
            return {"kind": "MANAGE", "trade_id": tid, "action": "EXIT", "status": "CLOSED", "direction": direction, "strategy_type": strategy, "reason": "options positioning has shifted from range containment toward expansion risk", "fresh_entry_now": False}
        return {"kind": "MANAGE", "trade_id": tid, "action": "HOLD", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "range containment remains intact", "fresh_entry_now": True}
    aur = base.get("aurora", {}) or {}; active, early = _momentum_supports(direction, aur); exec_ok = _u(base.get("exec_of")) == direction
    transition = _u(aur.get("transition")); weakening = transition == ("BULLISH_MOMENTUM_WEAKENING" if direction == "BULLISH" else "BEARISH_MOMENTUM_WEAKENING")
    if pull_lo is not None and pull_hi is not None and price is not None and pull_lo <= price <= pull_hi and exec_ok and (active or early):
        return {"kind": "MANAGE", "trade_id": tid, "action": "ADD", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "price has pulled back into the recorded re-entry area while directional pressure and momentum remain supportive", "fresh_entry_now": True}
    if weakening or not exec_ok:
        return {"kind": "MANAGE", "trade_id": tid, "action": "REDUCE", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "the original thesis is not invalidated, but momentum or short-term pressure has weakened", "fresh_entry_now": False}
    return {"kind": "MANAGE", "trade_id": tid, "action": "HOLD", "status": "ACTIVE", "direction": direction, "strategy_type": strategy, "reason": "the original thesis remains intact", "fresh_entry_now": bool(active and exec_ok)}


def _strategy_text(plan: Dict[str, Any]) -> str:
    st = _u(plan.get("strategy_type")); legs = plan.get("legs", []) or []
    if st == "SHORT_CE" and legs: return f"sell the {_fmt(legs[0].get('strike'))} CE"
    if st == "SHORT_PE" and legs: return f"sell the {_fmt(legs[0].get('strike'))} PE"
    if st == "SHORT_STRANGLE" and len(legs) >= 2:
        ce = next((x for x in legs if _u(x.get("option")) == "CE"), {}); pe = next((x for x in legs if _u(x.get("option")) == "PE"), {})
        return f"sell the {_fmt(ce.get('strike'))} CE and {_fmt(pe.get('strike'))} PE strangle"
    if st == "FUTURES_LONG": return "take a long futures exposure"
    if st == "FUTURES_SHORT": return "take a short futures exposure"
    return st.lower().replace("_", " ") or "take the exposure"


def _reentry_phrase(direction: str) -> str:
    if direction == "BEARISH":
        return "Red, or Pink only when it is a fresh bearish build from balance/bullish conditions rather than a fading Pink after Red"
    return "Green, or Yellow only when it is a fresh bullish build from balance/bearish conditions rather than a fading Yellow after Green"


def _narrative(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> str:
    price = _f(base.get("price")); lines = [str(x).strip() for x in (base.get("lines", []) or []) if str(x).strip()]
    # The base market reader already contains ODME context in its line list.
    # Keep the visible paragraph concise by taking only pure market-action lines
    # here, then append the current options map once below.
    market_lines = [x for x in lines if not x.startswith(("Options positioning", "The options map", "For non-arrival risk", "Options premium behaviour"))]
    lead = " ".join(market_lines[:4])
    odme = evidence.get("odme", {}) or {}; extra: List[str] = []
    if _s(evidence.get("mode")) == "TV + ODME" and odme:
        extra.extend(_odme_lines(odme)[:2])
        pa = _s(odme.get("premium_alert"))
        if pa: extra.append(pa.replace("Premium alert:", "Options premium behaviour:").strip())
    if plan.get("kind") == "NEW":
        direction = _u(plan.get("direction")); action = _strategy_text(plan)
        sent = f"SuperBrain is taking this exposure now: {action} at spot {_fmt(price)}."
        sent += " The cleaner entry was slightly earlier, but the setup remains valid now; this is late-but-still-valid rather than a chase." if plan.get("late") else " The current scan satisfies the entry conditions."
        if plan.get("reason"):
            sent += " " + _sentence_case(plan.get("reason")) + "."
        if plan.get("target") is not None: sent += f" The first objective is around {_fmt(plan.get('target'))}."
        if plan.get("invalidation") is not None: sent += f" The thesis is invalidated around {_fmt(plan.get('invalidation'))} or by confirmed acceptance beyond the relevant decision area."
        if plan.get("expected_eta"): sent += " " + _s(plan.get("expected_eta"))
        if plan.get("pullback_low") is not None and plan.get("pullback_high") is not None and direction in {"BULLISH", "BEARISH"}:
            sent += f" If price offers a better pullback/add opportunity, watch approximately {_fmt(plan.get('pullback_low'))}–{_fmt(plan.get('pullback_high'))}; use it only if that area still holds, short-term pressure stays aligned and momentum is {_reentry_phrase(direction)}."
        sent += " Future scans will manage this SuperBrain exposure as hold, add, reduce or exit rather than treating it as a new trade."
        return " ".join(x for x in [lead, *extra, sent] if x)
    if plan.get("kind") == "MANAGE":
        action = _u(plan.get("action")); phrase = {"HOLD":"hold", "ADD":"add to", "REDUCE":"reduce", "EXIT":"exit"}.get(action, action.lower())
        sent = f"SuperBrain action now is to {phrase} the existing exposure because {plan.get('reason','')}."
        direction = _u(plan.get("direction"))
        if plan.get("fresh_entry_now"):
            sent += " A new user can still enter on the present scan, using the current price and the same invalidation discipline rather than assuming the earlier entry price."
        elif direction in {"BULLISH", "BEARISH"}:
            lv = _candidate_levels(base, evidence, direction)
            if lv.get("pullback_low") is not None and lv.get("pullback_high") is not None:
                sent += f" A fresh user should not blindly copy the older exposure; prefer a pullback toward {_fmt(lv.get('pullback_low'))}–{_fmt(lv.get('pullback_high'))} only if pressure stays aligned and momentum is {_reentry_phrase(direction)}."
        return " ".join(x for x in [lead, *extra, sent] if x)
    waits = [str(x).strip() for x in (base.get("waiting_for", []) or []) if str(x).strip()]
    sent = "No SuperBrain exposure is taken on this scan."
    if plan.get("reason"):
        sent += " " + _sentence_case(plan.get("reason")) + "."
        if plan.get("pullback_low") is not None and plan.get("pullback_high") is not None and _u(plan.get("direction")) in {"BULLISH", "BEARISH"}:
            sent += f" Prefer a pullback toward {_fmt(plan.get('pullback_low'))}–{_fmt(plan.get('pullback_high'))} only if short-term pressure stays aligned and momentum is {_reentry_phrase(_u(plan.get('direction')))}."
    elif waits:
        sent += " " + waits[0]
        if len(waits) > 1: sent += " " + waits[1]
    if base.get("view_change"): sent += " " + _s(base.get("view_change"))
    return " ".join(x for x in [lead, *extra, sent] if x)


def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    base = _analyze_market_sb32(evidence, previous_evidence, open_trades=open_trades)
    trades = open_trades or []
    plan = _manage_existing(base, evidence, trades) if trades else _new_entry_plan(base, evidence)
    if not plan: plan = {"kind": "NONE", "action": "WAIT"}
    base["reasoner_version"] = REASONER_VERSION
    base["trade_plan"] = plan
    base["narrative"] = _narrative(base, evidence, plan)
    return base


# =============================================================================
# SB3.5 final polish overrides
# =============================================================================
# The market-state and trade rules above remain authority. These overrides only
# refine common-candle freshness, liquidity-aware execution, readable narrative,
# view labels, and follow-on exposure wording for live testing.

_market_lines_sb34 = _market_lines
_candidate_levels_sb34 = _candidate_levels
_new_entry_plan_sb34 = _new_entry_plan
_manage_existing_sb34 = _manage_existing


def _tf_minutes(tf: Any) -> Optional[int]:
    s = _u(tf)
    if not s:
        return None
    try:
        return int(float(s))
    except Exception:
        pass
    if s.endswith("H"):
        try:
            return int(float(s[:-1]) * 60)
        except Exception:
            return None
    if s in {"D", "1D"}:
        return 1440
    if s in {"W", "1W"}:
        return 10080
    return None


def _source_close_dt(source: str, row: Dict[str, Any]) -> Optional[datetime]:
    raw = row.get("bar_time") or row.get("updated_at")
    dt = _dt_from_any(raw)
    if dt is None:
        return None
    # STRUCTURE and LIQUIDITY alerts carry Pine `time` (bar open); AURORA and
    # EDGE carry the confirmed close timestamp. Normalize all four to bar close.
    if row.get("bar_time") not in (None, "") and _u(source) in {"STRUCTURE", "LIQUIDITY"}:
        mins = _tf_minutes(row.get("tf"))
        if mins:
            dt = dt + timedelta(minutes=mins)
    return dt


def _freshness_line(evidence: Dict[str, Any], current: Dict[str, Dict[str, Any]]) -> str:
    refs: List[Tuple[str, datetime, Optional[float]]] = []
    for src in ("AURORA", "EDGE", "STRUCTURE", "LIQUIDITY"):
        row = current.get(src, {}) or {}
        if not row:
            continue
        dt = _source_close_dt(src, row)
        if dt is not None:
            refs.append((src, dt, _f(row.get("close"))))
    if not refs:
        return f"Basis latest confirmed market conditions — scan {_clock(evidence.get('scanned_at'))} SGT."

    # Earliest normalized close is the newest candle confirmed by every live
    # source, avoiding a display that mixes 07:45 and 07:30 for the same market.
    ref_dt = min(x[1] for x in refs)
    price = None
    for pref in ("EDGE", "AURORA", "STRUCTURE", "LIQUIDITY"):
        for src, dt, close in refs:
            if src == pref and abs((dt - ref_dt).total_seconds()) < 1 and close is not None:
                price = close
                break
        if price is not None:
            break
    if price is None:
        for pref in ("EDGE", "AURORA", "STRUCTURE", "LIQUIDITY"):
            row = current.get(pref, {}) or {}
            price = _f(row.get("close"))
            if price is not None:
                break
    px = f" | Latest price {_fmt(price)}" if price is not None else ""
    return f"Basis latest confirmed market conditions — {ref_dt.strftime('%H:%M')} SGT candle close{px}."


def _option_note(evidence: Dict[str, Any]) -> str:
    # Availability is already shown beside the instrument selector in the UI.
    return ""


def _liquidity_commentary(price: Optional[float], liquidity: Dict[str, Any], exec_dir: str, posture: str) -> List[str]:
    out: List[str] = []
    event = _u(liquidity.get("event"))
    lvl = _f(liquidity.get("level"))
    if event and lvl is not None:
        if event.startswith("BUY_") and event.endswith("_RUN"):
            out.append(f"Recent upside liquidity around {_fmt(lvl)} was cleared and price held above it, so that pool has been run rather than left unfinished.")
        elif event.startswith("BUY_") and event.endswith("_SWEEP"):
            out.append(f"Recent upside liquidity around {_fmt(lvl)} was swept and reclaimed back below, showing rejection after the liquidity grab.")
        elif event.startswith("SELL_") and event.endswith("_RUN"):
            out.append(f"Recent downside liquidity around {_fmt(lvl)} was cleared and price held below it, so that pool has been run rather than left unfinished.")
        elif event.startswith("SELL_") and event.endswith("_SWEEP"):
            out.append(f"Recent downside liquidity around {_fmt(lvl)} was swept and reclaimed back above, showing rejection after the liquidity grab.")

    above = _f(liquidity.get("above_level")); below = _f(liquidity.get("below_level"))
    has_above = above is not None and price is not None and above > price
    has_below = below is not None and price is not None and below < price
    short_context = posture == "SHORT_ELIGIBLE" or exec_dir == "BEARISH"
    long_context = posture == "LONG_ELIGIBLE" or exec_dir == "BULLISH"
    if short_context:
        bits = []
        if has_above:
            bits.append(f"unfinished liquidity above around {_fmt(above)} is a near-term risk to a short, but it is not an automatic entry veto")
        if has_below:
            bits.append(f"liquidity below around {_fmt(below)} is a natural downside magnet if bearish conditions activate")
        if bits:
            
            if len(bits) > 1:
                first = bits[0][0].upper() + bits[0][1:]
                second = bits[1]
                out.append(first + ". At the same time, " + second + ".")
            else:
                sentence = bits[0]
                out.append(sentence[0].upper() + sentence[1:] + ".")
    elif long_context:
        bits = []
        if has_below:
            bits.append(f"unfinished liquidity below around {_fmt(below)} is a near-term risk to a long, but it is not an automatic entry veto")
        if has_above:
            bits.append(f"liquidity above around {_fmt(above)} is a natural upside magnet if bullish conditions activate")
        if bits:
            
            if len(bits) > 1:
                first = bits[0][0].upper() + bits[0][1:]
                second = bits[1]
                out.append(first + ". At the same time, " + second + ".")
            else:
                sentence = bits[0]
                out.append(sentence[0].upper() + sentence[1:] + ".")
    else:
        bits = []
        if has_above: bits.append(f"liquidity remains above around {_fmt(above)}")
        if has_below: bits.append(f"liquidity remains below around {_fmt(below)}")
        if bits:
            out.append("Unfinished " + " and ".join(bits) + "; these levels can attract price before the next directional move is cleanly established.")
    return out


def _market_lines(
    price: Optional[float], macro: str, defender: Dict[str, Any], challenger: Dict[str, Any], battlefield_half: str,
    aurora: Dict[str, str], aurora_transition: str, exec_dir: str, fork: Dict[str, Any], liquidity: Dict[str, Any],
    poi: Optional[Dict[str, Any]], hurdles: List[Dict[str, Any]], odme: Dict[str, Any], posture: str,
) -> List[str]:
    lines: List[str] = []
    if _zone_valid(defender):
        z = f"{_fmt(defender['low'])}–{_fmt(defender['high'])}"
        noun = "support" if defender.get("side") == "DEMAND" else "resistance"
        if defender.get("transition") in {"HELD_AND_REJECTED", "MOVING_AWAY"}:
            bg = "support" if defender.get("side") == "DEMAND" else "resistance"
            lines.append(f"Price has moved away from the major {noun} area at {z}; that earlier location has done its job and now acts as background {bg} for the move rather than a fresh entry location.")
        elif defender.get("condition") == "UNDER_TEST":
            lines.append(f"Price is testing the major {noun} area at {z}. The location is still live, but it is under pressure and deserves caution until the reaction is clearer.")
        elif defender.get("transition") in {"FRESH_CONFIRMED_BREACH", "BREACH_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            lines.append(f"The major {noun} area at {z} has been breached. Any setup that depends on that location should be treated cautiously until price reclaims it or a new market structure forms.")
        elif defender.get("transition") == "ACCEPTED_BEYOND":
            lines.append(f"Price has remained beyond the former {noun} area at {z} on a later confirmed bar; the old location should no longer be treated as intact.")
        elif defender.get("transition") == "RECLAIMED":
            lines.append(f"The major {noun} area at {z} has been reclaimed after a prior breach, improving the location again but not by itself creating an entry.")

    if _zone_valid(challenger):
        z = f"{_fmt(challenger['low'])}–{_fmt(challenger['high'])}"
        noun = "resistance" if challenger.get("side") == "SUPPLY" else "support"
        if challenger.get("condition") == "UNDER_TEST":
            lines.append(f"Price is now inside the broader {noun} area at {z}; this is an active decision zone rather than a clean continuation area.")
        elif challenger.get("transition") in {"FRESH_CONFIRMED_CROSS", "CROSS_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            lines.append(f"Price has just crossed the broader {noun} area at {z}; the first break is not chased until later price action shows acceptance or reclaim.")
        elif challenger.get("transition") == "ACCEPTED_BEYOND":
            lines.append(f"Price has remained beyond the broader {noun} area at {z} on a later confirmed bar, so that barrier is no longer treated as intact.")
        elif challenger.get("transition") in {"REJECTED", "RECLAIMED", "RECLAIMING_INSIDE"}:
            lines.append(f"The broader {noun} area at {z} has rejected the attempt through it and remains relevant.")
        elif battlefield_half == "CHALLENGER_HALF":
            side = "upper" if challenger.get("side") == "SUPPLY" else "lower"
            lines.append(f"Price is trading in the {side} part of the current range, with broader {noun} still ahead at {z}. That makes chasing the move less attractive and increases the importance of the next demand/supply reaction.")

    lines.append(_momentum_sentence(aurora_transition, aurora.get("state", ""), exec_dir))
    lines.extend(_liquidity_commentary(price, liquidity, exec_dir, posture))

    if fork.get("position") == "ABOVE_UPPER_2SD":
        lines.append("Price is structurally extended above its normal path. Fresh longs here would be stretched; the extension can make overhead supply more relevant, but it does not trigger a short by itself.")
    elif fork.get("position") == "BELOW_LOWER_2SD":
        lines.append("Price is structurally extended below its normal path. Fresh shorts here would be stretched; the extension can make lower demand more relevant, but it does not trigger a long by itself.")
    elif fork.get("reclaimed_2sd"):
        lines.append("Price has reclaimed back inside an extreme structural boundary, which improves reversal location if the remaining entry conditions also line up.")

    future = None
    if posture in {"WAIT", "SHORT_ELIGIBLE", "NO_TRADE"}:
        future = next((h for h in hurdles if h.get("side") == "SUPPLY" and (price is None or h.get("low", 0) >= price)), None)
    if future is None and posture in {"WAIT", "LONG_ELIGIBLE", "NO_TRADE"}:
        future = next((h for h in hurdles if h.get("side") == "DEMAND" and (price is None or h.get("high", 0) <= price)), None)
    if future:
        z = f"{_fmt(future['low'])}–{_fmt(future['high'])}"
        side = "supply" if future.get("side") == "SUPPLY" else "demand"
        parent = " inside the broader opposing decision area" if future.get("parent") == "CHALLENGER" else " inside the major supporting decision area" if future.get("parent") == "DEFENDER" else ""
        lines.append(f"A qualified {side} pocket is waiting at {z}{parent}. That is a more precise location to watch for the next reaction than chasing price in between levels.")

    lines.extend(_odme_lines(odme))
    return lines[:12]


def _candidate_levels(base: Dict[str, Any], evidence: Dict[str, Any], direction: str) -> Dict[str, Any]:
    out = dict(_candidate_levels_sb34(base, evidence, direction) or {})
    price = _f(base.get("price")); liq = base.get("liquidity", {}) or {}
    if price is None:
        return out
    magnet = _f(liq.get("above_level")) if direction == "BULLISH" else _f(liq.get("below_level"))
    if direction == "BULLISH" and magnet is not None and magnet > price:
        cur = _f(out.get("target"))
        if cur is None or magnet < cur:
            out["target"] = magnet
    if direction == "BEARISH" and magnet is not None and magnet < price:
        cur = _f(out.get("target"))
        if cur is None or magnet > cur:
            out["target"] = magnet
    risk = _f(out.get("risk")); target = _f(out.get("target"))
    out["reward"] = abs(target - price) if target is not None else None
    out["rr"] = (out["reward"] / risk) if risk and out.get("reward") is not None and risk > 0 else None
    return out


def _adverse_liquidity_threat(base: Dict[str, Any], direction: str, levels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    price = _f(base.get("price")); liq = base.get("liquidity", {}) or {}
    if price is None or direction not in {"BULLISH", "BEARISH"}:
        return {}
    lv = levels or _candidate_levels(base, {}, direction)
    inv = _f(lv.get("invalidation")) if isinstance(lv, dict) else None
    if direction == "BEARISH":
        level = _f(liq.get("above_level"))
        if level is not None and level > price and (inv is None or level < inv):
            return {"side": "ABOVE", "level": level, "reason": f"unfinished upside liquidity around {_fmt(level)} remains inside the short's risk area"}
    else:
        level = _f(liq.get("below_level"))
        if level is not None and level < price and (inv is None or level > inv):
            return {"side": "BELOW", "level": level, "reason": f"unfinished downside liquidity around {_fmt(level)} remains inside the long's risk area"}
    return {}


def _option_liquidity_threat(base: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    price = _f(base.get("price")); liq = base.get("liquidity", {}) or {}; st = _u(plan.get("strategy_type"))
    legs = plan.get("legs", []) or []
    if price is None:
        return {}
    if st == "SHORT_CE":
        strike = next((_f(x.get("strike")) for x in legs if _u(x.get("option")) == "CE"), None)
        above = _f(liq.get("above_level"))
        if strike is not None and above is not None and above > price and above >= strike:
            return {"level": above, "reason": f"unfinished upside liquidity around {_fmt(above)} reaches or extends beyond the proposed sold call strike"}
    if st == "SHORT_PE":
        strike = next((_f(x.get("strike")) for x in legs if _u(x.get("option")) == "PE"), None)
        below = _f(liq.get("below_level"))
        if strike is not None and below is not None and below < price and below <= strike:
            return {"level": below, "reason": f"unfinished downside liquidity around {_fmt(below)} reaches or extends beyond the proposed sold put strike"}
    return {}


def _new_entry_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    plan = dict(_new_entry_plan_sb34(base, evidence) or {})
    if _u(plan.get("kind")) != "NEW":
        return plan
    direction = _u(plan.get("direction")); strategy = _u(plan.get("strategy_type"))
    if strategy.startswith("FUTURES") and direction in {"BULLISH", "BEARISH"}:
        threat = _adverse_liquidity_threat(base, direction, plan)
        if threat:
            return {
                "kind": "NONE", "action": "WAIT", "direction": direction,
                "pullback_low": plan.get("pullback_low"), "pullback_high": plan.get("pullback_high"),
                "reason": threat["reason"] + "; let that liquidity clear or be swept/reclaimed before taking the fresh directional entry",
            }
    if strategy in {"SHORT_CE", "SHORT_PE"}:
        threat = _option_liquidity_threat(base, plan)
        if threat:
            return {"kind": "NONE", "action": "WAIT", "direction": direction, "reason": threat["reason"] + "; the non-arrival entry is deferred until that risk is removed"}
    return plan


def _manage_existing(base: Dict[str, Any], evidence: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan = dict(_manage_existing_sb34(base, evidence, open_trades) or {})
    if _u(plan.get("kind")) != "MANAGE":
        return plan
    direction = _u(plan.get("direction")); action = _u(plan.get("action")); strategy = _u(plan.get("strategy_type"))
    if direction in {"BULLISH", "BEARISH"} and strategy.startswith("FUTURES"):
        levels = _candidate_levels(base, evidence, direction)
        threat = _adverse_liquidity_threat(base, direction, levels)
        if threat and action == "ADD":
            plan.update({"action": "HOLD", "fresh_entry_now": False, "reason": "the existing thesis remains intact, but " + threat["reason"] + "; adding is premature until it clears or is reclaimed"})
        elif threat and action == "HOLD" and plan.get("fresh_entry_now"):
            plan["fresh_entry_now"] = False
            plan["reason"] = _s(plan.get("reason")) + "; however, " + threat["reason"] + ", so a fresh user should wait rather than copy the entry now"
    return plan


def _view_label(base: Dict[str, Any], plan: Dict[str, Any]) -> str:
    kind = _u(plan.get("kind")); action = _u(plan.get("action")); direction = _u(plan.get("direction"))
    if kind == "NEW":
        if direction == "BULLISH": return "LONG"
        if direction == "BEARISH": return "SHORT"
        return "WATCH"
    if kind == "MANAGE":
        if action == "EXIT": return "WATCH"
        if direction == "BULLISH": return "LONG"
        if direction == "BEARISH": return "SHORT"
        return "WATCH"
    if plan.get("reason") or base.get("waiting_for"):
        return "WATCH"
    return "WAIT"




def _sentence_case(text: Any) -> str:
    s = _s(text)
    return s[:1].upper() + s[1:] if s else ""

def _strategy_from_trade_plan(plan: Dict[str, Any]) -> str:
    st = _u(plan.get("strategy_type")); legs = plan.get("legs", []) or []
    if st == "FUTURES_LONG": return "long futures exposure"
    if st == "FUTURES_SHORT": return "short futures exposure"
    if st == "SHORT_CE":
        leg = next((x for x in legs if _u(x.get("option")) == "CE"), {})
        return f"{_fmt(leg.get('strike'))} CE short" if leg else "call short"
    if st == "SHORT_PE":
        leg = next((x for x in legs if _u(x.get("option")) == "PE"), {})
        return f"{_fmt(leg.get('strike'))} PE short" if leg else "put short"
    if st == "SHORT_STRANGLE":
        ce = next((x for x in legs if _u(x.get("option")) == "CE"), {})
        pe = next((x for x in legs if _u(x.get("option")) == "PE"), {})
        if ce and pe:
            return f"{_fmt(ce.get('strike'))} CE / {_fmt(pe.get('strike'))} PE short strangle"
        return "short strangle"
    return st.lower().replace("_", " ") or "exposure"


def _narrative(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> str:
    price = _f(base.get("price"))
    lines = [str(x).strip() for x in (base.get("lines", []) or []) if str(x).strip()]
    odme_prefixes = ("Options positioning", "The options map", "For non-arrival risk", "Options premium behaviour")
    market = [x for x in lines if not x.startswith(odme_prefixes)]
    option_lines = [x for x in lines if x.startswith(odme_prefixes)]

    momentum = [x for x in market if x.startswith("Short-term")]
    liquidity = [x for x in market if "liquidity" in x.lower()]
    stretch = [x for x in market if "structurally extended" in x.lower() or "extreme structural boundary" in x.lower()]
    pocket = [x for x in market if x.startswith("A qualified")]
    used = set(momentum + liquidity + stretch + pocket)
    location = [x for x in market if x not in used]

    paragraphs: List[str] = []
    if location:
        paragraphs.append(" ".join(location[:2]))
    if momentum:
        paragraphs.append(" ".join(momentum[:1]))
    if liquidity:
        paragraphs.append(" ".join(liquidity[:2]))
    if stretch:
        paragraphs.append(" ".join(stretch[:1]))

    odme = evidence.get("odme", {}) or {}
    if _s(evidence.get("mode")) == "TV + ODME" and odme:
        if not option_lines:
            option_lines = _odme_lines(odme)
        pa = _s(odme.get("premium_alert"))
        if pa:
            pa = pa.replace("Premium alert:", "Options premium behaviour:").strip()
            if pa not in option_lines:
                option_lines.append(pa)
        if option_lines:
            paragraphs.append(" ".join(option_lines[:3]))

    kind = _u(plan.get("kind"))
    if kind == "NEW":
        direction = _u(plan.get("direction")); action_text = _strategy_text(plan)
        action = f"**SuperBrain is taking this exposure now: {action_text} at spot {_fmt(price)}.**"
        if plan.get("late"):
            action += " The cleaner entry developed slightly earlier, but the setup remains valid at the current scan price; this is late-but-still-valid rather than a chase."
        else:
            action += " The current scan satisfies the entry conditions."
        if plan.get("reason"):
            action += " " + _sentence_case(plan.get("reason")) + "."
        paragraphs.append(action)

        next_bits: List[str] = []
        if plan.get("target") is not None:
            next_bits.append(f"The first objective is around {_fmt(plan.get('target'))}.")
        if plan.get("invalidation") is not None:
            next_bits.append(f"The thesis is invalidated around {_fmt(plan.get('invalidation'))} or by confirmed acceptance beyond the relevant decision area.")
        if plan.get("expected_eta"):
            next_bits.append(_s(plan.get("expected_eta")))
        if plan.get("pullback_low") is not None and plan.get("pullback_high") is not None and direction in {"BULLISH", "BEARISH"}:
            next_bits.append(f"For an add or better re-entry, watch approximately {_fmt(plan.get('pullback_low'))}–{_fmt(plan.get('pullback_high'))}; use it only if the area still holds, short-term pressure remains aligned and momentum is {_reentry_phrase(direction)}.")
        next_bits.append("Future scans will manage this same SuperBrain exposure as HOLD, ADD, REDUCE or EXIT rather than treating it as a new trade.")
        paragraphs.append(" ".join(next_bits))

    elif kind == "MANAGE":
        action = _u(plan.get("action")); direction = _u(plan.get("direction")); strategy = _u(plan.get("strategy_type"))
        entry = _f(plan.get("entry_reference")); strat_text = _strategy_from_trade_plan(plan)
        if action == "HOLD":
            msg = f"**SuperBrain is holding the existing {strat_text}"
            if entry is not None: msg += f" from spot {_fmt(entry)}"
            msg += ".** " + _sentence_case(plan.get("reason")) + "."
        elif action == "ADD":
            msg = f"**SuperBrain is adding to the existing {strat_text} now at spot {_fmt(price)}.** " + _sentence_case(plan.get("reason")) + "."
        elif action == "REDUCE":
            msg = f"**SuperBrain is reducing the existing {strat_text} now.** " + _sentence_case(plan.get("reason")) + "."
        elif action == "EXIT":
            msg = f"**SuperBrain is exiting the existing {strat_text} now at spot {_fmt(price)}.** " + _sentence_case(plan.get("reason")) + "."
        else:
            msg = f"**SuperBrain action: {action or 'WATCH'}.** " + _sentence_case(plan.get("reason")) + "."
        paragraphs.append(msg)

        follow: List[str] = []
        if action not in {"EXIT"}:
            if plan.get("fresh_entry_now"):
                follow.append("A new user can still enter on the present scan at the current market level, using the same thesis and invalidation discipline rather than assuming the older entry price.")
            elif direction in {"BULLISH", "BEARISH"} and strategy.startswith("FUTURES"):
                lv = _candidate_levels(base, evidence, direction)
                if lv.get("pullback_low") is not None and lv.get("pullback_high") is not None:
                    follow.append(f"For a new user, do not chase the existing exposure; wait for a pullback toward {_fmt(lv.get('pullback_low'))}–{_fmt(lv.get('pullback_high'))} and use it only if pressure remains aligned and momentum is {_reentry_phrase(direction)}.")
            elif strategy == "SHORT_CE":
                follow.append("For a fresh call sale, wait until call-side protection remains intact and price is not accelerating upward toward the sold strike; rising expansion risk or weakening call-side protection is a reason to stand aside.")
            elif strategy == "SHORT_PE":
                follow.append("For a fresh put sale, wait until put-side protection remains intact and price is not accelerating downward toward the sold strike; rising expansion risk or weakening put-side protection is a reason to stand aside.")
            elif strategy == "SHORT_STRANGLE":
                follow.append("For a fresh strangle, range containment must still dominate expansion risk and neither side should show active directional acceleration toward the sold strikes.")
        else:
            follow.append("No immediate re-entry is assumed after the exit; wait for a fresh qualifying location, continuation setup or renewed non-arrival condition on a later scan.")

        if strategy == "SHORT_CE" and action not in {"EXIT"}:
            follow.append("Continue to HOLD only while the sold call remains protected; REDUCE if call-side protection shifts higher or expansion risk builds, and EXIT if spot reaches the sold strike or bullish pressure and momentum become actively adverse.")
        elif strategy == "SHORT_PE" and action not in {"EXIT"}:
            follow.append("Continue to HOLD only while the sold put remains protected; REDUCE if put-side protection shifts lower or expansion risk builds, and EXIT if spot reaches the sold strike or bearish pressure and momentum become actively adverse.")
        elif strategy == "SHORT_STRANGLE" and action not in {"EXIT"}:
            follow.append("Continue to HOLD while range containment remains intact; EXIT if options positioning shifts decisively toward expansion or either side's protection fails.")
        elif direction in {"BULLISH", "BEARISH"} and action not in {"EXIT"}:
            inv = _f(plan.get("invalidation"))
            if inv is not None:
                follow.append(f"The existing directional thesis remains invalidated around {_fmt(inv)}; additions are allowed only on a qualified pullback with pressure and momentum still aligned.")
        if follow:
            paragraphs.append(" ".join(follow))

    else:
        action = "**No SuperBrain exposure is taken on this scan.**"
        if plan.get("reason"):
            action += " " + _sentence_case(plan.get("reason")) + "."
        paragraphs.append(action)
        watch: List[str] = []
        if pocket:
            watch.extend(pocket[:1])
        waits = [str(x).strip() for x in (base.get("waiting_for", []) or []) if str(x).strip()]
        watch.extend(waits[:2])
        if base.get("view_change"):
            watch.append(_s(base.get("view_change")))
        if plan.get("pullback_low") is not None and plan.get("pullback_high") is not None and _u(plan.get("direction")) in {"BULLISH", "BEARISH"}:
            watch.append(f"A better entry is around {_fmt(plan.get('pullback_low'))}–{_fmt(plan.get('pullback_high'))} only if short-term pressure remains aligned and momentum is {_reentry_phrase(_u(plan.get('direction')))}.")
        if watch:
            paragraphs.append(" ".join(watch))

    return "\n\n".join(x for x in paragraphs if x).strip()


def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    base = _analyze_market_sb32(evidence, previous_evidence, open_trades=open_trades)
    trades = open_trades or []
    plan = _manage_existing(base, evidence, trades) if trades else _new_entry_plan(base, evidence)
    if not plan:
        plan = {"kind": "NONE", "action": "WAIT"}
    if _u(plan.get("kind")) == "MANAGE":
        tid = _s(plan.get("trade_id"))
        t = next((dict(x) for x in trades if _s(x.get("trade_id")) == tid), dict(trades[0]) if trades else {})
        plan.setdefault("entry_reference", _f(t.get("entry_reference")))
        plan.setdefault("target", _f(t.get("target")))
        plan.setdefault("invalidation", _f(t.get("invalidation")))
        plan.setdefault("legs", _json_list(t.get("legs_json")))
    base["reasoner_version"] = REASONER_VERSION
    base["trade_plan"] = plan
    base["view_label"] = _view_label(base, plan)
    base["narrative"] = _narrative(base, evidence, plan)
    return base

# =============================================================================
# SB3.6 final context polish overrides
# =============================================================================
# Adds nuanced liquidity timing, battlefield-advance awareness and compact
# no-change signaling. Core locked direction/location rules above remain authority.

REASONER_VERSION = "SB3.6_FINAL_CONTEXT_LIQUIDITY_WATCH"
_new_entry_plan_sb35 = _new_entry_plan
_manage_existing_sb35 = _manage_existing
_narrative_sb35 = _narrative


def _round_level(v: Any) -> Optional[float]:
    n = _f(v)
    return round(n, 4) if n is not None else None


def _battlefield_progression(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    current = _source_map(evidence)
    previous = _prev_source_map(previous_evidence or {})
    edge = current.get("EDGE", {}) or {}
    prev_edge = previous.get("EDGE", {}) or {}
    price = _f(base.get("price"))
    prev_price = _f(prev_edge.get("close"))
    macro = _u(base.get("macro"))
    cur_ch = _zone(edge, "CHALLENGER")
    old_ch = _zone(prev_edge, "CHALLENGER")
    cur_def = _zone(edge, "DEFENDER")
    old_def = _zone(prev_edge, "DEFENDER")
    edge_bar = _i(edge.get("bar_time")); prev_bar = _i(prev_edge.get("bar_time"))
    new_bar = edge_bar is not None and prev_bar is not None and edge_bar > prev_bar

    out: Dict[str, Any] = {
        "state": "UNCHANGED",
        "direction": macro,
        "supports_continuation": False,
        "old_challenger": old_ch,
        "new_challenger": cur_ch,
        "old_defender": old_def,
        "new_defender": cur_def,
        "opposing_fp_cleared": False,
        "detail": "",
    }

    # Same opposing zone accepted through on a later confirmed bar.
    ch = base.get("challenger", {}) or {}
    if ch.get("transition") == "ACCEPTED_BEYOND" and macro in {"BULLISH", "BEARISH"}:
        out.update({
            "state": "OPPOSING_BARRIER_ACCEPTED",
            "supports_continuation": True,
            "detail": "the previous opposing decision area has been accepted through on a later confirmed bar",
        })

    # Role migration after the prior opposing zone was crossed/cleared. This is
    # the important 'battlefield advanced' state: the next hurdle has shifted in
    # the same direction, so distance from the old Defender is not a standalone
    # reason to reject continuation.
    if new_bar and _zone_valid(cur_ch) and _zone_valid(old_ch) and not _same_role_lineage(cur_ch, old_ch):
        old_now = _zone_relation(old_ch, price)
        old_prev = _zone_relation(old_ch, prev_price)
        if macro == "BULLISH" and cur_ch.get("side") == "SUPPLY" and old_ch.get("side") == "SUPPLY":
            shifted = (_f(cur_ch.get("low")) or 0) > (_f(old_ch.get("low")) or 0)
            cleared = old_now == "DISTAL_SIDE" or old_prev == "DISTAL_SIDE"
            if shifted and cleared:
                out.update({
                    "state": "BATTLEFIELD_ADVANCED",
                    "supports_continuation": True,
                    "detail": "the previous resistance has been cleared and the next broader resistance has migrated higher",
                })
        elif macro == "BEARISH" and cur_ch.get("side") == "DEMAND" and old_ch.get("side") == "DEMAND":
            shifted = (_f(cur_ch.get("high")) or float("inf")) < (_f(old_ch.get("high")) or float("inf"))
            cleared = old_now == "DISTAL_SIDE" or old_prev == "DISTAL_SIDE"
            if shifted and cleared:
                out.update({
                    "state": "BATTLEFIELD_ADVANCED",
                    "supports_continuation": True,
                    "detail": "the previous support has been cleared and the next broader support has migrated lower",
                })

    # A recently run opposing footprint is also useful continuation context. It
    # does not create direction by itself; it only tells SuperBrain that a nearer
    # hurdle has been removed when pressure/momentum already agree.
    if price is not None and macro == "BULLISH":
        status = _u(edge.get("fp_s_status") or edge.get("fp_s_context_state"))
        event = _u(edge.get("fp_s_event_type"))
        hi = _f(edge.get("fp_s_high"))
        if hi is not None and price > hi and (status == "RUN" or "RUN" in event):
            out["opposing_fp_cleared"] = True
            if out["state"] == "UNCHANGED":
                out.update({"state": "NEARER_HURDLE_CLEARED", "supports_continuation": True,
                            "detail": "a recent supply pocket has been run and removed as a nearer upside hurdle"})
    elif price is not None and macro == "BEARISH":
        status = _u(edge.get("fp_d_status") or edge.get("fp_d_context_state"))
        event = _u(edge.get("fp_d_event_type"))
        lo = _f(edge.get("fp_d_low"))
        if lo is not None and price < lo and (status == "RUN" or "RUN" in event):
            out["opposing_fp_cleared"] = True
            if out["state"] == "UNCHANGED":
                out.update({"state": "NEARER_HURDLE_CLEARED", "supports_continuation": True,
                            "detail": "a recent demand pocket has been run and removed as a nearer downside hurdle"})

    # Defender failure remains adverse to the old macro. It is context only and
    # never authorizes an opposite trade without the normal locked conditions.
    d = base.get("defender", {}) or {}
    if d.get("condition") == "BREACHED" or d.get("transition") in {"FRESH_CONFIRMED_BREACH", "ACCEPTED_BEYOND"}:
        out["defender_failed"] = True
    else:
        out["defender_failed"] = False
    return out


def _progression_sentence(p: Dict[str, Any]) -> str:
    state = _u(p.get("state")); direction = _u(p.get("direction"))
    old = p.get("old_challenger", {}) or {}; new = p.get("new_challenger", {}) or {}
    if state == "BATTLEFIELD_ADVANCED":
        if direction == "BULLISH":
            oldz = f"{_fmt(old.get('low'))}–{_fmt(old.get('high'))}" if _zone_valid(old) else "the prior resistance"
            newz = f"{_fmt(new.get('low'))}–{_fmt(new.get('high'))}" if _zone_valid(new) else "a higher resistance area"
            return f"The prior resistance at {oldz} has been cleared and the next broader resistance has shifted higher to {newz}. The location context has advanced with the market, so distance from the older support area is not by itself a reason to reject a continuation long."
        if direction == "BEARISH":
            oldz = f"{_fmt(old.get('low'))}–{_fmt(old.get('high'))}" if _zone_valid(old) else "the prior support"
            newz = f"{_fmt(new.get('low'))}–{_fmt(new.get('high'))}" if _zone_valid(new) else "a lower support area"
            return f"The prior support at {oldz} has been cleared and the next broader support has shifted lower to {newz}. The location context has advanced with the market, so distance from the older resistance area is not by itself a reason to reject a continuation short."
    if state == "OPPOSING_BARRIER_ACCEPTED":
        if direction == "BULLISH":
            return "The previous resistance has been accepted through on a later confirmed bar. That hurdle is now cleared, which improves continuation context if buying pressure and momentum remain aligned."
        if direction == "BEARISH":
            return "The previous support has been accepted through on a later confirmed bar. That hurdle is now cleared, which improves continuation context if selling pressure and momentum remain aligned."
    if state == "NEARER_HURDLE_CLEARED":
        return _sentence_case(p.get("detail")) + "."
    return ""


def _liquidity_decision(base: Dict[str, Any], direction: str, levels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    direction = _u(direction)
    if direction not in {"BULLISH", "BEARISH"}:
        return {"has_threat": False, "wait_required": False}
    liq = base.get("liquidity", {}) or {}; price = _f(base.get("price"))
    if price is None:
        return {"has_threat": False, "wait_required": False}
    levels = levels or _candidate_levels(base, {}, direction)
    inv = _f(levels.get("invalidation")) if isinstance(levels, dict) else None
    if direction == "BEARISH":
        adverse = _f(liq.get("above_level")); count = _i(liq.get("above_count")) or 0
        favorable = _f(liq.get("below_level")); favorable_count = _i(liq.get("below_count")) or 0
        inside_risk = adverse is not None and adverse > price and (inv is None or adverse < inv)
    else:
        adverse = _f(liq.get("below_level")); count = _i(liq.get("below_count")) or 0
        favorable = _f(liq.get("above_level")); favorable_count = _i(liq.get("above_count")) or 0
        inside_risk = adverse is not None and adverse < price and (inv is None or adverse > inv)

    poi = base.get("poi", {}) or {}; exec_dir = _u(base.get("exec_of"))
    qualified_reversal = bool(poi.get("qualified") and _u(poi.get("intended")) == direction and exec_dir == _opposite(direction))
    escaped = False
    if qualified_reversal and price is not None:
        if direction == "BEARISH" and _f(poi.get("low")) is not None:
            escaped = price < _f(poi.get("low"))
        elif direction == "BULLISH" and _f(poi.get("high")) is not None:
            escaped = price > _f(poi.get("high"))
    active, early = _momentum_supports(direction, base.get("aurora", {}) or {})
    aligned_continuation = exec_dir == direction and active
    meaningful = count >= 2
    wait_required = bool(inside_risk and meaningful and qualified_reversal and not escaped)
    return {
        "has_threat": bool(inside_risk),
        "adverse_level": adverse,
        "adverse_count": count,
        "favorable_level": favorable,
        "favorable_count": favorable_count,
        "meaningful": meaningful,
        "inside_risk": bool(inside_risk),
        "qualified_reversal": qualified_reversal,
        "escaped": escaped,
        "aligned_continuation": aligned_continuation,
        "momentum_active": active,
        "momentum_early": early,
        "wait_required": wait_required,
    }


def _liquidity_action_paragraph(base: Dict[str, Any], plan: Dict[str, Any]) -> str:
    liq = base.get("liquidity", {}) or {}; price = _f(base.get("price"))
    direction = _u(plan.get("direction"))
    if direction not in {"BULLISH", "BEARISH"}:
        direction = _u(base.get("exec_of")) if _u(base.get("exec_of")) in {"BULLISH", "BEARISH"} else ""
    levels = plan if plan else _candidate_levels(base, {}, direction) if direction else {}
    dec = _liquidity_decision(base, direction, levels) if direction else {"has_threat": False}
    parts: List[str] = []

    event = _u(liq.get("event")); lvl = _f(liq.get("level"))
    if event and lvl is not None:
        if event.startswith("BUY_") and event.endswith("_RUN"):
            parts.append(f"Recent upside liquidity around {_fmt(lvl)} was run and price held above it.")
        elif event.startswith("BUY_") and event.endswith("_SWEEP"):
            parts.append(f"Recent upside liquidity around {_fmt(lvl)} was swept and reclaimed below, which is constructive for a short if the rest of the setup agrees.")
        elif event.startswith("SELL_") and event.endswith("_RUN"):
            parts.append(f"Recent downside liquidity around {_fmt(lvl)} was run and price held below it.")
        elif event.startswith("SELL_") and event.endswith("_SWEEP"):
            parts.append(f"Recent downside liquidity around {_fmt(lvl)} was swept and reclaimed above, which is constructive for a long if the rest of the setup agrees.")

    if direction == "BEARISH":
        adv = dec.get("adverse_level"); fav = dec.get("favorable_level")
        if dec.get("wait_required"):
            parts.append(f"Unfinished upside liquidity around {_fmt(adv)} is a material threat to this early reversal short, so SuperBrain is waiting for that pool to be swept/reclaimed or for price to escape the supply reaction more decisively before entering.")
        elif dec.get("has_threat"):
            if dec.get("aligned_continuation"):
                parts.append(f"Unfinished upside liquidity around {_fmt(adv)} is still a short-term threat, but selling pressure and active bearish momentum are already aligned; SuperBrain does not require that pool to clear before taking the continuation short.")
            else:
                parts.append(f"Unfinished upside liquidity around {_fmt(adv)} remains the main near-term risk to a short. It is not an automatic veto, but with timing not fully aligned SuperBrain gives it meaningful weight before entry.")
        elif adv is not None and price is not None and adv > price:
            parts.append(f"Liquidity above around {_fmt(adv)} remains unfinished, but SuperBrain does not consider it a material threat to the current short thesis at this location.")
        if fav is not None and price is not None and fav < price:
            parts.append(f"Liquidity below around {_fmt(fav)} is a useful downside magnet if bearish pressure continues.")
    elif direction == "BULLISH":
        adv = dec.get("adverse_level"); fav = dec.get("favorable_level")
        if dec.get("wait_required"):
            parts.append(f"Unfinished downside liquidity around {_fmt(adv)} is a material threat to this early reversal long, so SuperBrain is waiting for that pool to be swept/reclaimed or for price to escape the demand reaction more decisively before entering.")
        elif dec.get("has_threat"):
            if dec.get("aligned_continuation"):
                parts.append(f"Unfinished downside liquidity around {_fmt(adv)} is still a short-term threat, but buying pressure and active bullish momentum are already aligned; SuperBrain does not require that pool to clear before taking the continuation long.")
            else:
                parts.append(f"Unfinished downside liquidity around {_fmt(adv)} remains the main near-term risk to a long. It is not an automatic veto, but with timing not fully aligned SuperBrain gives it meaningful weight before entry.")
        elif adv is not None and price is not None and adv < price:
            parts.append(f"Liquidity below around {_fmt(adv)} remains unfinished, but SuperBrain does not consider it a material threat to the current long thesis at this location.")
        if fav is not None and price is not None and fav > price:
            parts.append(f"Liquidity above around {_fmt(fav)} is a useful upside magnet if bullish pressure continues.")
    else:
        above = _f(liq.get("above_level")); below = _f(liq.get("below_level"))
        if above is not None and price is not None and above > price:
            parts.append(f"Liquidity remains above around {_fmt(above)}.")
        if below is not None and price is not None and below < price:
            parts.append(f"Liquidity remains below around {_fmt(below)}.")
    return " ".join(parts).strip()


def _view_change_line(challenger: Dict[str, Any], defender: Dict[str, Any], hurdles: List[Dict[str, Any]], posture: str) -> str:
    if posture == "WAIT":
        supply = next((h for h in hurdles if h.get("side") == "SUPPLY"), None)
        if supply and _zone_valid(challenger) and challenger.get("side") == "SUPPLY":
            return f"If price pushes through {_fmt(challenger.get('high'))} and then remains accepted above that broader resistance on a later confirmed bar, the current short-location idea should be abandoned."
        demand = next((h for h in hurdles if h.get("side") == "DEMAND"), None)
        if demand and _zone_valid(challenger) and challenger.get("side") == "DEMAND":
            return f"If price pushes through {_fmt(challenger.get('low'))} and then remains accepted below that broader support on a later confirmed bar, the current long-location idea should be abandoned."
    if _zone_valid(defender) and defender.get("condition") == "BREACHED":
        return "The earlier supporting location has failed; do not rely on the old market structure until it is reclaimed or replaced by a new valid structure."
    return ""


def _new_entry_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    # Start with SB3.4 trade selection, not SB3.5's universal liquidity veto.
    plan = dict(_new_entry_plan_sb34(base, evidence) or {})
    if _u(plan.get("kind")) != "NEW":
        return plan
    direction = _u(plan.get("direction")); strategy = _u(plan.get("strategy_type"))
    if strategy.startswith("FUTURES") and direction in {"BULLISH", "BEARISH"}:
        dec = _liquidity_decision(base, direction, plan)
        plan["liquidity_context"] = dec
        if dec.get("wait_required"):
            return {
                "kind": "NONE", "action": "WAIT", "direction": direction,
                "pullback_low": plan.get("pullback_low"), "pullback_high": plan.get("pullback_high"),
                "reason": "a meaningful adverse liquidity pool remains unresolved during an early reversal attempt",
                "liquidity_context": dec,
            }
    if strategy in {"SHORT_CE", "SHORT_PE"}:
        threat = _option_liquidity_threat(base, plan)
        if threat:
            return {"kind": "NONE", "action": "WAIT", "direction": direction,
                    "reason": "the proposed non-arrival strike is exposed to unresolved liquidity reaching that strike",
                    "liquidity_context": threat}
    return plan


def _manage_existing(base: Dict[str, Any], evidence: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan = dict(_manage_existing_sb34(base, evidence, open_trades) or {})
    if _u(plan.get("kind")) != "MANAGE":
        return plan
    direction = _u(plan.get("direction")); action = _u(plan.get("action")); strategy = _u(plan.get("strategy_type"))
    if direction in {"BULLISH", "BEARISH"} and strategy.startswith("FUTURES"):
        levels = _candidate_levels(base, evidence, direction)
        dec = _liquidity_decision(base, direction, levels)
        plan["liquidity_context"] = dec
        if dec.get("wait_required") and action == "ADD":
            plan.update({"action": "HOLD", "fresh_entry_now": False,
                         "reason": "the existing thesis remains intact, but a meaningful adverse liquidity pool is unresolved during the attempted add"})
        elif dec.get("wait_required") and action == "HOLD" and plan.get("fresh_entry_now"):
            plan["fresh_entry_now"] = False
            plan["reason"] = _s(plan.get("reason")) + "; a fresh user should wait because the adverse liquidity risk is still material at this reversal location"
    return plan


def _material_signature(base: Dict[str, Any], plan: Dict[str, Any], progression: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    def zsig(z: Dict[str, Any]) -> Dict[str, Any]:
        z = z or {}
        return {"side": _u(z.get("side")), "low": _round_level(z.get("low")), "high": _round_level(z.get("high")),
                "condition": _u(z.get("condition")), "transition": _u(z.get("transition"))}
    poi = base.get("poi", {}) or {}; liq = base.get("liquidity", {}) or {}; aur = base.get("aurora", {}) or {}; fork = base.get("fork", {}) or {}
    odme = evidence.get("odme", {}) or {}
    rs = _f(odme.get("range_score")); es = _f(odme.get("expansion_score"))
    vol = "EXPANSION" if rs is not None and es is not None and es > rs else "RANGE" if rs is not None and es is not None and rs > es else "BALANCED"
    hurdles = []
    for h in (base.get("hurdles", []) or [])[:4]:
        hurdles.append({"side": _u(h.get("side")), "low": _round_level(h.get("low")), "high": _round_level(h.get("high")), "zone_id": _s(h.get("zone_id"))})
    return {
        "posture": _u(base.get("posture")), "exec": _u(base.get("exec_of")), "macro": _u(base.get("macro")),
        "momentum_state": _u(aur.get("state")), "momentum_transition": _u(aur.get("transition")),
        "defender": zsig(base.get("defender", {}) or {}), "challenger": zsig(base.get("challenger", {}) or {}),
        "progression": _u(progression.get("state")),
        "poi": {"qualified": bool(poi.get("qualified")), "side": _u(poi.get("side")), "low": _round_level(poi.get("low")), "high": _round_level(poi.get("high")), "parent": _u(poi.get("parent"))},
        "fork": {"position": _u(fork.get("position")), "reclaimed": bool(fork.get("reclaimed_2sd"))},
        "liquidity": {"event": _u(liq.get("event")), "confirm_time": _i(liq.get("confirm_time")), "above": _round_level(liq.get("above_level")), "above_count": _i(liq.get("above_count")), "below": _round_level(liq.get("below_level")), "below_count": _i(liq.get("below_count"))},
        "hurdles": hurdles,
        "odme": {"tilt": _u(odme.get("odme_tilt")), "ce_wall": _round_level(odme.get("active_ce_wall") or odme.get("ce_wall")), "pe_wall": _round_level(odme.get("active_pe_wall") or odme.get("pe_wall")), "safe_ce": _round_level(odme.get("safer_sell_ce")), "safe_pe": _round_level(odme.get("safer_sell_pe")), "vol": vol, "ce_sellable": _sale_action_ok(odme, "CE") if odme else False, "pe_sellable": _sale_action_ok(odme, "PE") if odme else False, "premium_alert": _s(odme.get("premium_alert"))[:180]},
        "plan": {"kind": _u(plan.get("kind")), "action": _u(plan.get("action")), "direction": _u(plan.get("direction")), "strategy": _u(plan.get("strategy_type")), "target": _round_level(plan.get("target")), "invalidation": _round_level(plan.get("invalidation"))},
    }


def _narrative(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> str:
    # Start from the polished SB3.5 paragraph structure, then replace generic
    # liquidity wording with a plan-aware verdict and add battlefield progression.
    text = _narrative_sb35(base, evidence, plan)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    filtered: List[str] = []
    for p in paras:
        low = p.lower()
        if "liquidity" in low and ("unfinished" in low or "recent upside" in low or "recent downside" in low or "natural" in low or "magnet" in low):
            continue
        if (base.get("battlefield_progression", {}) or {}).get("supports_continuation") and "that makes chasing the move less attractive" in low:
            # This generic late-location sentence is superseded by the explicit
            # battlefield-advance interpretation below.
            continue
        filtered.append(p)

    prog = _progression_sentence(base.get("battlefield_progression", {}) or {})
    liq = _liquidity_action_paragraph(base, plan)
    insert_at = 1 if filtered else 0
    if prog:
        filtered.insert(insert_at, prog); insert_at += 1
    if liq:
        filtered.insert(insert_at, liq)
    return "\n\n".join(filtered).strip()


def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    base = _analyze_market_sb32(evidence, previous_evidence, open_trades=open_trades)
    progression = _battlefield_progression(evidence, previous_evidence, base)
    base["battlefield_progression"] = progression
    trades = open_trades or []
    plan = _manage_existing(base, evidence, trades) if trades else _new_entry_plan(base, evidence)
    if not plan:
        plan = {"kind": "NONE", "action": "WAIT"}
    if _u(plan.get("kind")) == "MANAGE":
        tid = _s(plan.get("trade_id"))
        t = next((dict(x) for x in trades if _s(x.get("trade_id")) == tid), dict(trades[0]) if trades else {})
        plan.setdefault("entry_reference", _f(t.get("entry_reference")))
        plan.setdefault("target", _f(t.get("target")))
        plan.setdefault("invalidation", _f(t.get("invalidation")))
        plan.setdefault("legs", _json_list(t.get("legs_json")))

    base["reasoner_version"] = REASONER_VERSION
    base["trade_plan"] = plan
    core_view = _view_label(base, plan)
    sig = _material_signature(base, plan, progression, evidence)
    prev_analysis = previous_evidence.get("analysis", {}) if isinstance(previous_evidence, dict) and isinstance(previous_evidence.get("analysis"), dict) else {}
    prev_sig = prev_analysis.get("material_signature") if isinstance(prev_analysis, dict) else None
    no_change = bool(prev_sig and prev_sig == sig)
    base["material_signature"] = sig
    base["no_material_change"] = no_change
    base["core_view_label"] = core_view
    base["view_label"] = "WATCH (no change from last scan)" if no_change else core_view
    base["narrative"] = _narrative(base, evidence, plan)
    return base

# Keep the underlying STATE/thesis wording consistent with the visible liquidity
# treatment as well; the final narrative adds the plan-specific judgment.
def _liquidity_commentary(price: Optional[float], liquidity: Dict[str, Any], exec_dir: str, posture: str) -> List[str]:
    out: List[str] = []
    event = _u(liquidity.get("event")); lvl = _f(liquidity.get("level"))
    if event and lvl is not None:
        if event.startswith("BUY_") and event.endswith("_RUN"):
            out.append(f"Recent upside liquidity around {_fmt(lvl)} was run and price held above it.")
        elif event.startswith("BUY_") and event.endswith("_SWEEP"):
            out.append(f"Recent upside liquidity around {_fmt(lvl)} was swept and reclaimed below.")
        elif event.startswith("SELL_") and event.endswith("_RUN"):
            out.append(f"Recent downside liquidity around {_fmt(lvl)} was run and price held below it.")
        elif event.startswith("SELL_") and event.endswith("_SWEEP"):
            out.append(f"Recent downside liquidity around {_fmt(lvl)} was swept and reclaimed above.")
    above = _f(liquidity.get("above_level")); below = _f(liquidity.get("below_level"))
    short_context = posture == "SHORT_ELIGIBLE" or _u(exec_dir) == "BEARISH"
    long_context = posture == "LONG_ELIGIBLE" or _u(exec_dir) == "BULLISH"
    if short_context:
        if above is not None and price is not None and above > price:
            out.append(f"Unfinished liquidity above around {_fmt(above)} is a near-term risk to a short, but it is not an automatic entry veto.")
        if below is not None and price is not None and below < price:
            out.append(f"Liquidity below around {_fmt(below)} is a useful downside magnet if bearish pressure continues.")
    elif long_context:
        if below is not None and price is not None and below < price:
            out.append(f"Unfinished liquidity below around {_fmt(below)} is a near-term risk to a long, but it is not an automatic entry veto.")
        if above is not None and price is not None and above > price:
            out.append(f"Liquidity above around {_fmt(above)} is a useful upside magnet if bullish pressure continues.")
    return out

# ============================================================================
# SB3.7 campaign / expression manager overrides
# ============================================================================
REASONER_VERSION = "SB3.7_CAMPAIGN_EXPRESSION_MANAGER"

_new_entry_plan_sb36 = _new_entry_plan
_manage_existing_sb36 = _manage_existing
_narrative_sb36 = _narrative
_material_signature_sb36 = _material_signature
_analyze_market_sb36 = analyze_market


def _campaign_meta(trade: Dict[str, Any]) -> Dict[str, Any]:
    return _json_obj(trade.get("metadata_json"))


def _active_campaign_legs(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    legs = _json_list(trade.get("legs_json"))
    strategy = _u(trade.get("strategy_type"))
    if not legs and strategy in {"FUTURES_LONG", "FUTURES_SHORT"}:
        legs = [{
            "side": "BUY" if strategy == "FUTURES_LONG" else "SELL",
            "instrument_type": "FUTURES", "contract": "FUTURES",
            "role": "PRIMARY", "status": "ACTIVE",
        }]
    return [dict(x) for x in legs if _u(x.get("status") or "ACTIVE") != "CLOSED"]


def _future_leg(direction: str) -> Dict[str, Any]:
    return {
        "side": "BUY" if direction == "BULLISH" else "SELL",
        "instrument_type": "FUTURES", "contract": "FUTURES",
    }


def _atm_option_leg(evidence: Dict[str, Any], direction: str) -> Dict[str, Any]:
    odme = evidence.get("odme", {}) or {}
    strike = _f(odme.get("atm_strike"))
    option = "CE" if direction == "BULLISH" else "PE"
    premium = _f(odme.get("atm_ce_ltp" if option == "CE" else "atm_pe_ltp"))
    if strike is None or premium is None or premium <= 0:
        return {}
    leg = {
        "side": "BUY", "instrument_type": "OPTION", "option": option,
        "strike": strike, "premium": premium,
    }
    expiry = _s(odme.get("expiry"))
    if expiry:
        leg["expiry"] = expiry
    return leg


def _premium_buy_class(odme: Dict[str, Any]) -> str:
    text = " ".join([_s(odme.get("premium_alert")), _s(odme.get("commentary"))]).lower()
    if any(x in text for x in ["very expensive", "too expensive", "avoid option buy", "avoid buying", "premium rich", "overpriced"]):
        return "BLOCKED"
    if any(x in text for x in ["cheap option", "cheap premium", "normal option", "normal premium", "defined-risk option", "premium favorable", "premium favourable"]):
        return "FAVOURABLE"
    if any(x in text for x in ["expensive option", "expensive premium", "premium elevated"]):
        return "EXPENSIVE"
    return "UNKNOWN"


def _directional_expression(base: Dict[str, Any], evidence: Dict[str, Any], direction: str, has_location: bool = False) -> Dict[str, Any]:
    """Select vehicle after the directional thesis is already established."""
    fallback = "FUTURES_LONG" if direction == "BULLISH" else "FUTURES_SHORT"
    result = {
        "strategy_type": fallback,
        "legs": [_future_leg(direction)],
        "expression_style": "DIRECTIONAL",
        "expression_reason": "direct futures exposure is the cleanest available vehicle for the qualified directional thesis",
    }
    if _s(evidence.get("mode")) != "TV + ODME":
        return result
    odme = evidence.get("odme", {}) or {}
    if not odme:
        return result

    rs = _f(odme.get("range_score")) or 0.0
    es = _f(odme.get("expansion_score")) or 0.0
    exec_dir = _u(base.get("exec_of"))
    aur_state = _u((base.get("aurora", {}) or {}).get("state"))
    active_momentum = (direction == "BULLISH" and aur_state == "GREEN") or (direction == "BEARISH" and aur_state == "RED")
    eta = _eta_context(evidence, direction, _f(base.get("price")))
    path = _u(eta.get("path"))
    fast_path = any(x in path for x in ["SHARP", "FAST", "CLEAN", "DIRECT"])
    premium_class = _premium_buy_class(odme)

    # Directional but contained / slower: express the thesis as non-arrival on
    # the opposite option side, only when ODME explicitly permits that sale.
    if rs >= es + 5.0:
        opt = _odme_option_strategy(base, evidence, direction, has_location)
        side = "PE" if direction == "BULLISH" else "CE"
        if _u(opt.get("strategy_type")) and _sale_action_ok(odme, side):
            opt["expression_style"] = "NONARRIVAL"
            opt["expression_reason"] = (
                f"the {direction.lower()} thesis is intact but the option map is range/non-arrival dominant, "
                "so the protected opposite-side short option is preferred to full linear futures delta"
            )
            return opt

    # Fast, clean expansion: defined-risk convexity is allowed only with a real
    # live ATM contract from ODME and no explicit rich-premium warning.
    long_leg = _atm_option_leg(evidence, direction)
    if (
        es >= rs + 8.0
        and exec_dir == direction
        and active_momentum
        and fast_path
        and long_leg
        and premium_class in {"FAVOURABLE", "UNKNOWN"}
    ):
        return {
            "strategy_type": "LONG_CE" if direction == "BULLISH" else "LONG_PE",
            "legs": [long_leg],
            "expression_style": "CONVEXITY",
            "expression_reason": (
                f"the {direction.lower()} thesis is in active expansion on a {path.lower() or 'clean'} path, "
                "so defined-risk ATM option convexity is preferred to linear futures exposure"
            ),
        }

    return result


def _new_entry_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    plan = dict(_new_entry_plan_sb36(base, evidence) or {})
    if _u(plan.get("kind")) != "NEW":
        return plan
    direction = _u(plan.get("direction"))
    if direction not in {"BULLISH", "BEARISH"}:
        return plan
    expr = _directional_expression(base, evidence, direction, bool((base.get("poi", {}) or {}).get("qualified")))
    plan["strategy_type"] = expr.get("strategy_type", plan.get("strategy_type"))
    plan["legs"] = expr.get("legs", plan.get("legs", []))
    plan["expression_style"] = expr.get("expression_style", "")
    plan["expression_reason"] = expr.get("expression_reason", "")
    if expr.get("expression_reason"):
        plan["reason"] = (_s(plan.get("reason")) + "; " + _s(expr.get("expression_reason"))).strip("; ")
    return plan


def _leg_label(leg: Dict[str, Any]) -> str:
    side = _u(leg.get("side"))
    option = _u(leg.get("option"))
    if option in {"CE", "PE"}:
        return f"{side.lower()} {_fmt(leg.get('strike'))} {option}"
    kind = _u(leg.get("instrument_type") or leg.get("contract"))
    if kind == "FUTURES":
        return "long futures" if side == "BUY" else "short futures"
    return (side + " " + kind).strip().lower()


def _expression_exposure_text(legs: List[Dict[str, Any]]) -> str:
    labels = [_leg_label(x) for x in legs if _u(x.get("status") or "ACTIVE") != "CLOSED"]
    return " + ".join(labels) if labels else "recorded campaign exposure"


def _leg_ids(legs: List[Dict[str, Any]], *, option: str = "", primary_only: bool = False) -> List[str]:
    out: List[str] = []
    for leg in legs:
        if option and _u(leg.get("option")) != option:
            continue
        if primary_only and _u(leg.get("role") or "PRIMARY") not in {"PRIMARY", "EXPRESSION"}:
            continue
        leg_id = _s(leg.get("leg_id"))
        if leg_id:
            out.append(leg_id)
    return out


def _has_strategy_leg(legs: List[Dict[str, Any]], strategy: str) -> bool:
    strategy = _u(strategy)
    for leg in legs:
        side = _u(leg.get("side")); option = _u(leg.get("option")); kind = _u(leg.get("instrument_type") or leg.get("contract"))
        if strategy == "FUTURES_LONG" and side == "BUY" and kind == "FUTURES": return True
        if strategy == "FUTURES_SHORT" and side == "SELL" and kind == "FUTURES": return True
        if strategy == "SHORT_PE" and side == "SELL" and option == "PE": return True
        if strategy == "SHORT_CE" and side == "SELL" and option == "CE": return True
        if strategy == "LONG_CE" and side == "BUY" and option == "CE": return True
        if strategy == "LONG_PE" and side == "BUY" and option == "PE": return True
    return False


def _supportive_short_leg_safe(base: Dict[str, Any], evidence: Dict[str, Any], direction: str, legs: List[Dict[str, Any]]) -> bool:
    """Whether an existing opposite-side short option can remain as a campaign leg."""
    odme = evidence.get("odme", {}) or {}
    if _s(evidence.get("mode")) != "TV + ODME" or not odme:
        return False
    price = _f(base.get("price"))
    if price is None:
        return False
    option = "PE" if direction == "BULLISH" else "CE"
    sold = next((x for x in legs if _u(x.get("side")) == "SELL" and _u(x.get("option")) == option), None)
    if not sold:
        return False
    strike = _f(sold.get("strike"))
    if strike is None:
        return False
    if direction == "BULLISH" and price <= strike:
        return False
    if direction == "BEARISH" and price >= strike:
        return False
    exec_dir = _u(base.get("exec_of")); aur = _u((base.get("aurora", {}) or {}).get("state"))
    if direction == "BULLISH" and exec_dir == "BEARISH" and aur == "RED":
        return False
    if direction == "BEARISH" and exec_dir == "BULLISH" and aur == "GREEN":
        return False
    safe = _f(odme.get("safer_sell_pe" if option == "PE" else "safer_sell_ce"))
    if safe is not None:
        if option == "PE" and safe > strike:
            # A higher safer PE remains farther from spot for a bullish campaign only
            # if it is still below price; otherwise protection has deteriorated.
            if safe >= price:
                return False
        if option == "CE" and safe < strike:
            if safe <= price:
                return False
    return True


def _manage_existing(base: Dict[str, Any], evidence: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not open_trades:
        return {}
    trade = dict(open_trades[0])
    meta = _campaign_meta(trade)
    current_legs = _active_campaign_legs(trade)
    base_plan = dict(_manage_existing_sb36(base, evidence, open_trades) or {})
    if _u(base_plan.get("kind")) != "MANAGE":
        return base_plan

    base_plan["current_legs"] = current_legs
    base_plan["legs"] = current_legs
    base_plan["campaign_operation"] = base_plan.get("campaign_operation", "")

    # Existing SB3.6 invalidation/opposite-thesis exits remain authoritative.
    if _u(base_plan.get("action")) == "EXIT":
        direction = _u(trade.get("direction"))
        posture = _u(base.get("posture"))
        strategy = _u(trade.get("strategy_type"))
        reason = _s(base_plan.get("reason")).lower()

        # After the one-time SB3.7 adoption scan, an aligned directional
        # expansion does not have to kill a short-premium campaign.  The thesis
        # can survive while its vehicle changes.
        same_direction_posture = (direction == "BULLISH" and posture == "LONG_ELIGIBLE") or (direction == "BEARISH" and posture == "SHORT_ELIGIBLE")
        if (
            meta.get("campaign_schema") == "SB_CAMPAIGN1"
            and strategy in {"SHORT_PE", "SHORT_CE"}
            and same_direction_posture
            and ("expansion" in reason or "arrival risk" in reason)
            and "strike" not in reason
            and _u(base.get("exec_of")) == direction
            and ((direction == "BULLISH" and _u((base.get("aurora", {}) or {}).get("state")) == "GREEN") or (direction == "BEARISH" and _u((base.get("aurora", {}) or {}).get("state")) == "RED"))
        ):
            expr = _directional_expression(base, evidence, direction, bool((base.get("poi", {}) or {}).get("qualified")))
            candidate = _u(expr.get("strategy_type"))
            if candidate and candidate != strategy:
                return {
                    **base_plan,
                    "action": "REDUCE", "status": "ACTIVE",
                    "strategy_type": candidate, "next_strategy_type": candidate,
                    "campaign_operation": "ROTATE",
                    "close_leg_ids": _leg_ids(current_legs),
                    "new_legs": list(expr.get("legs", []) or []),
                    "reason": f"the {direction.lower()} thesis remains valid while the market has shifted from non-arrival into directional expansion; SuperBrain is closing the short-premium expression and rotating the same campaign into {_strategy_from_trade_plan({'strategy_type': candidate, 'legs': expr.get('legs', [])})}",
                    "fresh_entry_now": False,
                }

        # A range campaign can evolve into a qualified directional campaign in
        # the same underlying instead of being blindly terminated and blocked.
        if strategy == "SHORT_STRANGLE" and posture in {"LONG_ELIGIBLE", "SHORT_ELIGIBLE"} and "strike" not in reason:
            new_dir = "BULLISH" if posture == "LONG_ELIGIBLE" else "BEARISH"
            expr = _directional_expression(base, evidence, new_dir, bool((base.get("poi", {}) or {}).get("qualified")))
            close_opt = "CE" if new_dir == "BULLISH" else "PE"
            close_ids = _leg_ids(current_legs, option=close_opt)
            supportive = "PE" if new_dir == "BULLISH" else "CE"
            keep_supportive = any(_u(x.get("side")) == "SELL" and _u(x.get("option")) == supportive for x in current_legs)
            new_legs = [] if _has_strategy_leg(current_legs, _u(expr.get("strategy_type"))) else list(expr.get("legs", []) or [])
            return {
                **base_plan,
                "action": "ADD" if new_legs else "REDUCE",
                "status": "ACTIVE",
                "direction": new_dir,
                "strategy_type": expr.get("strategy_type"),
                "next_strategy_type": expr.get("strategy_type"),
                "campaign_operation": "TRANSFORM",
                "close_leg_ids": close_ids,
                "new_legs": new_legs,
                "reason": (
                    f"the neutral campaign has developed into a qualified {new_dir.lower()} thesis; "
                    f"SuperBrain is closing the adverse {close_opt} side"
                    + (f", retaining the protected {supportive} short" if keep_supportive else "")
                    + (f" and adding {_strategy_from_trade_plan({'strategy_type': expr.get('strategy_type'), 'legs': expr.get('legs', [])})}" if new_legs else "")
                    + " within the same underlying campaign"
                ),
                "fresh_entry_now": False,
            }

        base_plan["campaign_operation"] = "EXIT_CAMPAIGN"
        base_plan["close_leg_ids"] = _leg_ids(current_legs)
        return base_plan

    # First scan after upgrading an old SB3.6 trade only adopts the campaign/leg
    # model.  No expression rotation is caused by the software migration itself.
    if meta.get("campaign_schema") != "SB_CAMPAIGN1":
        base_plan["campaign_operation"] = "ADOPT"
        base_plan["reason"] = _s(base_plan.get("reason"))
        return base_plan

    direction = _u(trade.get("direction"))
    strategy = _u(trade.get("strategy_type"))
    posture = _u(base.get("posture"))

    # Neutral campaign -> directional campaign even when SB3.6 still returned HOLD.
    if strategy == "SHORT_STRANGLE" and posture in {"LONG_ELIGIBLE", "SHORT_ELIGIBLE"}:
        new_dir = "BULLISH" if posture == "LONG_ELIGIBLE" else "BEARISH"
        expr = _directional_expression(base, evidence, new_dir, bool((base.get("poi", {}) or {}).get("qualified")))
        close_opt = "CE" if new_dir == "BULLISH" else "PE"
        close_ids = _leg_ids(current_legs, option=close_opt)
        new_legs = [] if _has_strategy_leg(current_legs, _u(expr.get("strategy_type"))) else list(expr.get("legs", []) or [])
        return {
            **base_plan,
            "action": "ADD" if new_legs else "REDUCE",
            "status": "ACTIVE",
            "direction": new_dir,
            "strategy_type": expr.get("strategy_type"),
            "next_strategy_type": expr.get("strategy_type"),
            "campaign_operation": "TRANSFORM",
            "close_leg_ids": close_ids,
            "new_legs": new_legs,
            "reason": f"range containment has given way to a qualified {new_dir.lower()} campaign, so SuperBrain is removing the adverse strangle side and re-expressing the same underlying campaign directionally",
            "fresh_entry_now": False,
        }

    if direction not in {"BULLISH", "BEARISH"}:
        return base_plan

    expr = _directional_expression(base, evidence, direction, bool((base.get("poi", {}) or {}).get("qualified")))
    candidate = _u(expr.get("strategy_type"))
    if not candidate or candidate == strategy:
        return base_plan

    action = _u(base_plan.get("action"))
    reason_low = _s(base_plan.get("reason")).lower()

    # Existing short premium can remain as a supportive income leg while a new
    # directional expression is added, provided its sold strike is still safe.
    if strategy in {"SHORT_PE", "SHORT_CE"} and candidate in {"FUTURES_LONG", "FUTURES_SHORT", "LONG_CE", "LONG_PE"}:
        if _supportive_short_leg_safe(base, evidence, direction, current_legs):
            new_legs = [] if _has_strategy_leg(current_legs, candidate) else list(expr.get("legs", []) or [])
            if new_legs:
                return {
                    **base_plan,
                    "action": "ADD", "status": "ACTIVE",
                    "strategy_type": candidate, "next_strategy_type": candidate,
                    "campaign_operation": "ADD_LEG", "new_legs": new_legs,
                    "reason": (
                        f"the {direction.lower()} campaign has shifted from non-arrival toward stronger directional expression; "
                        f"the existing opposite-side short option remains protected, so SuperBrain is retaining it and adding {_strategy_from_trade_plan({'strategy_type': candidate, 'legs': new_legs})} as a new campaign leg"
                    ),
                    "fresh_entry_now": False,
                }
        # If the short option is no longer a suitable carry leg, rotate it out.
        return {
            **base_plan,
            "action": "REDUCE", "status": "ACTIVE",
            "strategy_type": candidate, "next_strategy_type": candidate,
            "campaign_operation": "ROTATE",
            "close_leg_ids": _leg_ids(current_legs, primary_only=False),
            "new_legs": list(expr.get("legs", []) or []),
            "reason": f"the underlying {direction.lower()} thesis remains valid, but the option non-arrival expression is no longer the right vehicle; SuperBrain is closing it and rotating into {_strategy_from_trade_plan({'strategy_type': candidate, 'legs': expr.get('legs', [])})}",
            "fresh_entry_now": False,
        }

    # Long options are regime-sensitive.  If expansion/clean-path conditions no
    # longer justify convexity, rotate the vehicle while preserving the thesis.
    if strategy in {"LONG_CE", "LONG_PE"}:
        return {
            **base_plan,
            "action": "REDUCE", "status": "ACTIVE",
            "strategy_type": candidate, "next_strategy_type": candidate,
            "campaign_operation": "ROTATE",
            "close_leg_ids": _leg_ids(current_legs, primary_only=False),
            "new_legs": list(expr.get("legs", []) or []),
            "reason": f"the {direction.lower()} thesis remains valid but the fast-expansion/convexity conditions have faded; SuperBrain is closing the long option expression and rotating into {_strategy_from_trade_plan({'strategy_type': candidate, 'legs': expr.get('legs', [])})}",
            "fresh_entry_now": False,
        }

    # Futures are not switched merely because another vehicle scores slightly
    # better.  Rotation is reserved for actual deterioration/failure-to-perform
    # already recognized by the existing SB3.6 management layer.
    if strategy in {"FUTURES_LONG", "FUTURES_SHORT"} and action == "REDUCE" and candidate in {"SHORT_PE", "SHORT_CE"}:
        return {
            **base_plan,
            "action": "REDUCE", "status": "ACTIVE",
            "strategy_type": candidate, "next_strategy_type": candidate,
            "campaign_operation": "ROTATE",
            "close_leg_ids": _leg_ids(current_legs, primary_only=False),
            "new_legs": list(expr.get("legs", []) or []),
            "reason": (
                f"the {direction.lower()} campaign is still valid, but the expected directional follow-through has weakened; "
                f"rather than carrying full futures delta, SuperBrain is closing the futures expression and rotating into {_strategy_from_trade_plan({'strategy_type': candidate, 'legs': expr.get('legs', [])})}"
            ),
            "fresh_entry_now": False,
        }

    return base_plan


def _strategy_from_trade_plan(plan: Dict[str, Any]) -> str:
    st = _u(plan.get("strategy_type")); legs = plan.get("legs", []) or plan.get("new_legs", []) or []
    if st == "FUTURES_LONG": return "long futures exposure"
    if st == "FUTURES_SHORT": return "short futures exposure"
    if st == "SHORT_CE":
        leg = next((x for x in legs if _u(x.get("option")) == "CE" and _u(x.get("side")) == "SELL"), {})
        return f"{_fmt(leg.get('strike'))} CE short" if leg else "call short"
    if st == "SHORT_PE":
        leg = next((x for x in legs if _u(x.get("option")) == "PE" and _u(x.get("side")) == "SELL"), {})
        return f"{_fmt(leg.get('strike'))} PE short" if leg else "put short"
    if st == "LONG_CE":
        leg = next((x for x in legs if _u(x.get("option")) == "CE" and _u(x.get("side")) == "BUY"), {})
        return f"{_fmt(leg.get('strike'))} CE long" if leg else "call long"
    if st == "LONG_PE":
        leg = next((x for x in legs if _u(x.get("option")) == "PE" and _u(x.get("side")) == "BUY"), {})
        return f"{_fmt(leg.get('strike'))} PE long" if leg else "put long"
    if st == "SHORT_STRANGLE":
        ce = next((x for x in legs if _u(x.get("option")) == "CE" and _u(x.get("side")) == "SELL"), {})
        pe = next((x for x in legs if _u(x.get("option")) == "PE" and _u(x.get("side")) == "SELL"), {})
        if ce and pe:
            return f"{_fmt(ce.get('strike'))} CE / {_fmt(pe.get('strike'))} PE short strangle"
        return "short strangle"
    return st.lower().replace("_", " ") or "exposure"


def _strategy_text(plan: Dict[str, Any]) -> str:
    st = _u(plan.get("strategy_type")); legs = plan.get("legs", []) or []
    if st == "FUTURES_LONG": return "take a long futures exposure"
    if st == "FUTURES_SHORT": return "take a short futures exposure"
    if st == "LONG_CE":
        leg = next((x for x in legs if _u(x.get("option")) == "CE"), {})
        return f"buy the {_fmt(leg.get('strike'))} CE" if leg else "buy an ATM CE"
    if st == "LONG_PE":
        leg = next((x for x in legs if _u(x.get("option")) == "PE"), {})
        return f"buy the {_fmt(leg.get('strike'))} PE" if leg else "buy an ATM PE"
    if st == "SHORT_CE" and legs: return f"sell the {_fmt(legs[0].get('strike'))} CE"
    if st == "SHORT_PE" and legs: return f"sell the {_fmt(legs[0].get('strike'))} PE"
    if st == "SHORT_STRANGLE" and len(legs) >= 2:
        ce = next((x for x in legs if _u(x.get("option")) == "CE"), legs[0]); pe = next((x for x in legs if _u(x.get("option")) == "PE"), legs[-1])
        return f"sell the {_fmt(ce.get('strike'))} CE and {_fmt(pe.get('strike'))} PE as one short strangle"
    return _strategy_from_trade_plan(plan)


def _material_signature(base: Dict[str, Any], plan: Dict[str, Any], progression: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    sig = dict(_material_signature_sb36(base, plan, progression, evidence) or {})
    sig["campaign_operation"] = _u(plan.get("campaign_operation"))
    sig["next_strategy"] = _u(plan.get("next_strategy_type"))
    current = plan.get("current_legs", []) or plan.get("legs", []) or []
    new_legs = plan.get("new_legs", []) or []
    sig["campaign_exposure"] = [_leg_label(x) for x in current]
    sig["campaign_new_legs"] = [_leg_label(x) for x in new_legs]
    sig["campaign_close_ids"] = sorted([_s(x) for x in (plan.get("close_leg_ids", []) or []) if _s(x)])
    return sig


def _campaign_action_paragraph(plan: Dict[str, Any], price: Optional[float]) -> str:
    current = plan.get("current_legs", []) or plan.get("legs", []) or []
    exposure = _expression_exposure_text(current)
    action = _u(plan.get("action"))
    operation = _u(plan.get("campaign_operation"))
    reason = _sentence_case(plan.get("reason"))

    if operation == "TRANSFORM":
        new_desc = _strategy_from_trade_plan({"strategy_type": plan.get("next_strategy_type") or plan.get("strategy_type"), "legs": plan.get("new_legs", []) or []}) if plan.get("new_legs") else "the remaining campaign legs"
        return f"**SuperBrain currently has this exposure: {exposure}. It is transforming the campaign now toward {new_desc}.** {reason}."
    if operation == "ROTATE":
        new_desc = _strategy_from_trade_plan({"strategy_type": plan.get("next_strategy_type") or plan.get("strategy_type"), "legs": plan.get("new_legs", []) or []})
        return f"**SuperBrain currently has this exposure: {exposure}. It is closing the current primary expression and rotating the same campaign into {new_desc}.** {reason}."
    if operation == "ADD_LEG":
        new_desc = _strategy_from_trade_plan({"strategy_type": plan.get("next_strategy_type") or plan.get("strategy_type"), "legs": plan.get("new_legs", []) or []})
        return f"**SuperBrain currently has this exposure: {exposure}. It is retaining it and adding {new_desc} as another leg of the same campaign.** {reason}."
    if operation == "CLOSE_LEG":
        return f"**SuperBrain currently has this exposure: {exposure}. It is removing the deteriorating leg while keeping the campaign alive.** {reason}."
    if operation == "ADOPT":
        return f"**SuperBrain currently has this exposure: {exposure}. It is continuing to manage the same live campaign without changing the expression on this migration scan.** {reason}."
    if action == "HOLD":
        return f"**SuperBrain currently has this exposure: {exposure}. It is holding the campaign.** {reason}."
    if action == "ADD":
        return f"**SuperBrain currently has this exposure: {exposure}. It is adding exposure now at spot {_fmt(price)}.** {reason}."
    if action == "REDUCE":
        return f"**SuperBrain currently has this exposure: {exposure}. It is reducing campaign risk.** {reason}."
    if action == "EXIT":
        return f"**SuperBrain currently has this exposure: {exposure}. It is closing the campaign now at spot {_fmt(price)}.** {reason}."
    return f"**SuperBrain currently has this exposure: {exposure}. Current management action: {action or 'WATCH'}.** {reason}."


def _narrative(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """SB3.7: exposure first for an existing campaign, market view second."""
    text = _narrative_sb36(base, evidence, plan)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if _u(plan.get("kind")) != "MANAGE":
        return text

    # Remove the older management-action paragraph and replace it with a
    # campaign-first statement.  Keep all market/ODME/follow-up explanation.
    remainder: List[str] = []
    for p in paras:
        low = p.lower()
        if p.startswith("**SuperBrain is holding") or p.startswith("**SuperBrain is adding") or p.startswith("**SuperBrain is reducing") or p.startswith("**SuperBrain is exiting") or p.startswith("**SuperBrain action"):
            continue
        if p.startswith("**SuperBrain currently has this exposure"):
            continue
        remainder.append(p)

    first = _campaign_action_paragraph(plan, _f(base.get("price")))
    market_intro = "Market view:"
    if remainder:
        remainder[0] = market_intro + " " + remainder[0]
    else:
        remainder = [market_intro + " the campaign thesis remains under active scan management."]
    return "\n\n".join([first] + remainder).strip()


# ============================================================================
# SB3.8 relevance / composite path / non-arrival intelligence overrides
# ============================================================================
import math
import re

REASONER_VERSION = "SB3.8_RELEVANCE_PATH_INTELLIGENCE"

_new_entry_plan_sb37 = _new_entry_plan
_manage_existing_sb37 = _manage_existing
_narrative_sb37 = _narrative
_material_signature_sb37 = _material_signature
_analyze_market_sb37 = analyze_market


def _expiry_datetime(evidence: Dict[str, Any]) -> Optional[datetime]:
    odme = evidence.get("odme", {}) or {}
    raw = _s(odme.get("expiry"))
    if not raw:
        return None
    token = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    d = None
    for fmt in ("%d%b%Y", "%d%m%Y", "%Y%m%d"):
        try:
            d = datetime.strptime(token, fmt).date()
            break
        except Exception:
            continue
    if d is None:
        return None
    exch = _u(odme.get("exchange"))
    tz = ZoneInfo("Asia/Kolkata")
    hh, mm = (23, 30) if exch == "MCX" else (15, 30)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)


def _scan_datetime(evidence: Dict[str, Any]) -> datetime:
    raw = _s(evidence.get("scanned_at"))
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _tf_minutes(value: Any) -> float:
    s = _u(value)
    if not s:
        return 60.0
    if s in {"D", "1D"}:
        return 1440.0
    if s in {"W", "1W"}:
        return 10080.0
    try:
        x = float(s)
        return x if x > 0 else 60.0
    except Exception:
        return 60.0


def _chain_rows(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    odme = evidence.get("odme", {}) or {}
    rows = odme.get("chain_window", []) or []
    out: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        strike = _f(raw.get("strike"))
        if strike is None:
            continue
        rec = dict(raw)
        rec["strike"] = strike
        out.append(rec)
    return sorted(out, key=lambda x: x["strike"])


def _bs_norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(spot: float, strike: float, t: float, rate: float, vol: float, option: str) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    t = max(float(t), 1e-9)
    vol = max(float(vol), 1e-6)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if _u(option) == "CE":
        return max(0.0, spot * _bs_norm_cdf(d1) - strike * math.exp(-rate * t) * _bs_norm_cdf(d2))
    return max(0.0, strike * math.exp(-rate * t) * _bs_norm_cdf(-d2) - spot * _bs_norm_cdf(-d1))


def _implied_vol(spot: float, strike: float, t: float, rate: float, premium: float, option: str) -> Optional[float]:
    if min(spot, strike, premium) <= 0 or t <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if _u(option) == "CE" else max(strike - spot, 0.0)
    if premium <= intrinsic + 1e-8:
        return 0.05
    lo, hi = 0.01, 5.0
    if _bs_price(spot, strike, t, rate, hi, option) < premium:
        return None
    for _ in range(70):
        mid = (lo + hi) * 0.5
        px = _bs_price(spot, strike, t, rate, mid, option)
        if px > premium:
            hi = mid
        else:
            lo = mid
    return (lo + hi) * 0.5


def _option_greeks(spot: float, strike: float, t: float, rate: float, vol: float, option: str) -> Dict[str, float]:
    if min(spot, strike, t, vol) <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta_day": 0.0, "vega": 0.0}
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * root_t)
    d2 = d1 - vol * root_t
    pdf = _bs_norm_pdf(d1)
    gamma = pdf / (spot * vol * root_t)
    vega = spot * pdf * root_t / 100.0
    if _u(option) == "CE":
        delta = _bs_norm_cdf(d1)
        theta = -(spot * pdf * vol) / (2 * root_t) - rate * strike * math.exp(-rate * t) * _bs_norm_cdf(d2)
    else:
        delta = _bs_norm_cdf(d1) - 1.0
        theta = -(spot * pdf * vol) / (2 * root_t) + rate * strike * math.exp(-rate * t) * _bs_norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta_day": theta / 365.0, "vega": vega}


def _current_focus(base: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    posture = _u(base.get("posture"))
    if posture == "LONG_ELIGIBLE":
        return "BULLISH"
    if posture == "SHORT_ELIGIBLE":
        return "BEARISH"

    bull = 0.0
    bear = 0.0
    macro = _u(base.get("macro"))
    if macro == "BULLISH": bull += 2.0
    if macro == "BEARISH": bear += 2.0
    exec_dir = _u(base.get("exec_of"))
    if exec_dir == "BULLISH": bull += 1.5
    if exec_dir == "BEARISH": bear += 1.5
    aur = _u((base.get("aurora", {}) or {}).get("state"))
    if aur in {"GREEN", "YELLOW"}: bull += 1.0
    if aur in {"RED", "PINK"}: bear += 1.0
    struct = _u((_source_map(evidence).get("STRUCTURE", {}) or {}).get("structure_trend"))
    if struct == "UP": bull += 1.0
    if struct == "DOWN": bear += 1.0

    price = _f(base.get("price"))
    for h in base.get("hurdles", []) or []:
        if not isinstance(h, dict):
            continue
        dist = _f(h.get("distance"))
        side = _u(h.get("side"))
        if dist is None or price is None:
            continue
        rel = dist / max(abs(price), 1.0)
        if rel <= 0.03:
            if side == "DEMAND": bull += 1.5
            if side == "SUPPLY": bear += 1.5

    liq = base.get("liquidity", {}) or {}
    read = _u(liq.get("directional_read"))
    if read == "BULLISH": bull += 0.75
    if read == "BEARISH": bear += 0.75
    if abs(bull - bear) < 1.5:
        return "NEUTRAL"
    return "BULLISH" if bull > bear else "BEARISH"


def _between(price: float, strike: float, low: Optional[float], high: Optional[float]) -> bool:
    if low is None or high is None:
        return False
    lo, hi = min(low, high), max(low, high)
    if strike < price:
        return strike < hi and lo < price and hi <= price + 1e-9
    return price < lo and hi > price


def _route_hurdles(base: Dict[str, Any], evidence: Dict[str, Any], strike: float) -> List[Dict[str, Any]]:
    price = _f(base.get("price"))
    if price is None or strike == price:
        return []
    down = strike < price
    edge = _source_map(evidence).get("EDGE", {}) or {}
    raw = edge.get("raw_json") if isinstance(edge.get("raw_json"), dict) else {}
    out: List[Dict[str, Any]] = []

    book = ((raw or {}).get("fp_hurdle_book") or {}).get("hurdles", []) if isinstance(raw, dict) else []
    for h in book or []:
        if not isinstance(h, dict):
            continue
        side = _u(h.get("side"))
        if (down and side != "DEMAND") or ((not down) and side != "SUPPLY"):
            continue
        lo, hi = _f(h.get("low")), _f(h.get("high"))
        if _between(price, strike, lo, hi):
            out.append({
                "side": side, "low": lo, "high": hi, "tf": _s(h.get("parent") or ""),
                "strength": _s(h.get("strength")), "strength_score": _f(h.get("strength_score")),
                "hold": _f(h.get("hold")), "status": _u(h.get("status") or h.get("context")),
                "source": "zone",
            })

    router = (raw or {}).get("edge_router_map", []) if isinstance(raw, dict) else []
    for tfrow in router or []:
        if not isinstance(tfrow, dict):
            continue
        tf = _s(tfrow.get("tf"))
        names = ("d1", "d2") if down else ("s1", "s2")
        for name in names:
            z = tfrow.get(name) or {}
            if not isinstance(z, dict):
                continue
            lo, hi = _f(z.get("low")), _f(z.get("high"))
            if _between(price, strike, lo, hi):
                out.append({"side": "DEMAND" if down else "SUPPLY", "low": lo, "high": hi, "tf": tf, "status": "ACTIVE", "source": "route"})

    for z in (base.get("defender", {}) or {}, base.get("challenger", {}) or {}):
        side = _u(z.get("side"))
        if (down and side == "DEMAND") or ((not down) and side == "SUPPLY"):
            lo, hi = _f(z.get("low")), _f(z.get("high"))
            if _between(price, strike, lo, hi):
                out.append({"side": side, "low": lo, "high": hi, "tf": _s(z.get("tf")), "status": _u(z.get("state") or "ACTIVE"), "source": "strategic"})

    unique: Dict[str, Dict[str, Any]] = {}
    for h in out:
        lo, hi = _f(h.get("low")), _f(h.get("high"))
        key = f"{h.get('side')}|{round(lo or 0,4)}|{round(hi or 0,4)}"
        existing = unique.get(key)
        if existing is None or (h.get("source") == "strategic" and existing.get("source") != "strategic"):
            unique[key] = h
    return list(unique.values())


def _hurdle_weight(h: Dict[str, Any]) -> float:
    status = _u(h.get("status"))
    if any(x in status for x in ("RUN", "BROKEN", "INVALID", "CROSSED")):
        return 0.0
    tf = _u(h.get("tf"))
    tfw = {"W": 1.0, "D": 0.9, "240": 0.75, "125": 0.65, "75": 0.6, "60": 0.55, "15": 0.35}.get(tf, 0.45)
    statusw = 1.0 if "ACTIVE_FRESH" in status or status == "ACTIVE" else 0.65 if "TOUCHED" in status else 0.8
    strength = _f(h.get("strength_score"))
    if strength is not None:
        strengthw = min(1.15, max(0.6, strength / 75.0))
    else:
        strengthw = 1.0 if "STRONG" in _u(h.get("strength")) else 0.8
    hold = _f(h.get("hold"))
    holdw = min(1.15, max(0.75, (hold or 60.0) / 72.0))
    return tfw * statusw * strengthw * holdw


def _oi_route_friction(evidence: Dict[str, Any], price: float, strike: float, option: str) -> Tuple[float, List[Dict[str, Any]]]:
    """Estimate option-positioning friction on the route to a sold strike.

    This is deliberately not a probability model.  Relevant-side OI is the
    primary friction, while combined OI / POC / active wall add smaller route
    penalties when they sit between current price and the candidate strike.
    """
    odme = evidence.get("odme", {}) or {}
    rows = [r for r in _chain_rows(evidence) if min(price, strike) < r["strike"] < max(price, strike)]
    if not rows:
        return 0.0, []
    field = "pe_oi" if _u(option) == "PE" else "ce_oi"
    mx_side = max((_f(r.get(field)) or 0.0) for r in rows) or 0.0
    mx_combined = max((_f(r.get("combined_oi")) or 0.0) for r in rows) or 0.0
    ranked = sorted(
        rows,
        key=lambda r: ((_f(r.get(field)) or 0.0) / max(mx_side, 1.0)) * 0.7 + ((_f(r.get("combined_oi")) or 0.0) / max(mx_combined, 1.0)) * 0.3,
        reverse=True,
    )
    strong: List[Dict[str, Any]] = []
    score = 0.0
    for r in ranked[:8]:
        side_oi = _f(r.get(field)) or 0.0
        combined = _f(r.get("combined_oi")) or 0.0
        rel_side = side_oi / mx_side if mx_side else 0.0
        rel_combined = combined / mx_combined if mx_combined else 0.0
        strength = 0.7 * rel_side + 0.3 * rel_combined
        if strength < 0.42:
            continue
        strong.append({"strike": r["strike"], "oi": side_oi, "combined_oi": combined, "relative": strength})
        score += 0.065 * strength

    # The current option POC and active relevant wall are additional friction
    # only when they genuinely lie on the route.  They never act as hard walls.
    route_lo, route_hi = min(price, strike), max(price, strike)
    poc = _f(odme.get("option_poc") or odme.get("poc"))
    wall = _f(odme.get("active_pe_wall") or odme.get("pe_wall")) if _u(option) == "PE" else _f(odme.get("active_ce_wall") or odme.get("ce_wall"))
    if poc is not None and route_lo < poc < route_hi:
        score += 0.07
    if wall is not None and route_lo < wall < route_hi:
        score += 0.09

    return min(0.45, score), strong[:5]

def _fast_arrival_hours(base: Dict[str, Any], evidence: Dict[str, Any], strike: float, option: str) -> Optional[float]:
    price = _f(base.get("price"))
    if price is None or strike <= 0:
        return None
    src = _source_map(evidence)
    aur = src.get("AURORA", {}) or {}
    tfh = _tf_minutes(aur.get("tf")) / 60.0
    down = _u(option) == "PE"
    prefix = "eta_down" if down else "eta_up"
    candidates: List[float] = []
    speed = _f(aur.get(f"{prefix}_speed_price_per_bar"))
    dist = abs(price - strike)
    if speed is not None and speed > 0:
        candidates.append((dist / speed) * tfh)
    eta_level = _f(aur.get(f"{prefix}_level"))
    rem = _f(aur.get(f"{prefix}_locked_remaining_bars"))
    if eta_level is not None and rem is not None and rem > 0:
        level_dist = abs(price - eta_level)
        if level_dist > 0:
            candidates.append(max(0.25, rem * dist / level_dist) * tfh)
    model_bars = _f(aur.get(f"{prefix}_model_bars"))
    model_dist = _f(aur.get(f"{prefix}_model_distance"))
    if model_bars is not None and model_bars > 0 and model_dist is not None and model_dist > 0:
        candidates.append(max(0.25, model_bars * dist / model_dist) * tfh)
    vals = [x for x in candidates if math.isfinite(x) and x > 0]
    return min(vals) if vals else None


def _composite_path(base: Dict[str, Any], evidence: Dict[str, Any], strike: float, option: str) -> Dict[str, Any]:
    price = _f(base.get("price"))
    now = _scan_datetime(evidence)
    expiry = _expiry_datetime(evidence)
    fastest = _fast_arrival_hours(base, evidence, strike, option)
    if price is None or fastest is None:
        return {"available": False, "reason": "arrival speed is not available from the current market path"}

    hurdles = _route_hurdles(base, evidence, strike)
    structural = min(0.85, sum(_hurdle_weight(x) for x in hurdles) * 0.10)
    oi_friction, oi_clusters = _oi_route_friction(evidence, price, strike, option)

    down = _u(option) == "PE"
    path_dir = "BEARISH" if down else "BULLISH"
    aligned = 0.0
    exec_dir = _u(base.get("exec_of"))
    aur_state = _u((base.get("aurora", {}) or {}).get("state"))
    if exec_dir == path_dir:
        aligned += 0.10
    if (path_dir == "BULLISH" and aur_state == "GREEN") or (path_dir == "BEARISH" and aur_state == "RED"):
        aligned += 0.12
    opposite = 0.0
    if exec_dir == _opposite(path_dir):
        opposite += 0.14
    if (path_dir == "BULLISH" and aur_state == "RED") or (path_dir == "BEARISH" and aur_state == "GREEN"):
        opposite += 0.18

    struct = _u((_source_map(evidence).get("STRUCTURE", {}) or {}).get("structure_trend"))
    if (down and struct == "UP") or ((not down) and struct == "DOWN"):
        opposite += 0.10
    if (down and struct == "DOWN") or ((not down) and struct == "UP"):
        aligned += 0.06

    liq = base.get("liquidity", {}) or {}
    magnet = _f(liq.get("below_level" if down else "above_level"))
    if magnet is not None and min(price, strike) < magnet < max(price, strike):
        aligned += 0.08

    multiplier = max(1.0, 1.0 + structural + oi_friction + opposite - aligned)
    expected = fastest * multiplier
    fastest_dt = now + timedelta(hours=fastest)
    expected_dt = now + timedelta(hours=expected)
    buffer_hours = min(24.0, max(6.0, expected * 0.12))
    safe_dt = expected_dt - timedelta(hours=buffer_hours)
    protected = bool(expiry and safe_dt >= expiry)
    if expiry and safe_dt > expiry:
        display_safe = expiry
    else:
        display_safe = safe_dt
    return {
        "available": True,
        "fastest_hours": fastest,
        "expected_hours": expected,
        "fastest_arrival": fastest_dt.isoformat(),
        "expected_arrival": expected_dt.isoformat(),
        "safe_through": display_safe.isoformat(),
        "protected_through_expiry": protected,
        "expiry": expiry.isoformat() if expiry else "",
        "hurdle_count": len(hurdles),
        "structural_friction": round(structural, 4),
        "oi_friction": round(oi_friction, 4),
        "oi_clusters": oi_clusters[:4],
        "hurdles": hurdles[:8],
        "path_multiplier": round(multiplier, 3),
    }


def _format_date_iso(iso_value: Any, evidence: Dict[str, Any]) -> str:
    raw = _s(iso_value)
    if not raw:
        return "?"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        tz = ZoneInfo("Asia/Kolkata") if _u((evidence.get("odme", {}) or {}).get("exchange")) in {"NFO", "MCX"} else ZoneInfo("Asia/Singapore")
        return dt.astimezone(tz).strftime("%d %b")
    except Exception:
        return raw


def _candidate_rows_for_side(base: Dict[str, Any], evidence: Dict[str, Any], option: str) -> List[Dict[str, Any]]:
    odme = evidence.get("odme", {}) or {}
    price = _f(base.get("price"))
    if price is None:
        return []
    safe = _f(odme.get("safer_sell_pe" if _u(option) == "PE" else "safer_sell_ce"))
    if safe is None:
        return []
    rows = _chain_rows(evidence)
    if _u(option) == "PE":
        rows = [r for r in rows if r["strike"] <= safe and r["strike"] < price and (_f(r.get("pe_ltp")) or 0) > 0]
        rows.sort(key=lambda r: r["strike"], reverse=True)
    else:
        rows = [r for r in rows if r["strike"] >= safe and r["strike"] > price and (_f(r.get("ce_ltp")) or 0) > 0]
        rows.sort(key=lambda r: r["strike"])
    return rows[:4]


def _evaluate_short_option(base: Dict[str, Any], evidence: Dict[str, Any], option: str, row: Dict[str, Any]) -> Dict[str, Any]:
    option = _u(option)
    price = _f(base.get("price")) or 0.0
    strike = _f(row.get("strike")) or 0.0
    ltp = _f(row.get("pe_ltp" if option == "PE" else "ce_ltp")) or 0.0
    bid = _f(row.get("pe_bid" if option == "PE" else "ce_bid")) or 0.0
    ask = _f(row.get("pe_ask" if option == "PE" else "ce_ask")) or 0.0
    # For a naked sale, use executable bid as the reward estimate when present.
    # IV uses a fair mark (mid when sane, otherwise LTP) so greeks are not based
    # on an optimistic sale price.
    entry_premium = bid if bid > 0 else ltp
    mark = (bid + ask) * 0.5 if bid > 0 and ask >= bid else ltp
    if mark <= 0:
        mark = entry_premium
    expiry = _expiry_datetime(evidence)
    now = _scan_datetime(evidence)
    if min(price, strike, entry_premium, mark) <= 0 or not expiry or expiry <= now:
        return {"valid": False}
    t = max((expiry - now).total_seconds() / (365.0 * 24.0 * 3600.0), 1e-6)
    rate = 0.06
    iv = _implied_vol(price, strike, t, rate, mark, option)
    if iv is None:
        return {"valid": False}
    greeks = _option_greeks(price, strike, t, rate, iv, option)
    path = _composite_path(base, evidence, strike, option)
    lot = max(1, int(_f((evidence.get("odme", {}) or {}).get("lot_size")) or 1))
    reward = entry_premium * lot

    atr = _f((_source_map(evidence).get("AURORA", {}) or {}).get("eta_current_atr")) or 0.0
    step = _f((evidence.get("odme", {}) or {}).get("strike_step")) or 0.0
    overshoot = max(2.0 * step, 0.25 * atr, abs(price - strike) * 0.12)
    stress_spot = strike - overshoot if option == "PE" else strike + overshoot
    fastest_iso = _s(path.get("fastest_arrival"))
    try:
        stress_dt = datetime.fromisoformat(fastest_iso) if fastest_iso else expiry - timedelta(hours=6)
        if stress_dt.tzinfo is None:
            stress_dt = stress_dt.replace(tzinfo=timezone.utc)
    except Exception:
        stress_dt = expiry - timedelta(hours=6)
    stress_dt = min(max(stress_dt, now + timedelta(minutes=30)), expiry - timedelta(minutes=5))
    remaining_t = max((expiry - stress_dt).total_seconds() / (365.0 * 24.0 * 3600.0), 1e-6)
    stress_iv = min(5.0, iv * 1.20)
    stress_px = _bs_price(max(stress_spot, 1e-6), strike, remaining_t, rate, stress_iv, option)
    stress_risk = max(0.0, (stress_px - entry_premium) * lot)
    rr = reward / stress_risk if stress_risk > 0 else 99.0

    score = 0.0
    if path.get("protected_through_expiry"):
        score += 5.0
    score += min(2.0, (_f(path.get("structural_friction")) or 0.0) * 3.0)
    score += min(1.5, (_f(path.get("oi_friction")) or 0.0) * 3.0)
    score += min(2.0, rr / 2.0)
    score -= min(2.0, abs(greeks["delta"]) * 4.0)
    score -= min(1.5, abs(greeks["gamma"]) * price * price * 0.01)

    return {
        "valid": bool(path.get("available")),
        "option": option,
        "strike": strike,
        "premium": entry_premium,
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "lot_size": lot,
        "estimated_iv": iv,
        # Position-signed greeks for the naked short.  Raw long-option greeks are
        # retained separately for auditability.
        "delta": -greeks["delta"],
        "gamma": -greeks["gamma"],
        "theta_day": -greeks["theta_day"],
        "vega": -greeks["vega"],
        "option_delta": greeks["delta"],
        "option_gamma": greeks["gamma"],
        "option_theta_day": greeks["theta_day"],
        "option_vega": greeks["vega"],
        "reward": reward,
        "stress_risk": stress_risk,
        "rr": rr,
        "stress_spot": stress_spot,
        "stress_time": stress_dt.isoformat(),
        "stress_iv": stress_iv,
        "path": path,
        "score": score,
        "oi": _f(row.get("pe_oi" if option == "PE" else "ce_oi")) or 0.0,
    }


def _option_mark_at(candidate: Dict[str, Any], spot: float, when: datetime, expiry: datetime, option: str, iv_mult: float = 1.20) -> float:
    iv = float(candidate.get("estimated_iv", 0) or 0)
    strike = float(candidate.get("strike", 0) or 0)
    if iv <= 0 or strike <= 0:
        return 0.0
    t = max((expiry - when).total_seconds() / (365.0 * 24.0 * 3600.0), 1e-6)
    return _bs_price(max(spot, 1e-6), strike, t, 0.06, min(5.0, iv * iv_mult), option)


def _strangle_stress_risk(pe: Dict[str, Any], ce: Dict[str, Any], expiry: datetime) -> float:
    """Joint two-leg mark-to-market stress, not theoretical maximum loss."""
    scenarios = []
    for stressed, other, side in ((pe, ce, "PE"), (ce, pe, "CE")):
        try:
            when = datetime.fromisoformat(_s(stressed.get("stress_time")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            when = expiry - timedelta(minutes=5)
        spot = float(stressed.get("stress_spot", 0) or 0)
        if spot <= 0:
            continue
        total = 0.0
        for cand, opt in ((pe, "PE"), (ce, "CE")):
            px = _option_mark_at(cand, spot, when, expiry, opt, 1.20)
            entry = float(cand.get("premium", 0) or 0)
            lot = int(cand.get("lot_size", 1) or 1)
            total += (px - entry) * lot
        scenarios.append(max(0.0, total))
    return max(scenarios) if scenarios else max(float(pe.get("stress_risk", 0) or 0), float(ce.get("stress_risk", 0) or 0))

def _best_side_candidate(base: Dict[str, Any], evidence: Dict[str, Any], option: str) -> Optional[Dict[str, Any]]:
    odme = evidence.get("odme", {}) or {}
    if not odme or _s(evidence.get("mode")) != "TV + ODME":
        return None
    if not _sale_action_ok(odme, option):
        return None
    # Candidate rows are already ordered nearest-first outside the ODME safer
    # boundary. Prefer the nearest protected listed strike; go farther only when
    # the nearer strike does not remain protected through expiry.
    for row in _candidate_rows_for_side(base, evidence, option):
        candidate = _evaluate_short_option(base, evidence, option, row)
        if not (candidate.get("valid") and candidate.get("path", {}).get("protected_through_expiry")):
            continue
        # Naked premium is allowed only when the live strike is genuinely OTM
        # enough for its estimated tail sensitivity and stress mark-to-market.
        if abs(float(candidate.get("delta", 0) or 0)) > 0.30:
            continue
        reward = float(candidate.get("reward", 0) or 0)
        stress = float(candidate.get("stress_risk", 0) or 0)
        if reward <= 0 or (stress > 0 and reward / stress < 0.25):
            continue
        return candidate
    return None


def _nonarrival_final_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    if _s(evidence.get("mode")) != "TV + ODME":
        return {}
    odme = evidence.get("odme", {}) or {}
    expiry = _expiry_datetime(evidence)
    if not odme or not expiry:
        return {}
    focus = _current_focus(base, evidence)
    range_score = _f(odme.get("range_score")) or 0.0
    expansion_score = _f(odme.get("expansion_score")) or 0.0
    pe = _best_side_candidate(base, evidence, "PE")
    ce = _best_side_candidate(base, evidence, "CE")

    chosen: Optional[Dict[str, Any]] = None
    strategy = ""
    legs: List[Dict[str, Any]] = []
    direction = "NEUTRAL"

    if focus == "BULLISH" and pe:
        chosen = pe; strategy = "SHORT_PE"; direction = "BULLISH"
    elif focus == "BEARISH" and ce:
        chosen = ce; strategy = "SHORT_CE"; direction = "BEARISH"
    elif focus == "NEUTRAL" and pe and ce and range_score >= expansion_score + 5.0:
        # Both tails independently have to remain protected through expiry.
        strategy = "SHORT_STRANGLE"
        direction = "NEUTRAL"
        reward = pe["reward"] + ce["reward"]
        risk = _strangle_stress_risk(pe, ce, expiry)
        chosen = {
            "reward": reward,
            "stress_risk": risk,
            "rr": reward / risk if risk > 0 else 99.0,
            "path": {
                "safe_through": min(pe["path"].get("safe_through", ""), ce["path"].get("safe_through", "")),
                "protected_through_expiry": True,
                "hurdle_count": int(pe["path"].get("hurdle_count", 0)) + int(ce["path"].get("hurdle_count", 0)),
            },
        }
        legs = [
            {"side": "SELL", "instrument_type": "OPTION", "option": "CE", "strike": ce["strike"], "premium": ce["premium"], "expiry": _s(odme.get("expiry")), "estimated_iv": ce["estimated_iv"], "delta": ce["delta"], "gamma": ce["gamma"], "theta_day": ce["theta_day"], "vega": ce["vega"], "lot_size": ce["lot_size"], "ltp": ce.get("ltp"), "bid": ce.get("bid"), "ask": ce.get("ask"), "mark": ce.get("mark")},
            {"side": "SELL", "instrument_type": "OPTION", "option": "PE", "strike": pe["strike"], "premium": pe["premium"], "expiry": _s(odme.get("expiry")), "estimated_iv": pe["estimated_iv"], "delta": pe["delta"], "gamma": pe["gamma"], "theta_day": pe["theta_day"], "vega": pe["vega"], "lot_size": pe["lot_size"], "ltp": pe.get("ltp"), "bid": pe.get("bid"), "ask": pe.get("ask"), "mark": pe.get("mark")},
        ]
    elif focus == "NEUTRAL":
        # A balanced chart does not force a two-sided trade; one exceptionally
        # protected side can still be used if the other tail is not good enough.
        singles = [x for x in (pe, ce) if x]
        if singles:
            best = max(singles, key=lambda x: x.get("score", 0))
            chosen = best
            strategy = "SHORT_PE" if best["option"] == "PE" else "SHORT_CE"
            direction = "BULLISH" if best["option"] == "PE" else "BEARISH"

    if not chosen or not strategy:
        return {}
    if not legs:
        legs = [{
            "side": "SELL", "instrument_type": "OPTION", "option": chosen["option"],
            "strike": chosen["strike"], "premium": chosen["premium"], "expiry": _s(odme.get("expiry")),
            "estimated_iv": chosen["estimated_iv"], "delta": chosen["delta"], "gamma": chosen["gamma"],
            "theta_day": chosen["theta_day"], "vega": chosen["vega"], "lot_size": chosen["lot_size"],
            "ltp": chosen.get("ltp"), "bid": chosen.get("bid"), "ask": chosen.get("ask"), "mark": chosen.get("mark"),
        }]

    path = chosen.get("path", {}) or {}
    expiry_label = expiry.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b")
    safe_label = _format_date_iso(path.get("safe_through"), evidence)
    reward = float(chosen.get("reward", 0) or 0)
    risk = float(chosen.get("stress_risk", 0) or 0)
    rr = reward / risk if risk > 0 else 99.0
    reason = _nonarrival_reason(base, evidence, strategy, chosen, safe_label, expiry_label)
    invalidation = None
    if strategy == "SHORT_PE": invalidation = legs[0].get("strike")
    elif strategy == "SHORT_CE": invalidation = legs[0].get("strike")

    return {
        "kind": "NEW", "action": "ENTER", "status": "ACTIVE",
        "direction": direction, "strategy_type": strategy, "legs": legs,
        "entry_reference": _f(base.get("price")), "target": None, "invalidation": invalidation,
        "expected_eta": f"protected through {safe_label}; expiry {expiry_label}",
        "risk": round(risk, 2), "reward": round(reward, 2), "rr": round(rr, 2),
        "reason": reason, "fresh_entry_now": True,
        "expression_style": "NONARRIVAL", "relevance_focus": focus,
        "path_snapshot": path, "nonarrival_snapshot": chosen,
        "expected_behavior": "sold strike remains outside the composite arrival path while premium decays and the route barriers remain intact",
        "behavior_health": "ON_PLAN", "behavior_bad_streak": 0,
    }


def _nonarrival_reason(base: Dict[str, Any], evidence: Dict[str, Any], strategy: str, chosen: Dict[str, Any], safe_label: str, expiry_label: str) -> str:
    path = chosen.get("path", {}) or {}
    hurdles = int(path.get("hurdle_count", 0) or 0)
    clusters = len(path.get("oi_clusters", []) or [])
    side = "downside" if strategy == "SHORT_PE" else "upside" if strategy == "SHORT_CE" else "both tails"
    parts: List[str] = []
    if hurdles:
        parts.append(f"{hurdles} active price barriers slow the {side} route")
    if clusters:
        parts.append("intermediate option positioning adds path friction")
    if not parts:
        parts.append(f"the {side} path remains slow enough for the selected expiry")
    return "; ".join(parts)

def _new_entry_plan(base: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    old = dict(_new_entry_plan_sb37(base, evidence) or {})
    old_st = _u(old.get("strategy_type"))
    # Keep qualified directional entries unchanged.  Replace only legacy/simple
    # premium-selling decisions, or fill a no-trade state with the richer scan.
    if _u(old.get("kind")) == "NEW" and old_st not in {"SHORT_PE", "SHORT_CE", "SHORT_STRANGLE"}:
        old["relevance_focus"] = _current_focus(base, evidence)
        old.setdefault("expected_behavior", "price should make directional progress without repeatedly surrendering the entry-side structure")
        old.setdefault("behavior_health", "ON_PLAN")
        old.setdefault("behavior_bad_streak", 0)
        return old
    richer = _nonarrival_final_plan(base, evidence)
    if richer:
        return richer
    old["relevance_focus"] = _current_focus(base, evidence)
    return old


def _behavior_health(base: Dict[str, Any], evidence: Dict[str, Any], trade: Dict[str, Any]) -> Dict[str, Any]:
    direction = _u(trade.get("direction"))
    if direction not in {"BULLISH", "BEARISH"}:
        return {"health": "ON_PLAN", "bad_streak": 0, "score": 0, "reason": "campaign is not directional"}
    meta = _campaign_meta(trade)
    prev_streak = int(_f(meta.get("behavior_bad_streak")) or 0)
    price = _f(base.get("price"))
    entry = _f(trade.get("entry_reference"))
    atr = _f((_source_map(evidence).get("AURORA", {}) or {}).get("eta_current_atr")) or 0.0
    adverse = 0
    reasons: List[str] = []
    opposite = _opposite(direction)
    if _u(base.get("exec_of")) == opposite:
        adverse += 1; reasons.append("short-term pressure is working against the trade")
    aur = _u((base.get("aurora", {}) or {}).get("state"))
    if (direction == "BULLISH" and aur == "RED") or (direction == "BEARISH" and aur == "GREEN"):
        adverse += 1; reasons.append("momentum is active against the trade")
    liq_read = _u((base.get("liquidity", {}) or {}).get("directional_read"))
    if liq_read == opposite:
        adverse += 1; reasons.append("recent liquidity behaviour is adverse")
    if price is not None and entry is not None and atr > 0:
        signed = price - entry if direction == "BULLISH" else entry - price
        if signed < -0.5 * atr:
            adverse += 1; reasons.append("price has moved materially against the entry without enough favorable progress")
        elif signed > 0.5 * atr:
            adverse = max(0, adverse - 1)
    prog = base.get("battlefield_progression", {}) or {}
    if bool(prog.get("defender_failed")):
        adverse += 2; reasons.append("the protecting structure has failed")

    if adverse >= 4:
        health = "THESIS_DAMAGED"
    elif adverse >= 2:
        health = "FAILURE_TO_PERFORM"
    elif adverse == 1:
        health = "SLOW_BUT_VALID"
    else:
        health = "ON_PLAN"
    bad_streak = prev_streak + 1 if health in {"FAILURE_TO_PERFORM", "THESIS_DAMAGED"} else 0
    return {"health": health, "bad_streak": bad_streak, "score": adverse, "reason": "; ".join(reasons[:3])}


def _manage_existing(base: Dict[str, Any], evidence: Dict[str, Any], open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    old = dict(_manage_existing_sb37(base, evidence, open_trades) or {})
    if not open_trades:
        return old
    tid = _s(old.get("trade_id"))
    trade = next((dict(x) for x in open_trades if _s(x.get("trade_id")) == tid), dict(open_trades[0]))
    health = _behavior_health(base, evidence, trade)
    old["behavior_health"] = health["health"]
    old["behavior_bad_streak"] = health["bad_streak"]
    old["expected_behavior"] = _campaign_meta(trade).get("expected_behavior") or "the campaign should continue converting its thesis into price progress without persistent adverse pressure"
    old["relevance_focus"] = _current_focus(base, evidence)

    # Hard invalidation/opposite-thesis exits from the existing manager remain
    # immediate and authoritative.
    if _u(old.get("action")) == "EXIT":
        return old

    strategy = _u(trade.get("strategy_type"))
    direction = _u(trade.get("direction"))
    current_legs = _active_campaign_legs(trade)

    # Existing naked short options are re-evaluated on the same path model as a
    # fresh trade.  One bad scan is a warning; persistent path deterioration is
    # required before SuperBrain removes the exposure early.
    if strategy in {"SHORT_PE", "SHORT_CE"}:
        opt = "PE" if strategy == "SHORT_PE" else "CE"
        sold = next((x for x in current_legs if _u(x.get("side")) == "SELL" and _u(x.get("option")) == opt), {})
        strike = _f(sold.get("strike"))
        if strike is not None:
            path = _composite_path(base, evidence, strike, opt)
            old["path_snapshot"] = path
            protected = bool(path.get("protected_through_expiry"))
            meta = _campaign_meta(trade); prev = int(_f((meta.get("nonarrival_snapshot", {}) or {}).get("nonarrival_bad_streak") if isinstance(meta.get("nonarrival_snapshot", {}), dict) else 0) or 0)
            bad = 0 if protected else prev + 1
            old["nonarrival_snapshot"] = {"path": path, "nonarrival_bad_streak": bad}
            if not protected and bad >= 2:
                old.update({
                    "action": "EXIT", "status": "CLOSED", "campaign_operation": "EXIT_CAMPAIGN",
                    "close_leg_ids": _leg_ids(current_legs),
                    "reason": "the sold strike is no longer protected through expiry on two consecutive scans, so the non-arrival thesis has failed",
                    "fresh_entry_now": False,
                })
                return old

    # Pre-emptive management for directional expressions: do not wait for the
    # hard invalidation when several independent observations show persistent
    # failure-to-perform.  First reduce/rotate; full early exit needs stronger
    # multi-scan damage.
    if strategy in {"FUTURES_LONG", "FUTURES_SHORT", "LONG_CE", "LONG_PE"}:
        if health["health"] == "THESIS_DAMAGED" and health["bad_streak"] >= 3:
            old.update({
                "action": "EXIT", "status": "CLOSED", "campaign_operation": "EXIT_CAMPAIGN",
                "close_leg_ids": _leg_ids(current_legs),
                "reason": "the trade has failed to produce the expected progress across repeated scans while adverse pressure and momentum have persisted",
                "fresh_entry_now": False,
            })
            return old
        if health["health"] in {"FAILURE_TO_PERFORM", "THESIS_DAMAGED"} and health["bad_streak"] >= 2 and _u(old.get("action")) == "HOLD":
            # When the thesis remains directionally relevant, a protected naked
            # opposite-side option can replace full delta; otherwise simply reduce.
            expr = _directional_expression(base, evidence, direction, bool((base.get("poi", {}) or {}).get("qualified"))) if direction in {"BULLISH", "BEARISH"} else {}
            candidate = _u(expr.get("strategy_type"))
            if strategy in {"FUTURES_LONG", "FUTURES_SHORT"} and candidate in {"SHORT_PE", "SHORT_CE"}:
                old.update({
                    "action": "REDUCE", "status": "ACTIVE", "campaign_operation": "ROTATE",
                    "strategy_type": candidate, "next_strategy_type": candidate,
                    "close_leg_ids": _leg_ids(current_legs), "new_legs": list(expr.get("legs", []) or []),
                    "reason": "directional follow-through has failed across repeated scans, while the underlying thesis still supports a lower-delta non-arrival expression",
                    "fresh_entry_now": False,
                })
            else:
                old.update({
                    "action": "REDUCE", "status": "ACTIVE",
                    "reason": "the trade is not converting its thesis into price progress across repeated scans; exposure is being reduced before hard invalidation",
                    "fresh_entry_now": False,
                })
            return old
    return old


def _nearest_relevant_zone(base: Dict[str, Any], side: str) -> Optional[Tuple[float, float]]:
    candidates: List[Tuple[float, float, float]] = []
    for h in base.get("hurdles", []) or []:
        if not isinstance(h, dict) or _u(h.get("side")) != _u(side):
            continue
        lo, hi = _f(h.get("low")), _f(h.get("high"))
        if lo is None or hi is None:
            continue
        dist = _f(h.get("distance"))
        candidates.append((dist if dist is not None else 1e99, lo, hi))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]
    # Strategic boundary is a fallback only when no nearer qualified zone exists.
    key = "defender" if _u(side) == "DEMAND" else "challenger"
    z = base.get(key, {}) or {}
    if _u(z.get("side")) == _u(side):
        lo, hi = _f(z.get("low")), _f(z.get("high"))
        if lo is not None and hi is not None:
            return lo, hi
    return None


def _relevant_setup_text(base: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    focus = _current_focus(base, evidence)
    if focus == "BULLISH":
        zone = _nearest_relevant_zone(base, "DEMAND")
        return f"Watching the long side from {_fmt(zone[0])}–{_fmt(zone[1])}; confirmation is not ready." if zone else "Watching the long side; confirmation is not ready."
    if focus == "BEARISH":
        zone = _nearest_relevant_zone(base, "SUPPLY")
        return f"Watching the short side from {_fmt(zone[0])}–{_fmt(zone[1])}; confirmation is not ready." if zone else "Watching the short side; confirmation is not ready."
    return "No directional setup is ready."


def _natural_market_sentence(base: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    focus = _current_focus(base, evidence)
    price = _f(base.get("price"))
    if focus == "BULLISH":
        zone = _nearest_relevant_zone(base, "DEMAND")
        return f"Price {_fmt(price)} is holding active support at {_fmt(zone[0])}–{_fmt(zone[1])}." if zone else f"Price {_fmt(price)} is holding the stronger support side."
    if focus == "BEARISH":
        zone = _nearest_relevant_zone(base, "SUPPLY")
        return f"Price {_fmt(price)} is pressing active resistance at {_fmt(zone[0])}–{_fmt(zone[1])}." if zone else f"Price {_fmt(price)} is pressing the stronger resistance side."
    return f"Price {_fmt(price)} remains balanced between active support and resistance."

def _fmt_money(v: Any) -> str:
    x = _f(v)
    if x is None:
        return "?"
    return f"₹{x:,.0f}" if abs(x) >= 100 else f"₹{x:,.2f}"


def _narrative(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> str:
    kind = _u(plan.get("kind")); action = _u(plan.get("action")); st = _u(plan.get("strategy_type"))
    price = _f(base.get("price"))

    if kind == "NEW":
        if st in {"SHORT_PE", "SHORT_CE", "SHORT_STRANGLE"}:
            legs = plan.get("legs", []) or []
            if st == "SHORT_PE":
                leg = legs[0] if legs else {}; desc = f"{_fmt(leg.get('strike'))} PE short at about {_fmt(leg.get('premium'))}"
            elif st == "SHORT_CE":
                leg = legs[0] if legs else {}; desc = f"{_fmt(leg.get('strike'))} CE short at about {_fmt(leg.get('premium'))}"
            else:
                ce = next((x for x in legs if _u(x.get("option")) == "CE"), {}); pe = next((x for x in legs if _u(x.get("option")) == "PE"), {})
                desc = f"{_fmt(ce.get('strike'))} CE / {_fmt(pe.get('strike'))} PE short strangle"
            path = plan.get("path_snapshot", {}) or {}
            safe = _format_date_iso(path.get("safe_through"), evidence)
            expiry = _expiry_datetime(evidence)
            expiry_text = expiry.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b") if expiry else "?"
            protection = "The sold strikes are" if st == "SHORT_STRANGLE" else "The sold strike is"
            return (
                f"**SuperBrain is taking {desc} now.** {protection} protected through {safe} against {expiry_text} expiry. "
                f"Expected premium reward {_fmt_money(plan.get('reward'))}; estimated stress risk {_fmt_money(plan.get('risk'))}. "
                f"{_sentence_case(plan.get('reason'))}."
            )
        return (
            f"**SuperBrain is taking {_strategy_text(plan)} now at spot {_fmt(price)}.** "
            f"{_sentence_case(plan.get('reason'))}. Target {_fmt(plan.get('target'))}; hard invalidation {_fmt(plan.get('invalidation'))}."
        )

    if kind == "MANAGE":
        current = plan.get("current_legs", []) or plan.get("legs", []) or []
        exposure = _expression_exposure_text(current)
        reason = _sentence_case(plan.get("reason"))
        if action == "HOLD":
            return f"**Holding {exposure}.** {reason}."
        if action == "ADD":
            return f"**Adding to {exposure}.** {reason}."
        if action == "REDUCE":
            if _u(plan.get("campaign_operation")) == "ROTATE":
                new_desc = _strategy_from_trade_plan({"strategy_type": plan.get("next_strategy_type") or plan.get("strategy_type"), "legs": plan.get("new_legs", []) or []})
                return f"**Reducing {exposure} and rotating into {new_desc}.** {reason}."
            return f"**Reducing {exposure}.** {reason}."
        if action == "EXIT":
            return f"**Closing {exposure} now at spot {_fmt(price)}.** {reason}."
        return f"**Managing {exposure}.** {reason}."

    focus = _current_focus(base, evidence)
    if focus == "BULLISH":
        zone = _nearest_relevant_zone(base, "DEMAND")
        if zone:
            return f"**No exposure now.** Price {_fmt(price)} is holding support at {_fmt(zone[0])}–{_fmt(zone[1])}; long confirmation is not ready."
        return f"**No exposure now.** Price {_fmt(price)} is holding the stronger support side; long confirmation is not ready."
    if focus == "BEARISH":
        zone = _nearest_relevant_zone(base, "SUPPLY")
        if zone:
            return f"**No exposure now.** Price {_fmt(price)} is pressing resistance at {_fmt(zone[0])}–{_fmt(zone[1])}; short confirmation is not ready."
        return f"**No exposure now.** Price {_fmt(price)} is pressing the stronger resistance side; short confirmation is not ready."
    return f"**No exposure now.** Price {_fmt(price)} remains balanced; no setup is ready."


def _material_signature(base: Dict[str, Any], plan: Dict[str, Any], progression: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    sig = dict(_material_signature_sb37(base, plan, progression, evidence) or {})
    sig["relevance_focus"] = _u(plan.get("relevance_focus") or _current_focus(base, evidence))
    sig["behavior_health"] = _u(plan.get("behavior_health"))
    ns = plan.get("nonarrival_snapshot", {}) or {}
    if ns:
        sig["nonarrival"] = {
            "strike": _f(ns.get("strike")), "option": _u(ns.get("option")),
            "safe_through": _s((ns.get("path", {}) or {}).get("safe_through")),
            "reward": round(_f(ns.get("reward")) or 0.0, 2),
            "risk": round(_f(ns.get("stress_risk")) or 0.0, 2),
        }
    return sig


def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    base = _analyze_market_sb37(evidence, previous_evidence, open_trades=open_trades)
    plan = base.get("trade_plan", {}) or {}
    base["reasoner_version"] = REASONER_VERSION
    base["trade_plan"] = plan
    base["relevance_focus"] = _u(plan.get("relevance_focus") or _current_focus(base, evidence))
    # Visible commentary is deliberately relevance-filtered and natural.  The
    # full two-sided evidence stays internal for decision-making.
    base["waiting_for"] = [_relevant_setup_text(base, evidence)] if _u(plan.get("kind")) not in {"NEW", "MANAGE"} else []
    base["narrative"] = _narrative(base, evidence, plan)
    return base

# =============================================================================
# SB3.9 market-view commentary / explicit non-arrival conclusion overrides
# =============================================================================
REASONER_VERSION = "SB3.9_MARKET_VIEW_EXPECTED_BEHAVIOR"


def _sb39_zone_text(low: Any, high: Any) -> str:
    return f"{_fmt(low)}–{_fmt(high)}"


def _sb39_battlefield_sentence(base: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    price = _f(base.get("price"))
    macro = _u(base.get("macro"))
    defender = base.get("defender", {}) or {}
    challenger = base.get("challenger", {}) or {}
    edge = _source_map(evidence).get("EDGE", {}) or {}
    raw = edge.get("raw_json") if isinstance(edge.get("raw_json"), dict) else {}
    pct = _f(raw.get("battlefield_range_position_pct"))
    divider = _f(raw.get("battlefield_divider"))

    loc = ""
    if pct is not None:
        if pct <= 20:
            loc = "near the lower end of the battlefield"
        elif pct >= 80:
            loc = "near the upper end of the battlefield"
        elif pct < 45:
            loc = "in the lower half of the battlefield"
        elif pct > 55:
            loc = "in the upper half of the battlefield"
        else:
            loc = "near the middle of the battlefield"
    elif _u(base.get("battlefield_half")) == "DEFENDER_HALF":
        loc = "on the defended side of the battlefield"
    elif _u(base.get("battlefield_half")) == "CHALLENGER_HALF":
        loc = "on the opposing side of the battlefield"
    else:
        loc = "inside the active battlefield"

    context: List[str] = []
    if _zone_valid(defender):
        noun = "demand" if _u(defender.get("side")) == "DEMAND" else "supply"
        context.append(f"major {noun} is {_sb39_zone_text(defender.get('low'), defender.get('high'))}")
    if _zone_valid(challenger):
        noun = "supply" if _u(challenger.get("side")) == "SUPPLY" else "demand"
        context.append(f"opposing {noun} is {_sb39_zone_text(challenger.get('low'), challenger.get('high'))}")
    suffix = "; " + ", while ".join(context) if context else ""
    macro_text = "bullish" if macro == "BULLISH" else "bearish" if macro == "BEARISH" else "balanced"
    div_text = f" around a {_fmt(divider)} battlefield divider" if divider is not None and not context else ""
    return f"Price {_fmt(price)} is {loc} in a {macro_text} strategic map{suffix}{div_text}."


def _sb39_action_zone_sentence(base: Dict[str, Any]) -> str:
    price = _f(base.get("price"))
    hurdles = [h for h in (base.get("hurdles", []) or []) if isinstance(h, dict)]
    if not hurdles:
        poi = base.get("poi", {}) or {}
        if poi and _f(poi.get("low")) is not None and _f(poi.get("high")) is not None:
            side = "demand" if _u(poi.get("side")) == "DEMAND" else "supply"
            return f"The nearest actionable {side} area is {_sb39_zone_text(poi.get('low'), poi.get('high'))}; it is {_s(poi.get('status')).replace('_', ' ').lower() or 'active'}."
        return "No nearby qualified action zone is close enough to control the immediate decision."

    # Mention at most the two nearest useful zones, preserving relevance without
    # turning the commentary into an inventory of every historical hurdle.
    ranked = sorted(hurdles, key=lambda h: (_f(h.get("distance")) if _f(h.get("distance")) is not None else 1e99))[:2]
    parts: List[str] = []
    for h in ranked:
        side = "demand" if _u(h.get("side")) == "DEMAND" else "supply"
        status = _s(h.get("status")).replace("_", " ").lower()
        strength = _s(h.get("strength")).lower()
        
        status = {"touched reduced": "previously tested", "active fresh": "fresh", "active": "active"}.get(status, status)
        descriptor = " ".join(x for x in (strength, status) if x).strip()
        z = _sb39_zone_text(h.get("low"), h.get("high"))
        if price is not None and _f(h.get("low")) is not None and _f(h.get("high")) is not None:
            interacting = _f(h.get("low")) <= price <= _f(h.get("high"))
        else:
            interacting = False
        if interacting:
            parts.append(f"price is interacting with {descriptor + ' ' if descriptor else ''}{side} at {z}")
        else:
            parts.append(f"{descriptor + ' ' if descriptor else ''}{side} at {z} is the next relevant action zone")
    return "; ".join(parts).capitalize() + "."


def _sb39_momentum_sentence(base: Dict[str, Any]) -> str:
    aur = base.get("aurora", {}) or {}
    return _momentum_sentence(_u(aur.get("transition")), _u(aur.get("state")), _u(base.get("exec_of")))


def _sb39_structure_sentence(base: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    fork = base.get("fork", {}) or {}
    structure = _source_map(evidence).get("STRUCTURE", {}) or {}
    trend = _u(structure.get("structure_trend"))
    if not fork.get("valid") and not trend:
        return "The current structural channel is not providing a usable directional read."
    t = "upward" if trend == "UP" else "downward" if trend == "DOWN" else "mixed"
    pos = _u(fork.get("position"))
    pos_map = {
        "ABOVE_UPPER_2SD": "price is stretched above the upper extreme",
        "UPPER_1SD_TO_2SD": "price is in the upper extension area",
        "MEDIAN_TO_UPPER_1SD": "price is above the channel median",
        "LOWER_1SD_TO_MEDIAN": "price is below the channel median",
        "LOWER_2SD_TO_1SD": "price is in the lower extension area",
        "BELOW_LOWER_2SD": "price is stretched below the lower extreme",
    }
    detail = pos_map.get(pos, "price is inside the active structural channel")
    if fork.get("reclaimed_2sd"):
        side = "lower" if _u(fork.get("reclaim_side")) == "LOWER" else "upper"
        detail += f" after reclaiming the {side} extreme"
    return f"The broader swing structure is {t}; {detail}."


def _sb39_liquidity_sentence(base: Dict[str, Any]) -> str:
    liq = base.get("liquidity", {}) or {}
    above, below = _f(liq.get("above_level")), _f(liq.get("below_level"))
    event, level, meaning = _u(liq.get("event")), _f(liq.get("level")), _s(liq.get("meaning"))
    parts: List[str] = []
    if above is not None:
        parts.append(f"upside liquidity sits near {_fmt(above)}")
    if below is not None:
        parts.append(f"downside liquidity sits near {_fmt(below)}")
    if event and level is not None and meaning:
        parts.append(f"the latest confirmed event at {_fmt(level)} shows {meaning}")
    if not parts:
        return "Liquidity is not materially changing the immediate path on this scan."
    return "; ".join(parts).capitalize() + "."


def _sb39_extract_option_note(odme: Dict[str, Any]) -> str:
    text = _s(odme.get("commentary"))
    if not text:
        return ""
    # ODME commentary is structured in labelled sentences.  Pull only the one
    # genuinely decision-useful warning/support sentence instead of replaying it.
    chunks = [x.strip() for x in re.split(r"(?=\b(?:Important|CE Action|PE Action|Final Action):)", text) if x.strip()]
    important = next((x for x in chunks if x.lower().startswith("important:")), "")
    if important:
        return important.split("Important:", 1)[-1].strip().rstrip(".")
    return ""


def _sb39_options_sentence(evidence: Dict[str, Any]) -> str:
    if _s(evidence.get("mode")) != "TV + ODME":
        return "Fresh options positioning is unavailable on this scan, so no option-based conclusion is being used."
    odme = evidence.get("odme", {}) or {}
    if not odme:
        return "Fresh options positioning is unavailable on this scan, so no option-based conclusion is being used."
    poc = _f(odme.get("option_poc"))
    ce = _f(odme.get("active_ce_wall") or odme.get("ce_wall"))
    pe = _f(odme.get("active_pe_wall") or odme.get("pe_wall"))
    safe_ce = _f(odme.get("safer_sell_ce"))
    safe_pe = _f(odme.get("safer_sell_pe"))
    tilt = _s(odme.get("odme_tilt")) or "mixed"
    range_score = _f(odme.get("range_score"))
    expansion_score = _f(odme.get("expansion_score"))
    bits: List[str] = []
    if poc is not None:
        bits.append(f"POC {_fmt(poc)}")
    if ce is not None:
        bits.append(f"call wall {_fmt(ce)}")
    if pe is not None:
        bits.append(f"put wall {_fmt(pe)}")
    sentence = f"Options are {tilt.lower()}" + (" with " + ", ".join(bits) if bits else "") + "."
    if range_score is not None and expansion_score is not None:
        if expansion_score >= range_score + 15:
            sentence += " Positioning is leaning more toward expansion than clean containment."
        elif range_score >= expansion_score + 15:
            sentence += " Positioning is more compatible with containment/theta than expansion."
        else:
            sentence += " Range and expansion evidence remain mixed."
    safe: List[str] = []
    if safe_ce is not None:
        safe.append(f"safer call-sale boundary {_fmt(safe_ce)}")
    if safe_pe is not None:
        safe.append(f"safer put-sale boundary {_fmt(safe_pe)}")
    if safe:
        sentence += " " + ", ".join(safe).capitalize() + "."
    note = _sb39_extract_option_note(odme)
    if note:
        sentence += " " + _sentence_case(note) + "."
    return sentence


def _sb39_candidate_failure_reason(candidate: Optional[Dict[str, Any]], side: str) -> str:
    if not candidate:
        return f"no usable listed {side} candidate is available beyond the safer boundary"
    path = candidate.get("path", {}) or {}
    if not candidate.get("valid"):
        return f"the {side} path cannot be modelled reliably from the current data"
    if not path.get("protected_through_expiry"):
        return f"the {side} strike is not protected through expiry on the composite path"
    if abs(float(candidate.get("delta", 0) or 0)) > 0.30:
        return f"the {side} strike carries too much directional delta for a non-arrival sale"
    reward = float(candidate.get("reward", 0) or 0)
    stress = float(candidate.get("stress_risk", 0) or 0)
    if reward <= 0:
        return f"the {side} strike does not offer usable executable premium"
    if stress > 0 and reward / stress < 0.25:
        return f"the {side} premium is too small relative to estimated stress risk"
    return f"the {side} candidate does not pass the current sale filters"


def _sb39_first_evaluated_candidate(base: Dict[str, Any], evidence: Dict[str, Any], option: str) -> Optional[Dict[str, Any]]:
    odme = evidence.get("odme", {}) or {}
    if not odme or _s(evidence.get("mode")) != "TV + ODME":
        return None
    if not _sale_action_ok(odme, option):
        return None
    rows = _candidate_rows_for_side(base, evidence, option)
    if not rows:
        return None
    return _evaluate_short_option(base, evidence, option, rows[0])


def _sb39_nonarrival_assessment(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    if _s(evidence.get("mode")) != "TV + ODME":
        return {"decision": "WAIT", "text": "Non-arrival cannot be concluded because a fresh live option-chain assessment is unavailable."}

    st = _u(plan.get("strategy_type"))
    if _u(plan.get("kind")) == "NEW" and st in {"SHORT_PE", "SHORT_CE", "SHORT_STRANGLE"}:
        legs = plan.get("legs", []) or []
        if st == "SHORT_STRANGLE":
            ce = next((x for x in legs if _u(x.get("option")) == "CE"), {})
            pe = next((x for x in legs if _u(x.get("option")) == "PE"), {})
            desc = f"sell {_fmt(ce.get('strike'))} CE and {_fmt(pe.get('strike'))} PE"
        else:
            leg = legs[0] if legs else {}
            desc = f"sell {_fmt(leg.get('strike'))} {_u(leg.get('option'))}"
        path = plan.get("path_snapshot", {}) or {}
        safe = _format_date_iso(path.get("safe_through"), evidence)
        expiry = _expiry_datetime(evidence)
        exp = expiry.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b") if expiry else "?"
        return {
            "decision": "TAKE",
            "text": f"TAKE — {desc}. The selected strike protection remains intact through {safe} against {exp} expiry; expected premium reward {_fmt_money(plan.get('reward'))} versus estimated stress risk {_fmt_money(plan.get('risk'))}. {_sentence_case(plan.get('reason'))}.",
        }

    odme = evidence.get("odme", {}) or {}
    pe_sale_ok = _sale_action_ok(odme, "PE")
    ce_sale_ok = _sale_action_ok(odme, "CE")
    pe_best = _best_side_candidate(base, evidence, "PE") if pe_sale_ok else None
    ce_best = _best_side_candidate(base, evidence, "CE") if ce_sale_ok else None
    # If a good candidate exists but another directional expression was selected,
    # state that it was evaluated and deliberately not used as the primary trade.
    if pe_best or ce_best:
        candidates = [x for x in (pe_best, ce_best) if x]
        best = max(candidates, key=lambda x: float(x.get("score", 0) or 0))
        side = _u(best.get("option"))
        safe = _format_date_iso((best.get("path", {}) or {}).get("safe_through"), evidence)
        selected = _strategy_text(plan) if _u(plan.get("kind")) == "NEW" else "the existing campaign expression" if _u(plan.get("kind")) == "MANAGE" else "the current market decision"
        txt = (
            f"PASS as additional fresh exposure — {_fmt(best.get('strike'))} {side} is individually protected through {safe}, "
            f"but {selected} is preferred on this scan. Premium reward {_fmt_money(best.get('reward'))} versus estimated stress risk {_fmt_money(best.get('stress_risk'))}."
        )
        return {"decision": "PASS", "text": txt}

    pe_raw = _sb39_first_evaluated_candidate(base, evidence, "PE") if pe_sale_ok else None
    ce_raw = _sb39_first_evaluated_candidate(base, evidence, "CE") if ce_sale_ok else None
    reasons: List[str] = []
    if pe_sale_ok:
        reasons.append(_sb39_candidate_failure_reason(pe_raw, "PE"))
    else:
        reasons.append("put selling is not approved by the current option positioning")
    if ce_sale_ok:
        reasons.append(_sb39_candidate_failure_reason(ce_raw, "CE"))
    else:
        reasons.append("call selling is not approved by the current option positioning")
    return {"decision": "PASS", "text": "PASS — no naked non-arrival entry now because " + "; ".join(reasons) + "."}


def _sb39_behavior_sentence(plan: Dict[str, Any]) -> str:
    health = _u(plan.get("behavior_health"))
    expected = _s(plan.get("expected_behavior"))
    reason = _s(plan.get("reason"))
    if not health and not expected:
        return ""
    label = health or "ON_PLAN"
    if expected and reason:
        return f"Expected vs actual: {label}. Expected: {expected}. Actual: {_sentence_case(reason)}."
    if expected:
        return f"Expected vs actual: {label}. Expected: {expected}."
    return f"Expected vs actual: {label}. {_sentence_case(reason)}."


def _sb39_decision_sentence(base: Dict[str, Any], plan: Dict[str, Any]) -> str:
    kind, action, st = _u(plan.get("kind")), _u(plan.get("action")), _u(plan.get("strategy_type"))
    price = _f(base.get("price"))
    if kind == "NEW":
        if st in {"SHORT_PE", "SHORT_CE", "SHORT_STRANGLE"}:
            return "Decision: TAKE the non-arrival exposure described above."
        return f"Decision: TAKE {_strategy_text(plan)} at spot {_fmt(price)}. {_sentence_case(plan.get('reason'))}."
    if kind == "MANAGE":
        current = plan.get("current_legs", []) or plan.get("legs", []) or []
        exposure = _expression_exposure_text(current)
        if action == "HOLD":
            return f"Decision: HOLD {exposure}."
        if action == "ADD":
            return f"Decision: ADD to {exposure}."
        if action == "REDUCE" and _u(plan.get("campaign_operation")) == "ROTATE":
            new_desc = _strategy_from_trade_plan({"strategy_type": plan.get("next_strategy_type") or plan.get("strategy_type"), "legs": plan.get("new_legs", []) or []})
            return f"Decision: REDUCE {exposure} and CHANGE EXPRESSION into {new_desc}."
        if action == "REDUCE":
            return f"Decision: REDUCE {exposure}."
        if action == "EXIT":
            return f"Decision: EXIT {exposure} now."
        return f"Decision: MANAGE {exposure}."
    focus = _u(plan.get("relevance_focus") or base.get("relevance_focus"))
    if focus not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        focus = _current_focus(base, {})
    if focus == "BULLISH":
        return "Decision: WAIT for long-side confirmation; location is constructive but behaviour has not released the trade."
    if focus == "BEARISH":
        return "Decision: WAIT for short-side confirmation; location is constructive but behaviour has not released the trade."
    return "Decision: NO DIRECTIONAL EXPOSURE; neither side has released a clean trade."


def _narrative(base: Dict[str, Any], evidence: Dict[str, Any], plan: Dict[str, Any]) -> str:
    # Every scan follows the same human reading order.  Relevance controls depth,
    # not whether SuperBrain explains the market.
    sections = [
        f"**Battlefield:** {_sb39_battlefield_sentence(base, evidence)}",
        f"**Action zones:** {_sb39_action_zone_sentence(base)}",
        f"**Momentum:** {_sb39_momentum_sentence(base)}",
        f"**Structure:** {_sb39_structure_sentence(base, evidence)}",
        f"**Liquidity:** {_sb39_liquidity_sentence(base)}",
        f"**Options:** {_sb39_options_sentence(evidence)}",
    ]
    na = _sb39_nonarrival_assessment(base, evidence, plan)
    sections.append(f"**Non-arrival:** {na.get('text', '')}")
    behavior = _sb39_behavior_sentence(plan) if _u(plan.get("kind")) == "MANAGE" else ""
    if behavior:
        sections.append(f"**Trade behaviour:** {behavior}")
    sections.append(f"**Decision:** {_sb39_decision_sentence(base, plan).replace('Decision: ', '', 1)}")
    return "\n\n".join(sections)


def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    base = _analyze_market_sb37(evidence, previous_evidence, open_trades=open_trades)
    plan = base.get("trade_plan", {}) or {}
    base["reasoner_version"] = REASONER_VERSION
    base["trade_plan"] = plan
    base["relevance_focus"] = _u(plan.get("relevance_focus") or _current_focus(base, evidence))
    base["nonarrival_assessment"] = _sb39_nonarrival_assessment(base, evidence, plan)
    base["waiting_for"] = [_relevant_setup_text(base, evidence)] if _u(plan.get("kind")) not in {"NEW", "MANAGE"} else []
    base["narrative"] = _narrative(base, evidence, plan)
    return base
