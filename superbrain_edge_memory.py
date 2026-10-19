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
        if (tm is not None and first_ms<=tm<=last_ms
            and 0<=ms-tm<=1.7*tf_ms
            and (bars is None or bars<=2)
            and any(k in ev for k in ('DEFENCE','BREAK','RUN','RECLAIM','BREACH'))):
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
    # Latest genuinely witnessed event/interaction, not the latest stale label.
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
                return {'label':'RANGE / CHOPPY','basis':'Inside confirmed HTF range; wait near an edge or confirmed break.'}
            box_note='HTF box boundary active'
        else:
            box_note=''
        if str(a.get('aurora_state') or '').upper()=='WHITE':
            return {'label':'CHOPPY / SIDEWAYS','basis':'Candle AURORA WHITE; directional conviction is poor.'}
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
        if bearshare>=0.70 and move>=threshold:
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
    return {'label':label,'basis':clue[:185],
        'evidence_bars':str(len(tape)), 'temporal':str(len(tape)>=5),
        'journey_anchor':anchor_type,
        'journey_pct':str(round(progress.get('move',0),3)) if progress else ''}
