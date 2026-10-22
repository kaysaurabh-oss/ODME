"""WISH SuperBrain EDGE + ODME policy (locked October 2026 rulebook). Does not calculate Pine/ODME indicators.

Pure, testable decision helpers. AURORA is deliberately manual for ENTRY only.
No AURORA/STRUCTURE/LIQUIDITY feed is consumed by this module.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

RULEBOOK_VERSION = "SB_EDGE_ODME_20261022_V2"


def direction(v: Any) -> str:
    s = str(v or '').upper()
    if 'BEARISH' in s and 'BULLISH' not in s:
        return 'BEARISH'
    if 'BULLISH' in s and 'BEARISH' not in s:
        return 'BULLISH'
    return 'NEUTRAL'


def option_direction(option_type: str) -> str:
    return 'BULLISH' if str(option_type).upper() == 'PE' else 'BEARISH'


def adverse_expansion(assessment: Dict[str, Any], option_type: str) -> bool:
    """Hard ODME risk exception: a material expansion in the trade's adverse direction.

    The ODME packet's expansion_score alone cannot identify direction. Require
    its expansion_direction to explicitly oppose the proposed trade.
    """
    opposite = 'BEARISH' if option_type.upper() == 'PE' else 'BULLISH'
    exp_dir = str(assessment.get('expansion_direction') or '').upper()
    matching = ('DOWNSIDE' in exp_dir and opposite == 'BEARISH') or ('UPSIDE' in exp_dir and opposite == 'BULLISH')
    return matching and str(assessment.get('expansion_risk') or '').upper() in {'ELEVATED','HIGH'}


def rank_for_aurora(option_type: str, of_control: str, odme_direction: str, aurora_direction: str) -> Optional[int]:
    """Three equally weighted directional votes: EDGE OF, ODME, manual AURORA.

    3 aligned -> L1; 2 -> L2; 1 -> L3; 0 -> no entry.
    Neutral votes never count as positive.
    """
    desired = option_direction(option_type)
    count = sum(direction(x) == desired for x in (of_control,odme_direction,aurora_direction))
    return {3:1,2:2,1:3}.get(count)


def entry_matrix(option_type: str, of_control: str, odme_assessment: Dict[str, Any],
                 ladder: List[Dict[str, Any]], fresh: bool, map_reliable: bool,
                 strike_selector, safer_strike: Optional[float] = None) -> Dict[str, Any]:
    """Choose the actual qualifying listed strike beyond each distinct mapped level.

    strike_selector(option_type, boundary) must return a *real* listed strike or None.
    """
    opt = str(option_type or '').upper()
    if opt not in {'CE','PE'}:
        return {'status':'WAIT','reason':'Unknown proposed option type','choices':{}}
    if not fresh:
        return {'status':'WAIT','reason':'Fresh ODME scan unavailable. Do not use saved ODME as current.','choices':{}}
    if not map_reliable:
        return {'status':'WAIT','reason':'EDGE/ODME structural map is incomplete or stale.','choices':{}}
    if adverse_expansion(odme_assessment,opt):
        return {'status':'AVOID','reason':'ODME reports material adverse expansion risk against this short option.','choices':{}}
    od = odme_assessment.get('direction','NEUTRAL')
    choices:Dict[str,Any]={}
    for aurora in ('BULLISH','NEUTRAL','BEARISH'):
        rank=rank_for_aurora(opt,of_control,od,aurora)
        if rank is None:
            choices[aurora]={'status':'WAIT','reason':'All three directional votes oppose or fail to support this entry.','rank':None}
            continue
        if len(ladder)<rank:
            choices[aurora]={'status':'WAIT','reason':f'L{rank} mapped protection level is unavailable.','rank':rank}
            continue
        group=ladder[rank-1]
        boundary=float(group['far'])
        strike=strike_selector(opt,boundary)
        if strike is None and safer_strike is not None:
            safer=float(safer_strike)
            if (opt=='PE' and safer<boundary) or (opt=='CE' and safer>boundary):
                strike=safer
        if strike is None:
            choices[aurora]={'status':'WAIT','reason':f'No verified listed {opt} strike beyond L{rank}.','rank':rank,'group':group,'boundary':boundary}
            continue
        choices[aurora]={
            'status':'ENTRY READY','rank':rank,'strike':float(strike),
            'group':group,'boundary':boundary,'reason':f'{rank_for_aurora(opt,of_control,od,aurora)}-tier alignment; L{rank} protection.'
        }
    return {
        'status':'ENTRY READY' if any(x['status']=='ENTRY READY' for x in choices.values()) else 'WAIT',
        'reason':'Select the actual Renko AURORA bias manually. EDGE OF and ODME determine the other two votes.',
        'choices':choices,
        'option_type':opt,
        'of_direction':direction(of_control),
        'odme_direction':direction(od),
    }


def protection_broken(option_type: str, price: Optional[float], level: Optional[float],
                      confirmed_fp_run: bool=False, accepted_two_bars: bool=False) -> bool:
    """Only explicit confirmed FP RUN or two confirmed EDGE closes across level.

    Price being over a protection boundary on a single observation is not a
    confirmed break. The caller must validate the bar TF and age first.
    """
    if price is None or level is None:
        return False
    crossed=price < level if option_type.upper()=='PE' else price > level
    return crossed and (bool(confirmed_fp_run) or bool(accepted_two_bars))


def campaign_decision(option_type: str, of_control: str, broken_l2: bool,
                      broken_l3: bool, odme_adverse: bool, safer_target: Optional[float],
                      current_strike: float, protective_rank: int = 3,
                      broken_l1: bool = False) -> Dict[str, Any]:
    """Locked hierarchy: supportive EDGE OF -> L3; neutral/opposing -> L2.

    A confirmed break calls for risk action, not a forced new leg or martingale.
    """
    opt=option_type.upper()
    supportive=direction(of_control)==option_direction(opt)
    max_rank=max(1,min(3,int(protective_rank)))
    exit_rank=max_rank if supportive else max(1,max_rank-1)
    breaches={1:bool(broken_l1),2:bool(broken_l2),3:bool(broken_l3)}
    risk=bool(odme_adverse or any(breaches.get(r,False) for r in range(exit_rank,max_rank+1)))
    if not risk:
        return {'action':'HOLD','trigger':f'WAIT_L{exit_rank}','required_rank':exit_rank,
                'reason':f'EDGE OF is {direction(of_control)}; L{exit_rank} protection has not confirmed a break.'}
    reason='Material ODME adverse expansion risk.' if odme_adverse else f'Confirmed L{exit_rank} protection break with {direction(of_control)} EDGE OF.'
    outward=(safer_target is not None and
             ((opt=='PE' and safer_target<current_strike) or (opt=='CE' and safer_target>current_strike)))
    if outward:
        return {'action':'ROLL OUT','trigger':f'ADVERSE_L{exit_rank}' if not odme_adverse else 'ODME_EXPANSION',
                'required_rank':exit_rank,'target':float(safer_target),
                'reason':reason+' Check executable price and risk before rolling the same-direction option.'}
    return {'action':'CLOSE','trigger':f'ADVERSE_L{exit_rank}' if not odme_adverse else 'ODME_EXPANSION',
            'required_rank':exit_rank,'target':None,
            'reason':reason+' No verified safer same-direction strike is available.'}


# ---------------------------------------------------------------------------
# Non-optionable / ODME-disabled market action. This ONLY reads EDGE facts and
# manual Renko AURORA bias; it never calls ODME and does not invent stock orders.
# ---------------------------------------------------------------------------
def cash_entry_signal(setup_name: str, fp: Dict[str, Any], price: Optional[float],
                      active: bool, manual_aurora: str = "CHECK",
                      max_event_age: int = 2) -> Dict[str, Any]:
    """A conditional spot/futures direction from an actual FP defence or break.

    The setup is first confirmed PENDING_ENTRY by the EDGE Level-2 service.
    Merely TOUCHING or BREACH_PENDING is not HOLD and cannot authorize entry.
    'BREAK' setups require prior accepted break on EDGE and remain beyond the
    distal. 'DEFENCE' setups require a recent explicit defence/rejection event.
    """
    wait = lambda reason: {"status": "WAIT", "reason": reason, "direction": "", "stage": ""}
    if not active:
        return wait("EDGE execution feed is not fresh/active; no new action.")
    name = str(setup_name or "").upper()
    side = str(fp.get("side") or "").upper()
    event = str(fp.get("eventType") or fp.get("event_type") or "").upper()
    status = str(fp.get("status") or "").upper()
    context = str(fp.get("contextState") or fp.get("context_state") or fp.get("context") or "").upper()
    try:
        lo,hi,px = float(fp.get("low")),float(fp.get("high")),float(price)
    except (ValueError,TypeError):
        return wait("Confirmed FP zone/price is missing.")
    if not all(v==v and abs(v)!=float('inf') for v in (lo,hi,px)) or lo>hi:
        return wait("Invalid FP zone/price.")
    if side not in {"DEMAND","SUPPLY"}:
        return wait("Unknown FP side.")
    breaking = name in {"SHORT_CE_AFTER_DEMAND_BREAK", "SHORT_PE_AFTER_SUPPLY_BREAK"}
    if breaking:
        if (side == "DEMAND" and name != "SHORT_CE_AFTER_DEMAND_BREAK") or (side == "SUPPLY" and name != "SHORT_PE_AFTER_SUPPLY_BREAK"):
            return wait("Break setup does not match its originating FP.")
        if not (px<lo if side=="DEMAND" else px>hi):
            return wait("Broken FP is no longer accepted beyond its distal level.")
        action = "SELL" if side=="DEMAND" else "BUY"
        stage = "ACCEPTED_BREAK"
        explanation = f"{side.title()} FP {lo:g}–{hi:g} broken; price remains beyond the distal."
    else:
        if name not in {"LONG_FROM_DEMAND","LONG_REVERSAL_FROM_DEMAND","LONG_FROM_DEMAND_UNRESOLVED",
                        "SHORT_FROM_SUPPLY","SHORT_REVERSAL_FROM_SUPPLY","SHORT_FROM_SUPPLY_UNRESOLVED"}:
            return wait("No recognized current EDGE defence setup.")
        if (side=="DEMAND" and not name.startswith('LONG')) or (side=="SUPPLY" and not name.startswith('SHORT')):
            return wait("Setup and FP sides do not agree.")
        if status == "RUN" or event == "RUN" or "BREACH_PENDING" in context:
            return wait("FP defence is breached/pending; do not treat a touch as a hold.")
        defence = any(word in event for word in ("DEFENCE","DEFENSE","HOLD","REJECT")) or \
                  status in {"HELD","HOLD","DEFENDED","DEFENDED_CONFIRMED","REJECTED"}
        if not defence:
            return wait("FP touched, but no explicit EDGE defence/rejection event yet.")
        age = fp.get("barsSinceEvent",fp.get("bars_since_event"))
        if age not in (None,""):
            try:
                if int(float(age))>max_event_age:
                    return wait("Latest FP defence event is no longer recent; wait for a new confirmed interaction.")
            except (TypeError,ValueError):
                return wait("FP event age cannot be verified.")
        if not (px>=lo if side=="DEMAND" else px<=hi):
            return wait("Price has moved through the FP; defence no longer supports entry.")
        action = "BUY" if side=="DEMAND" else "SELL"
        stage = "EARLY_DEFENCE" if "ABSORPTION" in event or "AGGRESSIVE" in event or "WAITING" in context else "FP_HOLD"
        explanation = f"{side.title()} FP {lo:g}–{hi:g} shows {stage.replace('_',' ').lower()}."
    aur = str(manual_aurora or "CHECK").upper()
    required = "BULLISH" if action=="BUY" else "BEARISH"
    invalidation = lo if action=="BUY" else hi
    out = {"status":"CHECK AURORA","direction":action,"stage":stage,
           "reason":explanation, "required_aurora":required,"invalidation_reference":invalidation,
           "confirmed_action":f"{action} only when manual Renko AURORA is {required}."}
    if aur == required:
        out["status"] = "ENTRY READY"
        out["reason"] = explanation+f" Manual Renko AURORA {aur} agrees."
    elif aur in {"BULLISH","NEUTRAL","BEARISH"}:
        out["status"] = "WAIT"
        out["reason"] = explanation+f" Manual Renko AURORA {aur} does not permit {action}."
    return out
