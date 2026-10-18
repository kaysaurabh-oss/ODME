"""WISH SuperBrain bias only — informational, never an entry or exit gate.

EDGE execution OF is the anchor; EDGE HTF structure, location and fresh optional
candle AURORA, liquidity and fresh ODME inform conviction. This output never
creates orders, pending entries, campaign actions or alerts. No FP trigger needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json
import math

from superbrain_optional_context import _best, _ms


def _n(value: Any) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _j(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        v = json.loads(str(value or ''))
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _of(value: Any) -> int:
    text = str(value or '').upper()
    if 'BEAR' in text:
        return -2 if 'STRONG' in text else -1
    if 'BULL' in text:
        return 2 if 'STRONG' in text else 1
    return 0


def _tfnum(value: Any) -> float:
    t = str(value or '').upper().strip()
    if t in ('D','1D'):
        return 1440
    if t in ('W','1W'):
        return 10080
    if t.endswith('M') and t[:-1].isdigit():
        return float(t[:-1])
    if t.endswith('H') and t[:-1].isdigit():
        return 60.0*float(t[:-1])
    return _n(t) or 0


def _box(a: Dict[str, Any], price: Optional[float]):
    if price is None:
        return None
    active = str(a.get('htf_box_active',a.get('htf_range_active',''))).lower() in ('true','1','yes')
    if not active:
        return None
    lo = _n(a.get('htf_box_low',a.get('htf_range_low')))
    hi = _n(a.get('htf_box_high',a.get('htf_range_high')))
    if lo is None or hi is None or hi<=lo:
        return None
    confirmed = str(a.get('htf_box_break_confirmed') or '').lower() in ('true','1','yes')
    if not confirmed:
        state = str(a.get('htf_box_state') or '').upper()
        confirmed = 'CONFIRMED' in state or 'ACCEPTED' in state
    if price>hi:
        return ('UP_BREAK',confirmed)
    if price<lo:
        return ('DOWN_BREAK',confirmed)
    pos=(price-lo)/(hi-lo)
    if pos<=0.25:
        return ('LOW',False)
    if pos>=0.75:
        return ('HIGH',False)
    return ('MID',False)


def _recent_liquidity(liq: Dict[str,Any], edge: Dict[str,Any]) -> str:
    e=_ms(edge.get('bar_time'))
    if e is None:
        return ''
    tf=_tfnum(liq.get('tf')) or 15
    # Event, not merely source row, must be fresh relative to current EDGE bar.
    max_age=min(60.0,max(15.0,2.5*tf))*60_000
    events=[]
    for i in range(1,6):
        key=f'liq_event{i}_'
        kind=str(liq.get(key+'type') or '').upper()
        t=_ms(liq.get(key+'confirm_time'))
        if kind and t is not None and 'FULL' in kind and ('RUN' in kind or 'SWEEP' in kind) and 0<=e-t<=max_age:
            events.append((t,kind))
    return max(events)[1] if events else ''


def _closest_opposition(edge: Dict[str,Any], raw: Dict[str,Any], price: float, side: int):
    """Opposing EDGE structures, excluding stale FP events and synthetic levels."""
    ranges=[]
    want='SUPPLY' if side>0 else 'DEMAND'
    for role in ('defender','challenger'):
        if str(edge.get(role+'_side') or '').upper()==want:
            lo,hi=_n(edge.get(role+'_low')),_n(edge.get(role+'_high'))
            if lo is not None and hi is not None and lo<=hi:
                ranges.append((lo,hi))
    for route in raw.get('edge_router_map',[]):
        if not isinstance(route,dict):continue
        zone=route.get('s1' if side>0 else 'd1')
        if not isinstance(zone,dict):continue
        lo,hi=_n(zone.get('low')),_n(zone.get('high'))
        if lo is not None and hi is not None and lo<=hi:
            ranges.append((lo,hi))
    if not ranges:return None
    distances=[]
    for lo,hi in ranges:
        distance=max(0.0,lo-price) if side>0 else max(0.0,price-hi)
        # Reject already fully crossed levels in the trend direction as resistance/support.
        if (side>0 and hi<price) or (side<0 and lo>price):continue
        distances.append(distance)
    return min(distances) if distances else None


def assess_market_bias(edge: Dict[str,Any],tv_rows:List[Dict[str,Any]],
                       odme:Optional[Dict[str,Any]]=None,edge_fresh:bool=True,
                       now_ms:Optional[float]=None) -> Dict[str,str]:
    """Return a clear 5-state bias plus an optional ONE-line factual basis.

    No FP event or PENDING_ENTRY is necessary. Never outputs BUY/SELL READY.
    Optional feeds only influence conviction when fresh and relevant.
    """
    if not edge_fresh or not edge:
        return {'label':'UNAVAILABLE','basis':''}
    if str(edge.get('edge_tracking_state') or '').upper()!='ACTIVE':
        return {'label':'UNAVAILABLE','basis':''}
    tf=_tfnum(edge.get('tf'))
    required=_tfnum(edge.get('edge_required_exec_tf') or edge.get('exec_tf') or edge.get('tf'))
    if not tf or required!=tf:
        return {'label':'UNAVAILABLE','basis':''}
    price=_n(edge.get('close'))
    sign=_of(edge.get('exec_of'))
    raw=_j(edge.get('raw_json'))
    route=raw.get('edge_router_map') or []
    htf=[]
    for r in route:
        if not isinstance(r,dict):continue
        minutes=_tfnum(r.get('tf'))
        if minutes<=tf:continue
        direction=_of(r.get('of_state'))
        if direction:
            htf.append((minutes,1 if direction>0 else -1))
    # Keep one vote each from 15m / 60m / 4h when available, with distant W/D
    # as context rather than overpowering execution control.
    mid=[s for m,s in htf if 15<=m<=75]
    higher=[s for m,s in htf if 125<=m<=240]
    htf_all=[s for m,s in htf if m>=60 and m<=240]
    htf_sign=1 if sum(htf_all)>0 else -1 if sum(htf_all)<0 else 0
    if sign==0:
        if htf_sign:
            base='BULLISH LEAN' if htf_sign>0 else 'BEARISH LEAN'
        else:
            base='NEUTRAL'
    else:
        base='BULLISH' if sign>0 else 'BEARISH'
    direction=1 if sign>0 else -1 if sign<0 else htf_sign
    match=sum(1 for _,s in htf if s==direction and _<=240)
    oppose=sum(1 for _,s in htf if s==-direction and _<=240)
    # Broad STRONG bias requires strong execution AND actual agreement across
    # higher timeframes, not just a powerful 3-minute candle.
    strong=abs(sign)==2 and match>=2 and oppose==0
    notes=[]
    is_counter=bool(sign and htf_sign and sign*htf_sign<0)
    if is_counter:
        notes.append('short-term correction against higher-TF control')
        strong=False
    # Add location only when materially distinctive.
    battlefield=_n(raw.get('battlefield_range_position_pct'))
    if battlefield is not None:
        if battlefield>=65: notes.append('upper battlefield')
        elif battlefield<=35: notes.append('lower battlefield')
    if price is not None and direction:
        dist=_closest_opposition(edge,raw,price,direction)
        if dist is not None and price>0:
            pct=100*dist/price
            # Higher-TF resistance/support close enough to make extension poor.
            if pct<=0.5:
                notes.append('near supply' if direction>0 else 'near demand')
                strong=False
    now_ms=(float(now_ms) if now_ms is not None else datetime.now(timezone.utc).timestamp()*1000)
    a=_best(tv_rows,'AURORA',edge,now_ms)
    l=_best(tv_rows,'LIQUIDITY',edge,now_ms)
    state=str(a.get('aurora_state') or '').upper()
    location=_box(a,price)
    if location:
        name,confirmed=location
        if name=='MID':
            return {'label':'CHOPPY / RANGE','basis':'inside HTF box; no directional edge'}
        if name=='LOW':
            notes.insert(0,'HTF box lower edge: potential buy zone')
            if direction<0:strong=False
        elif name=='HIGH':
            notes.insert(0,'HTF box upper edge: potential sell zone')
            if direction>0:strong=False
        elif name=='UP_BREAK':
            notes.insert(0,'confirmed HTF box upside breakout' if confirmed else 'testing HTF box upside break')
            if not confirmed or direction<0:strong=False
        elif name=='DOWN_BREAK':
            notes.insert(0,'confirmed HTF box downside breakout' if confirmed else 'testing HTF box downside break')
            if not confirmed or direction>0:strong=False
    if state=='WHITE':
        return {'label':'CHOPPY / SIDEWAYS','basis':'candle AURORA WHITE; avoid new directional chasing'}
    if (direction>0 and state in ('RED','PINK')) or (direction<0 and state in ('GREEN','YELLOW')):
        strong=False
        notes.append('candle momentum conflicts')
    elif state in ('YELLOW','PINK') and direction:
        # These colors are slowing states, not full confirmation of a trend.
        strong=False
        if not is_counter:notes.append('momentum slowing')
    liq_kind=_recent_liquidity(l,edge) if l else ''
    if liq_kind:
        liq_sign=1 if 'BUY' in liq_kind else -1
        # In this feed BUY_FULL_SWEEP rejects the upside; SELL_FULL_SWEEP
        # reclaims the downside. RUN is accepted continuation, opposite of SWEEP.
        effect=liq_sign if 'RUN' in liq_kind else -liq_sign
        if direction and effect==-direction:
            strong=False
            notes.append('recent liquidity rejection')
        elif direction and effect==direction and 'RUN' in liq_kind and not notes:
            notes.append('liquidity run supports')
    if odme:
        od_dir=str(odme.get('direction') or 'NEUTRAL').upper()
        od_sign=1 if od_dir=='BULLISH' else -1 if od_dir=='BEARISH' else 0
        if od_sign and direction and od_sign!=direction:
            strong=False
            if 'short-term correction against higher-TF control' not in notes:
                notes.append('ODME opposes')
        elif od_sign and direction and od_sign==direction and odme.get('strength')=='STRONG' and not notes:
            notes.append('ODME confirms')
        elif od_sign==0 and str(odme.get('regime') or '').upper() in ('RANGE','MIXED'):
            strong=False
    if strong and base in ('BULLISH','BEARISH'):
        base='STRONG '+base
    # Keep one compact qualifying line, prioritizing conflict/location to explain
    # the bias without retelling the full market or enumerating irrelevant levels.
    return {'label':base,'basis':' · '.join(notes[:2])}
