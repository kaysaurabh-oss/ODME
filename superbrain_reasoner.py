from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

REASONER_VERSION = "SB3.4_FINAL_MARKET_ACTION_ODME_THETA"


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
            return f"If price pushes through {_fmt(challenger.get('high'))} and then remains accepted above that broader resistance on a later confirmed bar, the current short-location idea should be abandoned rather than repeatedly selling the old zone."
        demand = next((h for h in hurdles if h.get("side") == "DEMAND"), None)
        if demand and _zone_valid(challenger) and challenger.get("side") == "DEMAND":
            return f"If price pushes through {_fmt(challenger.get('low'))} and then remains accepted below that broader support on a later confirmed bar, the current long-location idea should be abandoned rather than repeatedly buying the old zone."
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
REASONER_VERSION = "SB3.4_FINAL_MARKET_ACTION_ODME_THETA"


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
            sent += " " + _s(plan.get("reason")).capitalize() + "."
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
        sent += " " + _s(plan.get("reason")).capitalize() + "."
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
