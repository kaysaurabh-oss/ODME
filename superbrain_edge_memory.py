"""EDGE journey: display-only longitudinal intelligence from an append-only EDGE candle tape.

The Telegram scheduler writes the tape. Streamlit reads it. No effect on existing
setup recognition, option strikes, campaign mutation or Level 2 alert decisions.
Never reconstruct missing candles and never treat estimated FP delta as executed
bid/ask volume. Missing history yields a clearly bounded snapshot-only view.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
import json
import math
from typing import Any, Dict, List, Optional

from superbrain_optional_context import _best, _ms, _box_read

TAPE_TAB = 'SB_EDGE_TAPE'


def num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def of(v: Any) -> int:
    v=str(v or '').upper()
    return -1 if 'BEAR' in v else 1 if 'BULL' in v else 0


def jsondict(v: Any) -> Dict[str, Any]:
    if isinstance(v,dict):return v
    try:
        r=json.loads(str(v or ''))
        return r if isinstance(r,dict) else {}
    except (TypeError, ValueError):
        return {}


def read_edge_tape(store: Any, instrument: str, max_rows: int=3000) -> List[Dict[str, Any]]:
    """Bounded read, fail silent if local-only or tab isn't ready.

    The Sheet is provisioned separately; NEVER create or rewrite it from the app.
    """
    try:
        ws=store.sheet.worksheet(TAPE_TAB)
        last=len(ws.col_values(1))
        if last<=1:return []
        start=max(2,last-max_rows+1)
        cols=34
        headers=ws.row_values(1)[:cols]
        if len(headers)!=cols or headers[0]!='captured_at' or headers[2]!='bar_ms':return []
        values=ws.get(f'A{start}:AH{last}')
        wanted=str(instrument or '').strip().upper()
        rows=[]
        for row in values:
            if len(row)>2 and str(row[1]).strip().upper()==wanted:
                d={headers[i]:row[i] if i<len(row) else '' for i in range(cols)}
                if num(d.get('bar_ms')) is not None and num(d.get('close')) is not None:rows.append(d)
        rows.sort(key=lambda r:num(r.get('bar_ms')) or 0)
        return rows[-650:]
    except Exception:
        return []


def _snap_tape(edge: Dict[str,Any]) -> Dict[str,Any]:
    raw=jsondict(edge.get('raw_json'))
    routes=raw.get('edge_router_map') or []
    vals={str(i.get('tf')):i.get('of_state') for i in routes if isinstance(i,dict)}
    return {'instrument':edge.get('instrument'), 'bar_ms':edge.get('bar_time'),
        'tf':edge.get('tf'), 'close':edge.get('close'),
        'exec_of':edge.get('exec_of'),'exec_of_score':edge.get('exec_of_score'),
        'macro':edge.get('macro'),'of_15':vals.get('15'),'of_60':vals.get('60'),
        'of_125':vals.get('125'),'of_240':vals.get('240'),
        'battlefield_pct':raw.get('battlefield_range_position_pct'),
        'battlefield_divider':raw.get('battlefield_divider'),
        'defender_side':edge.get('defender_side'), 'defender_low':edge.get('defender_low'),
        'defender_high':edge.get('defender_high'),'challenger_side':edge.get('challenger_side'),
        'challenger_low':edge.get('challenger_low'),'challenger_high':edge.get('challenger_high'),
        'fp_zone_id':edge.get('fp_zone_id'),'fp_side':edge.get('fp_side'),
        'fp_low':edge.get('fp_low'),'fp_high':edge.get('fp_high'),
        'fp_context_state':edge.get('fp_context_state'),
        'fp_event_type':edge.get('fp_event_type'),'fp_session_delta':edge.get('fp_session_delta'),
        'fp_interacting':edge.get('fp_interacting'),
        'fp_event_time':edge.get('fp_event_time'),
        'fp_bars_since_event':edge.get('fp_bars_since_event'),
        'fp_breach_pending':edge.get('fp_breach_pending')}


def _clean_path(edge:Dict[str,Any],hist:List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """Longest recent *observed* uninterrupted run, up to 480 bars / 14d.

    A timeframe change, missing scheduler samples or nonmonotonic timestamp
    breaks the journey. In particular we do not interpolate missing candles.
    """
    cur=_snap_tape(edge)
    current_ms=_ms(cur.get('bar_ms'))
    tf=num(cur.get('tf'))
    if not current_ms or not tf:return []
    tf_ms=tf*60_000
    allowed=1.7*tf_ms
    oldest_allowed=current_ms-14*24*60*60_000
    seen={}
    for r in hist+[cur]:
        ms=_ms(r.get('bar_ms'))
        if not ms or ms>current_ms or ms<oldest_allowed:continue
        if num(r.get('tf'))!=tf:continue
        if str(r.get('instrument') or '').strip().upper()!=str(cur.get('instrument') or '').strip().upper():continue
        seen[int(ms)]=r
    ordered=[seen[k] for k in sorted(seen)]
    if not ordered:return []
    chain=[ordered[-1]]
    for r in reversed(ordered[:-1]):
        if _ms(chain[-1]['bar_ms'])-_ms(r['bar_ms'])>allowed:break
        chain.append(r)
        if len(chain)>=480:break
    return list(reversed(chain))


def _event_anchor(path:List[Dict[str,Any]],tf_ms:float) -> Optional[int]:
    """Anchor at latest FP defence/break or observed zone engagement.

    IMPORTANT: EDGE carries its last FP event forward for many candles. Only
    treat it as witnessed when the event timestamp actually falls within the
    observed path and a recorded sample exists at the event moment. Otherwise
    do not invent a starting zone, use earliest observed price instead.
    """
    if len(path)<2:return None
    first_ms=_ms(path[0].get('bar_ms'))
    last_ms=_ms(path[-1].get('bar_ms'))
    if not first_ms or not last_ms:return None
    candidates=[]
    for i,r in enumerate(path):
        ms=_ms(r.get('bar_ms'))
        zid=str(r.get('fp_zone_id') or '').strip()
        side=str(r.get('fp_side') or '').upper()
        if not ms or not zid or side not in ('DEMAND','SUPPLY'):continue
        ev=str(r.get('fp_event_type') or '').upper()
        tm=_ms(r.get('fp_event_time'))
        bars=num(r.get('fp_bars_since_event'))
        # An absorption probe, breach test or repeatedly published pending
        # event is not a completed defence/break. Only a confirmed transition
        # may reset the dominant journey. The recent FP activity is still
        # available to the separate zone/delta commentary.
        confirmed=(ev in ('DEFENCE_CONFIRMED','BREAK_CONFIRMED',
                          'RUN_CONFIRMED','RECLAIM_CONFIRMED')
                   or (ev.startswith('DEFENCE_') and ev.endswith('_CONFIRMED')))
        if (confirmed and tm is not None and first_ms<=tm<=last_ms
            and 0<=ms-tm<=1.7*tf_ms and (bars is None or 0<=bars<=2)):
            candidates.append((tm,i,'FP_EVENT'))
        # Even if EDGE does not emit a new named event, the first *recorded*
        # zone interaction is a valid (but weaker) start marker.
        active=str(r.get('fp_interacting') or '').upper() in ('TRUE','1')
        preceding=False
        if i:
            prev=path[i-1]
            preceding=(str(prev.get('fp_interacting') or '').upper() in ('TRUE','1')
                       and str(prev.get('fp_zone_id') or '').strip()==zid)
        if active and not preceding:
            candidates.append((ms,i,'FP_INTERACTION'))
    if not candidates:return None
    # A new TEST or early zone engagement must not supersede an older, fully
    # observed confirmed event in the dominant journey. Prefer the latest
    # sufficiently developed confirmed transition; only when none exists use
    # a mature initial interaction as the observed journey anchor.
    mature=[c for c in candidates if len(path)-c[1]>=5]
    confirmed=[c for c in mature if c[2]=='FP_EVENT']
    candidates=confirmed or mature
    if not candidates:return None
    _tm, index, kind=max(candidates,key=lambda q:(q[0],-q[1]))
    # When an FP confirmed its defence/break a few candles after first touch,
    # use the first recorded touch in that same episode as the true start.
    if kind=='FP_EVENT':
        zid=str(path[index].get('fp_zone_id') or '').strip()
        side=str(path[index].get('fp_side') or '').upper()
        prior=[j for j in range(max(0,index-16),index+1)
               if str(path[j].get('fp_zone_id') or '').strip()==zid
               and str(path[j].get('fp_side') or '').upper()==side
               and str(path[j].get('fp_interacting') or '').upper() in ('TRUE','1')]
        if prior:index=prior[0]
    return index


def _journey(edge:Dict[str,Any],hist:List[Dict[str,Any]]):
    """Prefer the latest witnessed FP event; otherwise longest observed run."""
    path=_clean_path(edge,hist)
    tf=num(edge.get('tf'))
    if not path or not tf:return path,'OBSERVED_START'
    index=_event_anchor(path,tf*60_000)
    if index is None:return path,'OBSERVED_START'
    # A just-triggered FP event must not erase a long uninterrupted journey.
    # Until its own episode has developed, keep the broader observed history,
    # while _natural_fp still reports the new event from its real timestamp.
    if len(path)-index < 5:
        return path,'OBSERVED_START · FP DEVELOPING'
    return path[index:], 'RECORDED_FP'


def _movement(path:List[Dict[str,Any]]) -> Dict[str,float]:
    """Price travel, pullbacks, execution OF persistence and market noise."""
    close=[num(r.get('close')) for r in path]
    if len(close)<2 or any(x is None or x<=0 for x in close):return {}
    steps=[(close[i]-close[i-1])/close[i-1]*100 for i in range(1,len(close))]
    first,last=close[0],close[-1]
    travelled=(last-first)/first*100
    # Robust minimum move: appreciable relative to observed candle noise,
    # while still allowing a long gradual advance to qualify.
    typical=median(abs(x) for x in steps)
    threshold=max(0.16,2.1*typical,min(0.35,0.04*math.sqrt(len(close))))
    prev=path[:-1]
    bullshare=sum(of(r.get('exec_of'))>0 for r in prev)/len(prev)
    bearshare=sum(of(r.get('exec_of'))<0 for r in prev)/len(prev)
    peak=max(close)
    trough=min(close)
    peak_gain=(peak-first)/first*100
    trough_drop=(first-trough)/first*100
    retrace_up=(peak-last)/(peak-first) if peak>first else 0
    retrace_down=(last-trough)/(first-trough) if trough<first else 0
    recent=close[-min(9,len(close)):]
    latest_move=(last-recent[0])/recent[0]*100
    return {'move':travelled,'noise':typical,'threshold':threshold,
            'bullshare':bullshare,'bearshare':bearshare,
            'peak_gain':peak_gain,'trough_drop':trough_drop,
            'retrace_up':retrace_up,'retrace_down':retrace_down,
            'latest_move':latest_move,'n':len(close)}


def _htf(edge:Dict[str,Any]):
    raw=jsondict(edge.get('raw_json'))
    routes=raw.get('edge_router_map') or []
    bytf={str(r.get('tf')):of(r.get('of_state')) for r in routes if isinstance(r,dict)}
    s=[bytf.get('60',0),bytf.get('125',0),bytf.get('240',0)]
    return 1 if sum(s)>=2 else -1 if sum(s)<=-2 else 0


def _location(edge:Dict[str,Any]):
    raw=jsondict(edge.get('raw_json'))
    price=num(edge.get('close'))
    if price is None:return ('',False,False)
    lower=num(raw.get('battlefield_map_lower'))
    upper=num(raw.get('battlefield_map_upper'))
    position=num(raw.get('battlefield_range_position_pct'))
    if position is None and lower is not None and upper is not None and upper>lower:
        position=(price-lower)/(upper-lower)*100
    near_supply=False;near_demand=False
    # Unlike a bullish/bearish labeling shortcut, verify actual prices.
    for role in ('defender','challenger'):
        side=str(edge.get(role+'_side') or '').upper()
        lo=num(edge.get(role+'_low'));hi=num(edge.get(role+'_high'))
        if lo is None or hi is None:continue
        if side=='SUPPLY' and 0<=lo-price<=0.003*max(price,1):near_supply=True
        if side=='DEMAND' and 0<=price-hi<=0.003*max(price,1):near_demand=True
    if position is None:return ('',near_supply,near_demand)
    if position>=75:return ('upper battlefield',near_supply,near_demand)
    if position<=25:return ('lower battlefield',near_supply,near_demand)
    return ('middle battlefield',near_supply,near_demand)


def _recent_liq(rows:List[Dict[str,Any]],edge:Dict[str,Any],now_ms:float):
    row=_best(rows,'LIQUIDITY',edge,now_ms)
    if not row:return ''
    event=[]
    current=_ms(edge.get('bar_time'))
    for i in range(1,6):
        name=f'liq_event{i}_'
        kind=str(row.get(name+'type') or '').upper()
        tm=_ms(row.get(name+'confirm_time'))
        if not tm or not kind or not current or tm>current or current-tm>30*60000:continue
        event.append((tm,kind,num(row.get(name+'level'))))
    if not event:return ''
    event.sort(reverse=True)
    t,k,l=event[0]
    p=num(edge.get('close'))
    if p is None or l is None:return ''
    # Classification is based on the run/sweep and present relation to level;
    # the event names alone do not promise a reversal.
    if 'BUY' in k and 'SWEEP' in k and p<l:return 'upside liquidity swept and rejected'
    if 'SELL' in k and 'SWEEP' in k and p>l:return 'downside liquidity swept and reclaimed'
    if 'BUY' in k and 'RUN' in k and p>l:return 'upside liquidity accepted'
    if 'SELL' in k and 'RUN' in k and p<l:return 'downside liquidity accepted'
    return ''


def _journey_commentary(label: str, basis: str, path: List[Dict[str,Any]],
                        progress: Dict[str,float], edge: Dict[str,Any]) -> Dict[str,str]:
    """A brief evidence-based market narrative, no trade/entry instruction.

    Distinguish persistent control that *worked* from a newer counter-move.
    Do not silently attribute a recovery to an old, unwitnessed FP event.
    """
    if not progress or len(path)<5:
        return {'label':label, 'commentary':
                'Current EDGE control is '+str(edge.get('exec_of') or 'unconfirmed').replace('_',' ').lower()+
                '. There is not enough continuous EDGE history to assess price response or a developing turn.'}
    first=num(path[0].get('close'))
    current=num(path[-1].get('close'))
    if not first or not current:return {'label':label,'commentary':basis}
    ms0=_ms(path[0].get('bar_ms'))
    ms1=_ms(path[-1].get('bar_ms'))
    mins=(ms1-ms0)/60000 if ms0 and ms1 and ms1>ms0 else 0
    tf=num(path[-1].get('tf')) or 0
    mins+=tf
    span=(f'{round(mins/60,1):g}h' if mins>=60 else f'{max(1,round(mins))}m')
    n=len(path)
    neg=sum(of(r.get('exec_of'))<0 for r in path)
    pos=sum(of(r.get('exec_of'))>0 for r in path)
    travel=progress['move']
    dominant_flow='bearish' if neg>=0.70*n else 'bullish' if pos>=0.70*n else 'mixed'
    if travel>0:
        achievement=f'advanced {travel:.2f}%'
    elif travel<0:
        achievement=f'declined {abs(travel):.2f}%'
    else:achievement='made little net progress'
    if (travel>0 and dominant_flow=='bearish') or (travel<0 and dominant_flow=='bullish'):
        verb=('sellers could not secure sustained downside' if dominant_flow=='bearish'
              else 'buyers could not secure sustained upside')
        main=(f'Over {span} ({n} observed candles), price {achievement} despite '+
              f'{dominant_flow} execution OF on {neg if dominant_flow=="bearish" else pos}/{n} bars; {verb}.')
    else:
        main=(f'Over {span} ({n} observed candles), price {achievement} while '+
              (f'{dominant_flow} execution OF persisted on {neg if dominant_flow=="bearish" else pos}/{n} bars; '
               if dominant_flow!='mixed' else 'execution OF was mixed; ')+
              ('directional pressure has achieved price progress.' if dominant_flow!='mixed'
               else 'the broader journey lacks consistent execution control.'))
    # Reversal probes are tied to recent observed extremes, not the global low
    # hours earlier. Two meaningful counter-directional closes are required.
    prices=[num(r.get('close')) for r in path]
    recent=prices[-min(12,len(prices)):]
    median_step=progress.get('noise',0)
    floor=max(0.18,1.6*median_step)
    after_extreme=0
    move_since_extreme=0.0
    if travel < 0 and 'BEARISH' in label:
        i=min(range(len(recent)), key=lambda j:recent[j])
        after_extreme=len(recent)-i-1
        move_since_extreme=(recent[-1]-recent[i])/recent[i]*100
        if after_extreme>=2 and move_since_extreme>=floor:
            if move_since_extreme>=max(0.65, abs(travel)*0.50):
                label='BEARISH · REVERSAL RISK'
                tail=(f'But price has rebounded {move_since_extreme:.2f}% from its recent low '+
                      'while execution OF remains bearish; seller control is being challenged substantially.')
            else:
                label='BEARISH · REBOUND DEVELOPING'
                tail=(f'But price rebounded {move_since_extreme:.2f}% from its recent low '+
                      'without a bullish OF turn; early seller ineffectiveness, not yet a confirmed reversal.')
            return {'label':label,'commentary':main+' '+tail}
    if travel > 0 and label.startswith(('BULLISH','STRONG BULLISH')):
        i=max(range(len(recent)), key=lambda j:recent[j])
        after_extreme=len(recent)-i-1
        move_since_extreme=(recent[i]-recent[-1])/recent[i]*100
        if after_extreme>=2 and move_since_extreme>=floor:
            if move_since_extreme>=max(0.65,travel*0.50):
                label='BULLISH · REVERSAL RISK'
                tail=f'However, the latest decline of {move_since_extreme:.2f}% from the recent high is challenging buyers.'
            else:
                label=label+' · PULLBACK' if 'PULLBACK' not in label else label
                tail=f'A {move_since_extreme:.2f}% pullback is developing; the larger upside journey has not yet reversed.'
            return {'label':label,'commentary':main+' '+tail}
    # Brief structural qualifier, not a list of levels.
    htf=_htf(edge)
    flow=of(edge.get('exec_of'))
    if flow and htf and flow!=htf:
        tail='The execution move remains against higher-timeframe control.'
    elif 'RECOVERY' in label:
        tail='The observed recovery leads, although execution control still disagrees.'
    else:
        tail='No sufficiently developed opposing price turn is yet confirmed.'
    return {'label':label,'commentary':main+' '+tail}



def _signed_score(value: Any) -> str:
    n=num(value)
    return f'{n:+.0f}' if n is not None else 'unknown'


def _execution_evidence(edge: Dict[str,Any], path: List[Dict[str,Any]]) -> str:
    """Use actual OF scores and changes; price is not a proxy for OF."""
    state=str(edge.get('exec_of') or 'UNCONFIRMED').replace('_',' ').lower()
    score=_signed_score(edge.get('exec_of_score'))
    msg=f'Execution OF {state} ({score})'
    hist=[num(r.get('exec_of_score')) for r in path]
    hist=[x for x in hist if x is not None]
    if len(hist)>=10:
        prior=median(hist[:min(6,len(hist)//3)])
        recent=median(hist[-min(6,len(hist)//3):])
        delta=recent-prior
        if abs(delta)>=15:
            direction='less bearish' if prior<0 and recent<0 and delta>0 else (
                'more bearish' if prior<0 and recent<0 and delta<0 else (
                'less bullish' if prior>0 and recent>0 and delta<0 else (
                'more bullish' if prior>0 and recent>0 and delta>0 else 'shifting')))
            msg+=f'; control has become {direction} ({prior:+.0f} to {recent:+.0f})'
        elif abs(recent)>=45:
            msg+='; pressure has stayed persistent'
    return msg+'.'


def _mtf_evidence(edge: Dict[str,Any]) -> str:
    """Prefer the nearest independent control layers, retain the 4h nuance."""
    raw=jsondict(edge.get('raw_json'))
    records=raw.get('edge_router_map') or []
    routes={str(q.get('tf')):q for q in records if isinstance(q,dict)}
    summary=[]
    direction=of(edge.get('exec_of'))
    for tf in ('15','60','75'):
        q=routes.get(tf)
        if q and of(q.get('of_state'))==direction and direction:
            score=num(q.get('of_score'))
            summary.append(f'{tf}m {score:+.0f}' if score is not None else f'{tf}m {"bullish" if direction>0 else "bearish"}')
    if len(summary)>=2:
        lead='Confirming MTF OF: '+', '.join(summary[:3])
    elif summary:
        lead='Aligned MTF OF: '+', '.join(summary)
    else:
        lead=''
    long=[routes.get(tf) for tf in ('125','240')]
    valid=[(tf,q) for tf,q in zip(('125m','240m'),long) if q and of(q.get('of_state'))]
    opposing=[(tf,q) for tf,q in valid if of(q.get('of_state'))==-direction and direction]
    if opposing:
        tf,q=opposing[-1]
        longnote=f'{tf} still {str(q.get("of_state") or "").replace("_"," ").lower()} ({_signed_score(q.get("of_score"))})'
    elif valid and all(of(q.get('of_state'))==direction for tf,q in valid) and direction:
        longnote='125m/240m direction agrees'
    else:
        longnote='higher timeframes mixed' if not lead and len(valid)<2 else ''
    parts=[p for p in (lead,longnote) if p]
    return '; '.join(parts)+'.' if parts else ''


def _fp_evidence(edge:Dict[str,Any],path:List[Dict[str,Any]]) -> str:
    """In-zone delta is estimated and meaningful only while an interaction is actually observed."""
    snap=_snap_tape(edge)
    if not path or not snap:return ''
    zid=str(snap.get('fp_zone_id') or '').strip()
    side=str(snap.get('fp_side') or '').upper()
    active=str(snap.get('fp_interacting') or '').upper() in ('TRUE','1')
    recent_event=str(snap.get('fp_event_type') or '').upper()
    ev_bars=num(snap.get('fp_bars_since_event'))
    if active and zid and side in ('DEMAND','SUPPLY'):
        sess=[]
        for row in reversed(path):
            if (str(row.get('fp_zone_id') or '').strip()!=zid or
                str(row.get('fp_side') or '').upper()!=side or
                str(row.get('fp_interacting') or '').upper() not in ('TRUE','1')):
                break
            sess.append(row)
        sess.reverse()
        if len(sess)<2:
            return f'{side.lower()} FP is being tested; one observed bar is insufficient to interpret delta accumulation.'
        earlier=num(sess[0].get('fp_session_delta'));later=num(sess[-1].get('fp_session_delta'))
        oldpx=num(sess[0].get('close'));newpx=num(sess[-1].get('close'))
        if earlier is None or later is None or oldpx is None or newpx is None or oldpx<=0:
            return f'{side.lower()} FP has {len(sess)} recorded interacting candles; delta buildup is unverified.'
        change=later-earlier
        price=(newpx-oldpx)/oldpx*100
        if abs(change)<1:
            return f'{side.lower()} FP is interacting; no measurable change in estimated in-zone delta yet.'
        pressure='buying' if change>0 else 'selling'
        expected=1 if change>0 else -1
        if abs(price)<0.08:
            effectiveness='but price has barely progressed; pressure effectiveness is unconfirmed'
        elif price*expected<0:
            effectiveness='yet price moved the opposite way; that pressure has not controlled price'
        else:
            effectiveness='with price travelling in the same direction'
        return f'{len(sess)}-bar {side.lower()} FP test: estimated delta {change:+,.0f} ({pressure}), {effectiveness}.'
    # Do not call the carried-forward FP event a new confirmed defence/break.
    if ev_bars is not None and 0<=ev_bars<=2 and any(x in recent_event for x in ('DEFENCE','BREAK','RUN')):
        event_label='defence' if 'DEFENCE' in recent_event else 'break/run'
        return f'Fresh {side.lower()} FP {event_label} is on the EDGE map; no ongoing in-zone delta test.'
    # Only add absence as a qualification if current flow is very strong.
    strength=abs(num(edge.get('exec_of_score')) or 0)
    if strength>=65:
        return 'No fresh FP interaction or usable in-zone delta buildup; the OF reading alone cannot prove absorption.'
    return ''


def _structure_evidence(edge:Dict[str,Any]) -> str:
    raw=jsondict(edge.get('raw_json'))
    pct=num(raw.get('battlefield_range_position_pct'))
    lower=num(raw.get('battlefield_map_lower'));upper=num(raw.get('battlefield_map_upper'))
    price=num(edge.get('close'))
    if pct is None and lower is not None and upper is not None and upper>lower and price is not None:
        pct=(price-lower)/(upper-lower)*100
    zone=''
    book=raw.get('fp_hurdle_book') or {}
    for z in (book.get('hurdles') or []) if isinstance(book,dict) else []:
        if not isinstance(z,dict) or str(z.get('status') or '').upper() in {'RUN','BROKEN','FAILED'}:
            continue
        dist=num(z.get('distance_atr'))
        score=num(z.get('strength_score'))
        side=str(z.get('side') or '').upper()
        if side not in {'DEMAND','SUPPLY'} or dist is None or abs(dist)>2.5:
            continue
        direction='supply overhead' if side=='SUPPLY' else 'demand below'
        score_note=f' (FP score {score:.0f})' if score is not None else ''
        zone=f'Nearby {str(z.get("strength") or "").lower()} {direction}{score_note}; no defence/break inferred from proximity.'
        break
    if pct is None:return zone
    if pct>100:location='above upper battlefield boundary; acceptance has not been established from location alone'
    elif pct<0:location='below lower battlefield boundary; acceptance has not been established from location alone'
    elif pct>=75:location='upper battlefield, supply is the opposing side'
    elif pct<=25:location='lower battlefield, demand is the defending side'
    else:location='middle battlefield, no boundary advantage'
    return 'Location: '+location+'.'+(' '+zone if zone else '')


def _liquidity_sequence_evidence(rows:List[Dict[str,Any]],edge:Dict[str,Any],now_ms:float) -> str:
    row=_best(rows,'LIQUIDITY',edge,now_ms)
    if not row:return ''
    current=_ms(edge.get('bar_time'))
    tf=num(edge.get('tf')) or 15
    if not current:return ''
    cutoff=max(45, min(180, 4*tf))*60000
    events=[]
    for i in range(1,6):
        name=f'liq_event{i}_'
        kind=str(row.get(name+'type') or '').upper()
        tm=_ms(row.get(name+'confirm_time'))
        lvl=num(row.get(name+'level'))
        if tm and 0<=current-tm<=cutoff and 'FULL' in kind and any(t in kind for t in ('RUN','SWEEP')):
            events.append((tm,kind,lvl))
    events.sort(reverse=True)
    if not events:return ''
    ups=sum('BUY_' in k for _,k,_ in events)
    downs=len(events)-ups
    latest=events[0]
    k=latest[1];lvl=latest[2];price=num(edge.get('close'))
    if 'SWEEP' in k and lvl is not None and price is not None:
        relation=('below' if price<lvl else 'above' if price>lvl else 'at')
        outcome=f'; price now {relation} that sweep level'
    else:outcome=''
    if ups>=2 and ups>downs:
        kind='buy-side';desc=f'{ups} recent buy-side full runs/sweeps'
    elif downs>=2 and downs>ups:
        kind='sell-side';desc=f'{downs} recent sell-side full runs/sweeps'
    else:
        kind='mixed';desc='mixed recent full liquidity events'
    newest=('sweep' if 'SWEEP' in k else 'run')
    return f'Liquidity: {desc}; latest {"buy-side" if "BUY_" in k else "sell-side"} {newest}{outcome}.'


def _aurora_evidence(rows:List[Dict[str,Any]],edge:Dict[str,Any],now_ms:float) -> str:
    row=_best(rows,'AURORA',edge,now_ms)
    if not row:return ''
    box=_box_read(row,edge)
    if box:
        enriched=row.copy()
        from superbrain_optional_context import aurora_box_fields
        enriched=aurora_box_fields(enriched)
        lo=num(enriched.get('htf_box_low') or enriched.get('htf_range_low'))
        hi=num(enriched.get('htf_box_high') or enriched.get('htf_range_high'))
        px=num(edge.get('close'))
        if lo is not None and hi is not None and hi>lo and px is not None:
            pct=(px-lo)/(hi-lo)*100
            loc=('above the upper' if pct>100 else 'below the lower' if pct<0 else
                 'near the upper' if pct>=75 else 'near the lower' if pct<=25 else 'inside the middle of the')
            prefix='Candle AURORA WHITE (chop); ' if str(row.get('aurora_state') or '').upper()=='WHITE' else 'Candle AURORA: '
            return f'{prefix}verified HTF box, price {loc} boundary.'
    if str(row.get('aurora_state') or '').upper()=='WHITE':
        return 'Candle AURORA WHITE flags choppy conditions; it is not a directional vote.'
    return ''


def _natural_control(edge: Dict[str,Any], path: List[Dict[str,Any]]) -> str:
    """Plain-English control: short-term pressure, nearby agreement, macro trend."""
    flow=of(edge.get('exec_of'))
    strong='STRONG' in str(edge.get('exec_of') or '').upper()
    if flow>0:
        lead='Short-term buying pressure is strong' if strong else 'Short-term buying pressure is active'
    elif flow<0:
        lead='Short-term selling pressure is strong' if strong else 'Short-term selling pressure is active'
    else:
        lead='Short-term buying and selling pressure are balanced'
    raw=jsondict(edge.get('raw_json'))
    routes={str(x.get('tf')):x for x in (raw.get('edge_router_map') or []) if isinstance(x,dict)}
    near=[of(routes[t].get('of_state')) for t in ('15','60','75') if t in routes and of(routes[t].get('of_state'))]
    if flow and sum(v==flow for v in near)>=2:
        lead+=', and nearby timeframes mostly agree'
    elif flow and sum(v==-flow for v in near)>=2:
        lead+=', although nearby timeframes disagree'
    elif near and flow:
        lead+=', with mixed support from nearby timeframes'
    macro=str(edge.get('macro') or '').upper()
    if macro in ('BULLISH','BEARISH'):
        macro_direction=1 if macro=='BULLISH' else -1
        if macro_direction==flow:
            lead+=f'. The macro trend is {macro.lower()} too'
        elif macro_direction==-flow:
            lead+=f'. The macro trend is {macro.lower()}, against the short-term pressure'
        else:
            lead+=f'. The macro trend is {macro.lower()}'
    scores=[num(r.get('exec_of_score')) for r in path]
    scores=[x for x in scores if x is not None]
    if len(scores)>=10 and flow:
        n=min(6,len(scores)//3)
        old=median(scores[:n]);recent=median(scores[-n:]);change=recent-old
        if old*recent>0 and abs(change)>=15:
            if (old<0 and change>0) or (old>0 and change<0):
                lead+='. Short-term pressure has started to weaken'
            else:
                lead+='. Short-term pressure is strengthening'
    return lead+'.'


def _natural_response(label: str, path: List[Dict[str,Any]],
                      progress: Dict[str,float]) -> str:
    """Explain whether buying/selling pressure is working; avoid indicator jargon."""
    if not progress or len(path)<5:
        return 'There is not enough continuous EDGE history yet to tell whether this pressure is working.'
    n=len(path)
    bear=sum(of(r.get('exec_of'))<0 for r in path)/n
    bull=sum(of(r.get('exec_of'))>0 for r in path)/n
    move=progress['move'];threshold=progress['threshold']
    label=label.upper()
    if 'UPSIDE FAILURE' in label:
        sentence='Buyers drove an earlier rise, but most of those gains have been lost. Buying pressure remains strong, yet buyers are struggling to hold progress.'
    elif 'DOWNSIDE FAILURE' in label:
        sentence='Sellers drove an earlier fall, but most of that decline has been recovered. Selling pressure remains, yet sellers are struggling to hold progress.'
    elif bear>=.70 and move<=-threshold:
        sentence='Selling pressure is working: price has continued lower during the observed move.'
    elif bear>=.70 and move>=threshold:
        sentence='Selling pressure has not worked: price has climbed despite persistent sellers.'
    elif bull>=.70 and move>=threshold:
        sentence='Buying pressure is working: price has continued higher during the observed move.'
    elif bull>=.70 and move<=-threshold:
        sentence='Buying pressure has not worked: price has fallen despite persistent buyers.'
    elif bear>=.70:
        sentence='Sellers have not made much progress despite repeated selling pressure.'
    elif bull>=.70:
        sentence='Buyers have not made much progress despite repeated buying pressure.'
    else:
        sentence='Neither buyers nor sellers have gained lasting ground during the observed move.'
    if 'REBOUND DEVELOPING' in label:
        sentence+=' A rebound is starting, but sellers have not lost control yet.'
    elif 'REVERSAL RISK' in label:
        sentence+=' The move against the trend is now large enough to warn of a possible change.'
    elif 'PULLBACK' in label or ('FADING' in label and label.startswith('BULLISH')):
        sentence+=' The recent pullback needs watching, but has not yet erased the bigger recovery.'
    elif 'FADING' in label and label.startswith('BEARISH'):
        sentence+=' The rebound is challenging the bearish trend.'
    return sentence


def _natural_fp(edge: Dict[str,Any], path: List[Dict[str,Any]]) -> str:
    """Read delta against price behaviour at the same FP, never equate delta with a break.

    A negative cumulative delta at demand + limited downside may suggest
    possible absorption, not proof. A single observation cannot show buildup.
    """
    if not path:return ''
    cur=_snap_tape(edge)
    zid=str(cur.get('fp_zone_id') or '').strip()
    side=str(cur.get('fp_side') or '').upper()
    if side not in ('DEMAND','SUPPLY') or not zid:return ''
    active=str(cur.get('fp_interacting') or '').upper() in ('TRUE','1')
    name='Demand' if side=='DEMAND' else 'Supply'
    if active:
        session=[]
        for row in reversed(path):
            if (str(row.get('fp_zone_id') or '').strip()!=zid or
                str(row.get('fp_side') or '').upper()!=side or
                str(row.get('fp_interacting') or '').upper() not in ('TRUE','1')):break
            session.append(row)
        session.reverse()
        d_now=num(cur.get('fp_session_delta'))
        status=str(cur.get('fp_context_state') or '').upper()
        event=str(cur.get('fp_event_type') or '').upper()
        pending='BREACH' in status or 'BREACH' in event or str(cur.get('fp_breach_pending') or '').upper() in ('TRUE','1')
        prefix=(f'{name} is under a breach test, not a confirmed break.' if pending
                else f'{name} is being tested.')
        if len(session)<2:
            if d_now is not None and d_now<0 and side=='DEMAND':
                return prefix+' FP delta is negative; if price stops falling or reclaims Demand, this may indicate absorption. One reading cannot confirm it.'
            if d_now is not None and d_now>0 and side=='SUPPLY':
                return prefix+' FP delta is positive; if price stops rising, this may indicate absorption. One reading cannot confirm it.'
            return prefix+' There is not enough recorded delta change yet to judge absorption.'
        d0=num(session[0].get('fp_session_delta'));d1=num(session[-1].get('fp_session_delta'))
        p0=num(session[0].get('close'));p1=num(session[-1].get('close'))
        if d0 is None or d1 is None or p0 is None or p1 is None or not p0:
            return prefix+' FP delta is not clear enough to judge absorption.'
        change=(p1-p0)/abs(p0)*100
        difference=d1-d0
        if abs(difference)<1:
            return prefix+' FP delta has not built up enough to show who is gaining ground.'
        if side=='DEMAND' and difference<0:
            if change>=-.08:
                return prefix+' Selling delta is building, but price is holding up. This could be buying absorption at Demand, not a confirmed reversal.'
            return prefix+' Selling delta is building and price is falling; sellers are still making progress.'
        if side=='SUPPLY' and difference>0:
            if change<=.08:
                return prefix+' Buying delta is building, but price is not advancing. This could be selling absorption at Supply, not a confirmed reversal.'
            return prefix+' Buying delta is building and price is rising; buyers are still making progress.'
        if d1<0 and difference>0:
            return prefix+' FP delta remains negative but selling pressure is easing. Watch whether Demand holds.'
        if d1>0 and difference<0:
            return prefix+' FP delta remains positive but buying pressure is easing. Watch whether Supply holds.'
        return prefix+' FP delta is changing, but the result is not yet clear.'
    tm=_ms(cur.get('fp_event_time'));now=_ms(cur.get('bar_ms'))
    bars=num(cur.get('fp_bars_since_event'));tf=num(cur.get('tf'))
    ev=str(cur.get('fp_event_type') or '').upper()
    if (tm is not None and now is not None and tf and bars is not None
        and 0<=bars<=2 and 0<=now-tm<=2.5*tf*60000):
        if ev=='RUN_CONFIRMED':return f'{name} has a confirmed FP break; watch whether price stays beyond it.'
        if ev=='DEFENCE_CONFIRMED':return f'{name} has just been defended; watch whether that hold continues.'
        if 'BREACH' in ev:return f'{name} is under a breach test, but EDGE has not confirmed a break.'
    return ''


def _natural_location(edge:Dict[str,Any]) -> str:
    raw=jsondict(edge.get('raw_json'))
    pct=num(raw.get('battlefield_range_position_pct'))
    if pct is None:return ''
    if pct>=75 and pct<=110:return 'Price is in the upper battlefield, with Supply still ahead.'
    if pct<=25 and pct>=-10:return 'Price is in the lower battlefield, where Demand matters most.'
    if pct>110:return 'Price is beyond the upper battlefield boundary, but acceptance there is not yet established.'
    if pct<-10:return 'Price is beyond the lower battlefield boundary, but acceptance there is not yet established.'
    return ''


def _natural_liquidity(tv_rows:List[Dict[str,Any]],edge:Dict[str,Any],now_ms:float) -> str:
    row=_best(tv_rows,'LIQUIDITY',edge,now_ms)
    if not row:return ''
    current=_ms(edge.get('bar_time'))
    tf=num(edge.get('tf')) or 15
    if not current:return ''
    cutoff=max(45,min(180,4*tf))*60_000
    events=[]
    for i in range(1,6):
        kind=str(row.get(f'liq_event{i}_type') or '').upper()
        tm=_ms(row.get(f'liq_event{i}_confirm_time'))
        lvl=num(row.get(f'liq_event{i}_level'))
        if tm and 0<=current-tm<=cutoff and 'FULL' in kind and ('RUN' in kind or 'SWEEP' in kind):
            events.append((tm,kind,lvl))
    if not events:return ''
    events.sort(reverse=True)
    ups=sum('BUY_' in k for _,k,_ in events);downs=sum('SELL_' in k for _,k,_ in events)
    if ups>=2 and ups>downs:sentence='Recent Liquidity runs and sweeps have focused on the upside'
    elif downs>=2 and downs>ups:sentence='Recent Liquidity runs and sweeps have focused on the downside'
    else:return ''  # isolated/mixed noise should not crowd the core reading
    tm,latest,lvl=events[0];spot=num(edge.get('close'))
    if lvl is not None and spot is not None and 'SWEEP' in latest:
        if 'BUY_' in latest and spot<lvl:
            sentence+=', with the latest sweep rejected'
        elif 'SELL_' in latest and spot>lvl:
            sentence+=', followed by a recovery above the sweep'
    elif lvl is not None and spot is not None and 'RUN' in latest:
        if 'BUY_' in latest and spot>lvl:sentence+=', with price remaining above the latest run'
        if 'SELL_' in latest and spot<lvl:sentence+=', with price remaining below the latest run'
    return sentence+'.'


def _natural_aurora(tv_rows:List[Dict[str,Any]],edge:Dict[str,Any],now_ms:float) -> str:
    row=_best(tv_rows,'AURORA',edge,now_ms)
    if not row:return ''
    boxed=_box_read(row,edge)
    white=str(row.get('aurora_state') or '').upper()=='WHITE'
    if boxed:
        msg=str(boxed[0]).lower()
        if 'inside the box' in msg:return 'Price is inside an HTF box; directional conviction is lower.'
        if 'above' in msg:return 'Price is testing beyond a verified higher-timeframe box; a sustained escape is not yet established.'
        if 'below' in msg:return 'Price is testing below a verified higher-timeframe box; a sustained escape is not yet established.'
        return 'A verified higher-timeframe box is relevant to the current structure.'
    if white:return 'AURORA WHITE suggests sideways conditions, so wait for clearer direction.'
    return ''


def _conviction_commentary(edge:Dict[str,Any],path:List[Dict[str,Any]],
                           previous:Dict[str,str],tv_rows:List[Dict[str,Any]],
                           now_ms:float) -> str:
    """Short natural-language appraisal. Indicator values stay in metadata, not prose.

    Preserve distinct evidence: OF + timeframes, realised price effectiveness,
    real FP/delta interaction, and only relevant fresh optional observations.
    """
    label=str(previous.get('label') or '')
    progress=_movement(path) if len(path)>=5 else {}
    control=_natural_control(edge,path)
    outcome=_natural_response(label,path,progress)
    fp=_natural_fp(edge,path)
    location=_natural_location(edge)
    liquidity=_natural_liquidity(tv_rows,edge,now_ms)
    aurora=_natural_aurora(tv_rows,edge,now_ms)
    # FP response is more decisive than general battlefield location. Ignore
    # optional noise if it does not materially change the central assessment.
    context=[]
    if fp:context.append(fp)
    elif location:context.append(location)
    if liquidity:context.append(liquidity)
    if aurora:context.append(aurora)
    if not fp and not liquidity and not aurora and progress and abs(num(edge.get('exec_of_score')) or 0)>=65:
        if of(edge.get('exec_of'))<0:
            context.append('No fresh demand defence or in-zone delta response is currently confirmed.')
        elif of(edge.get('exec_of'))>0:
            context.append('No fresh supply rejection or in-zone delta response is currently confirmed.')
    # Two readable paragraphs, no raw score/timeframe/price lists. Cap to the
    # most relevant context, but retain observed FP and fresh Liquidity first.
    first=control+' '+outcome
    second=' '.join(context[:3])
    return first+('\n\n'+second if second else '')

def intelligent_bias(edge:Dict[str,Any],history:List[Dict[str,Any]],
                     tv_rows:List[Dict[str,Any]],odme:Optional[Dict[str,Any]]=None,
                     edge_fresh:bool=True,now_ms:Optional[float]=None) -> Dict[str,str]:
    """Directional intelligence only. NO entries or campaign advice."""
    if not edge_fresh or not edge or str(edge.get('edge_tracking_state') or '').upper()!='ACTIVE':
        return {'label':'UNAVAILABLE','basis':''}
    if str(edge.get('tf'))!=str(edge.get('edge_required_exec_tf') or edge.get('exec_tf') or edge.get('tf')):
        return {'label':'UNAVAILABLE','basis':''}
    px=num(edge.get('close'))
    if px is None:return {'label':'UNAVAILABLE','basis':''}
    now_ms=now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp()*1000
    a=_best(tv_rows,'AURORA',edge,now_ms)
    if a:
        box=_box_read(a,edge)
        if box:
            # AURORA only provides range location. Its color does not vote on direction.
            box_message=box[0]
            if 'inside the box' in box_message:
                return {'label':'RANGE / CHOPPY','basis':'Inside confirmed HTF range; wait near an edge or confirmed break.', 'commentary':_conviction_commentary(edge,_clean_path(edge,history),{'commentary':'Inside the verified HTF box; EDGE directional pressure alone does not confirm escape.'},tv_rows,now_ms)}
            box_note='HTF box boundary active'
        else:
            box_note=''
        if str(a.get('aurora_state') or '').upper()=='WHITE':
            return {'label':'CHOPPY / SIDEWAYS','basis':'Candle AURORA WHITE; directional conviction is poor.', 'commentary':_conviction_commentary(edge,_clean_path(edge,history),{'commentary':'AURORA WHITE warns of chop; a clean EDGE breakout is not confirmed from the color alone.'},tv_rows,now_ms)}
    else:
        box_note=''
    flow=of(edge.get('exec_of'))
    htf=_htf(edge)
    place,near_supply,near_demand=_location(edge)
    tape,anchor_type=_journey(edge,history)
    clue=''
    label=''
    strong=False
    # Longitudinal evidence is separate from short execution control. A long
    # movement against persistent OF should not be erased by one pullback or
    # by a fixed 25-candle sliding window.
    progress=_movement(tape) if len(tape)>=5 else {}
    if progress:
        move=progress['move']
        threshold=progress['threshold']
        bearshare=progress['bearshare']
        bullshare=progress['bullshare']
        # Control effectiveness can reverse while the current OF remains
        # bullish/bearish. A meaningful rally that was mostly surrendered is
        # NOT fresh bullish confirmation; the mirrored rule holds for shorts.
        if (bullshare>=0.70 and progress['peak_gain']>=max(0.50,2*threshold)
            and progress['retrace_up']>=0.70 and move<threshold):
            label='BULLISH PRESSURE · UPSIDE FAILURE'
            clue='sustained buying failed to retain the earlier advance'
        elif (bearshare>=0.70 and progress['trough_drop']>=max(0.50,2*threshold)
              and progress['retrace_down']>=0.70 and move>-threshold):
            label='BEARISH PRESSURE · DOWNSIDE FAILURE'
            clue='sustained selling failed to retain the earlier decline'
        elif bearshare>=0.70 and move>=threshold:
            label='BULLISH RECOVERY'
            clue='price advancing despite persistent bearish OF'
            if progress['retrace_up']>=0.55 and progress['latest_move']<0:
                label='BULLISH RECOVERY · FADING'
                clue='prior recovery has lost much of its advance'
        elif bullshare>=0.70 and move<=-threshold:
            label='BEARISH REJECTION'
            clue='price falling despite persistent bullish OF'
            if progress['retrace_down']>=0.55 and progress['latest_move']>0:
                label='BEARISH REJECTION · FADING'
                clue='prior decline has retraced substantially'
        elif bearshare>=0.70 and move<=-threshold:
            label='BEARISH' if htf<=0 else 'BEARISH CORRECTION'
            clue='selling pressure is achieving downside progress'
            strong=htf<0 and move<=-0.8
        elif bullshare>=0.70 and move>=threshold:
            label='BULLISH'
            clue='buying pressure is achieving upside progress'
            strong=htf>0 and move>=0.8
        # If a substantial opposing move has retraced far enough, no longer
        # proclaim the old direction just because its initial journey was long.
        elif (bearshare>=0.70 and progress['peak_gain']>=2*threshold
              and progress['retrace_up']>=0.65):
            label='MIXED / RECOVERY FADING'
            clue='earlier upside progress is being retraced'
        elif (bullshare>=0.70 and progress['trough_drop']>=2*threshold
              and progress['retrace_down']>=0.65):
            label='MIXED / REJECTION FADING'
            clue='earlier downside move is being retraced'
    # In-zone cumulative FP delta matters only during genuinely recorded,
    # contiguous interactions with the *same* zone. Do not reuse a stale zero.
    if progress and not clue:
        sessions=[]
        session=[]
        for i,r in enumerate(tape):
            zone=str(r.get('fp_zone_id') or '').strip()
            active=str(r.get('fp_interacting') or '').upper() in ('TRUE','1')
            delta=num(r.get('fp_session_delta'))
            if active and zone and delta is not None:
                if session and zone!=session[-1][1]:
                    sessions.append(session);session=[]
                session.append((delta,zone,i))
            elif session:
                sessions.append(session);session=[]
        if session:sessions.append(session)
        if sessions:
            latest=sessions[-1]
            if (len(latest)>=3 and len(tape)-1-latest[-1][2]<=2
                and abs(latest[-1][0]-latest[0][0])>0
                and abs(progress['move'])<0.10):
                clue='FP pressure accumulating without proportional price progress'
    if not label:
        if flow and htf and flow!=htf:
            label='BEARISH CORRECTION' if flow<0 else 'BULLISH REBOUND'
            clue=clue or 'execution OF opposes higher-timeframe control'
        elif flow:
            label='BULLISH' if flow>0 else 'BEARISH'
            clue=clue or 'execution pressure currently leads'
        elif htf:
            label='BULLISH LEAN' if htf>0 else 'BEARISH LEAN'
            clue=clue or 'higher-timeframe control leads'
        else:
            label='MIXED / RANGE'
            clue='no clear EDGE control'
    # The journey has a location history: a *newly observed* FP defence with
    # subsequent favourable price travel can explain where this move began.
    # A carried-forward DEFENCE_CONFIRMED from 60 bars ago does not qualify.
    if len(tape)>=5:
        defended=[]
        for r in tape[:-1]:
            side=str(r.get('fp_side') or '').upper()
            event=str(r.get('fp_event_type') or '').upper()
            bars=num(r.get('fp_bars_since_event'))
            start=num(r.get('close'))
            if side in ('DEMAND','SUPPLY') and 'DEFENCE' in event and bars is not None and bars<=2 and start:
                defended.append((side,start))
        if defended:
            side,start=defended[-1]
            change=(px-start)/start*100
            if side=='DEMAND' and change>=0.25 and label.startswith('BULLISH'):
                clue='price has recovered from defended demand' + (' despite bearish OF' if flow<0 else '')
            elif side=='SUPPLY' and change<=-0.25 and label.startswith('BEARISH'):
                clue='price has retreated from defended supply' + (' despite bullish OF' if flow>0 else '')
    if strong:label='STRONG '+label
    if near_supply and label.startswith('BULLISH') or near_demand and label.startswith('BEARISH'):
        label+=' · CAUTION'
        clue=clue+'; approaching opposing structure'
    liq=_recent_liq(tv_rows,edge,now_ms)
    if liq and ('recovery' in label.lower() or near_supply or near_demand):
        clue=clue+'; '+liq
    if box_note and not clue:clue=box_note
    if odme:
        od_dir=str(odme.get('direction') or '').upper()
        if od_dir in ('BULLISH','BEARISH') and of(od_dir)==-flow and flow:
            # ODME may temper conviction; it doesn't erase price-history evidence.
            if label.startswith('STRONG '):label=label[7:]
            clue=clue+'; ODME differs'
    explained=_journey_commentary(label,clue,tape,progress,edge)
    return {'label':explained['label'],'basis':clue[:185],
        'commentary':_conviction_commentary(edge,tape,explained,tv_rows,now_ms),
        'evidence_bars':str(len(tape)), 'temporal':str(len(tape)>=5),
        'journey_anchor':anchor_type,
        'journey_pct':str(round(progress.get('move',0),3)) if progress else '',
        'mtf_summary':_mtf_evidence(edge),
        'fp_summary':_fp_evidence(edge,tape),
        'liquidity_summary':_liquidity_sequence_evidence(tv_rows,edge,now_ms),
        'aurora_summary':_aurora_evidence(tv_rows,edge,now_ms)}
