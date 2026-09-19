from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

REASONER_VERSION = "SB3.1_LOCKED_RULES_CONTEXT_FIRST"


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
        directional, meaning = "BULLISH", "buy-side liquidity was crossed and price remained above it"
    elif event.startswith("SELL_") and event.endswith("_RUN"):
        directional, meaning = "BEARISH", "sell-side liquidity was crossed and price remained below it"
    elif event.startswith("BUY_") and event.endswith("_SWEEP"):
        directional, meaning = "BEARISH", "buy-side liquidity was swept and reclaimed back below"
    elif event.startswith("SELL_") and event.endswith("_SWEEP"):
        directional, meaning = "BULLISH", "sell-side liquidity was swept and reclaimed back above"
    return {"event": event, "level": level, "confirm_time": confirm_time, "is_new": is_new, "directional_read": directional, "meaning": meaning}


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


def _lines(
    price: Optional[float], macro: str, defender: Dict[str, Any], challenger: Dict[str, Any], battlefield_half: str,
    aurora: Dict[str, str], aurora_transition: str, exec_dir: str, fork: Dict[str, Any], liquidity: Dict[str, Any],
    poi: Optional[Dict[str, Any]], long_gate: Dict[str, Any], short_gate: Dict[str, Any], posture: str,
) -> List[str]:
    lines: List[str] = []
    ptxt = _fmt(price)

    # Battlefield first: this is the primary context.
    if _zone_valid(defender):
        z = f"{_fmt(defender['low'])}–{_fmt(defender['high'])}"
        if defender.get("transition") == "HELD_AND_REJECTED":
            lines.append(f"{defender['tf']} {defender['side'].lower()} Defender {z} held; price is moving away from it from {ptxt}.")
        elif defender.get("transition") == "MOVING_AWAY":
            lines.append(f"Defender {z} remains respected and price is moving away from it; location is supportive for the {macro.lower()} side.")
        elif defender.get("condition") == "UNDER_TEST":
            lines.append(f"Price is testing the {defender['tf']} Defender at {z}; this adds caution to location quality but does not override the locked trade rules by itself.")
        elif defender.get("transition") in {"FRESH_CONFIRMED_BREACH", "BREACH_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            lines.append(f"Defender {z} has been breached on a confirmed EDGE close; do not rely on the old {macro.lower()} battlefield until it reclaims or EDGE reconfigures.")
        elif defender.get("transition") == "ACCEPTED_BEYOND":
            lines.append(f"Defender {z} remains broken on a later confirmed EDGE bar; the old battlefield premise is invalid until EDGE rebuilds it.")
        elif defender.get("transition") == "RECLAIMED":
            lines.append(f"Defender {z} has reclaimed after a prior breach; support/resistance is recovering but should be revalidated before adding risk.")

    if _zone_valid(challenger):
        z = f"{_fmt(challenger['low'])}–{_fmt(challenger['high'])}"
        if challenger.get("condition") == "UNDER_TEST":
            lines.append(f"Price is inside the {challenger['tf']} Challenger at {z}; opposing control is being tested, so continuation is not yet clean.")
        elif challenger.get("transition") in {"FRESH_CONFIRMED_CROSS", "CROSS_PRESENT_AWAIT_NEXT_EDGE_BAR"}:
            lines.append(f"Challenger {z} has just been crossed; SuperBrain will not chase the first break and waits for acceptance or reclaim.")
        elif challenger.get("transition") == "ACCEPTED_BEYOND":
            lines.append(f"Challenger {z} has accepted beyond on a later confirmed EDGE bar; that hurdle is no longer treated as intact.")
        elif challenger.get("transition") in {"REJECTED", "RECLAIMED"}:
            lines.append(f"Challenger {z} rejected the attempt through it; opposing control is still relevant.")
        elif battlefield_half == "CHALLENGER_HALF":
            lines.append(f"Price is in the Challenger half of the {macro.lower()} battlefield with {z} still ahead; this is later-location context, not an automatic veto.")

    # Momentum / execution.
    if aurora_transition in {"BULLISH_MOMENTUM_PICKING_UP", "BULLISH_MOMENTUM_REACCELERATING"}:
        lines.append("Bullish momentum has picked up (AURORA sequence moved into Green).")
    elif aurora_transition == "BULLISH_MOMENTUM_PICKING_UP_EARLY":
        lines.append("Bullish momentum is beginning to build (AURORA moved into Yellow from a non-bull state); Yellow is context, not the locked Green trigger.")
    elif aurora_transition == "BULLISH_MOMENTUM_WEAKENING":
        lines.append("Bullish momentum is weakening (AURORA moved Green → Yellow).")
    elif aurora_transition in {"BEARISH_MOMENTUM_PICKING_UP", "BEARISH_MOMENTUM_REACCELERATING"}:
        lines.append("Bearish momentum has picked up (AURORA sequence moved into Red).")
    elif aurora_transition == "BEARISH_MOMENTUM_PICKING_UP_EARLY":
        lines.append("Bearish momentum is beginning to build (AURORA moved into Pink from Green/Yellow/White); Pink is context, not the locked Red trigger.")
    elif aurora_transition == "BEARISH_MOMENTUM_WEAKENING":
        lines.append("Bearish momentum is weakening (AURORA moved Red → Pink).")
    elif aurora_transition == "MOMENTUM_BALANCED":
        lines.append("Momentum has moved into confirmed balance (AURORA White).")
    else:
        lines.append(f"AURORA remains {aurora.get('state') or '?'}: {aurora.get('text')} while execution OF is {exec_dir.lower()}.")

    if fork.get("position") == "ABOVE_UPPER_2SD":
        lines.append("Price is above the Pitchfork upper 2SD: upside entry is stretched. This is risk/location context unless the locked opposing-OF reversal rule specifically uses the 2SD condition.")
    elif fork.get("position") == "BELOW_LOWER_2SD":
        lines.append("Price is below the Pitchfork lower 2SD: downside entry is stretched. This is risk/location context unless the locked opposing-OF reversal rule specifically uses the 2SD condition.")
    elif fork.get("reclaimed_2sd"):
        side = "lower" if fork.get("reclaim_side") == "LOWER" else "upper"
        lines.append(f"Price has reclaimed back inside the {side} 2SD boundary, restoring structural reversal permission at a qualified POI.")

    if poi:
        if poi.get("qualified"):
            lines.append(f"A qualified {poi.get('side','').lower()} Footprint POI is active inside the {poi.get('parent','').lower()} battlefield; demand/supply location is usable while that parent holds.")
        elif poi.get("conflict"):
            lines.append("Both demand and supply POIs are active together; SuperBrain treats this as conflict rather than forcing a direction.")
        else:
            lines.append("A Footprint interaction is present but it does not pass the locked Strong/Strongest + 70% hold + active-parent qualification, so it is not used as an entry POI.")

    if liquidity.get("is_new") and liquidity.get("meaning"):
        lines.append(f"Liquidity update: {liquidity['meaning']} near {_fmt(liquidity.get('level'))}; it is used as precision context, not as directional authority.")

    if posture == "LONG_ELIGIBLE":
        lines.append("Fresh long exposure is eligible from the current evidence; short exposure is not.")
    elif posture == "SHORT_ELIGIBLE":
        lines.append("Fresh short exposure is eligible from the current evidence; long exposure is not.")
    elif posture == "NO_TRADE":
        lines.append("Conditions are not favourable for a fresh directional trade; SuperBrain stays out rather than forcing a setup.")
    else:
        # Keep this concise but tell the user exactly what is gating.
        lr = long_gate.get("reasons", [])
        sr = short_gate.get("reasons", [])
        reason = (lr[0] if lr else sr[0] if sr else "the battlefield is unresolved")
        lines.append(f"No fresh entry yet: {reason}.")
    return lines[:7]


def analyze_market(evidence: Dict[str, Any], previous_evidence: Dict[str, Any], open_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Deterministic market-state reasoning with locked-rule priority.

    This layer does not recreate Pine formulas and does not invent a confidence
    score. Locked POI / Macro-ODME-OF / AURORA / Pitchfork rules determine trade
    eligibility. Defender/Challenger path context strengthens or weakens borderline
    situations, except when a trade explicitly depends on a strategic parent that
    is breaking/accepted beyond, which remains a hard invalidation.
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
    odme_dir = _odme_direction(evidence.get("odme", {}) or {})
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

    # Role integrity mismatch is a hard safety condition.
    role_mismatch = False
    if macro == "BULLISH":
        role_mismatch = defender.get("side") not in {"", "DEMAND"} or challenger.get("side") not in {"", "SUPPLY"}
    elif macro == "BEARISH":
        role_mismatch = defender.get("side") not in {"", "SUPPLY"} or challenger.get("side") not in {"", "DEMAND"}
    if role_mismatch:
        _block(long_gate, "EDGE role sides are inconsistent with the declared macro direction")
        _block(short_gate, "EDGE role sides are inconsistent with the declared macro direction")
        posture = "NO_TRADE"

    lines = _lines(
        price, macro, defender, challenger, battlefield_half, aurora, aurora_transition,
        exec_dir, fork, liquidity, poi, long_gate, short_gate, posture,
    )

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
        "long_gate": long_gate,
        "short_gate": short_gate,
        "posture": posture,
        "existing_exposure": existing,
        "lines": lines,
    }
