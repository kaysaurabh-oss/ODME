"""EDGE journey: display-only longitudinal intelligence from an append-only EDGE candle tape.

The Telegram scheduler writes the tape. Streamlit reads it. No effect on existing
setup recognition, option strikes, campaign mutation or Level 2 alert decisions.
Never reconstruct missing candles and never treat estimated FP delta as executed
bid/ask volume. Missing history yields a clearly bounded snapshot-only view.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def read_edge_tape(store: Any, instrument: str, max_rows: int=1500) -> List[Dict[str, Any]]:
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
        return rows[-160:]
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
        'fp_bars_since_event':edge.get('fp_bars_since_event')}


def _clean_path(edge:Dict[str,Any],hist:List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """Only contiguous matching execution-TF bars, reject stale or forward readings."""
    cur=_snap_tape(edge)
    current_ms=_ms(cur.get('bar_ms'))
    tf=num(cur.get('tf'))
    if not current_ms or not tf:return []
    allowed=1.7*tf*60000
    # Include the confirmed current candle even when scheduler lags one minute.
    seen={}
    for r in hist+[cur]:
        ms=_ms(r.get('bar_ms'))
        if not ms or ms>current_ms or ms<current_ms-8*60*60000:continue
        if num(r.get('tf'))!=tf:continue
        if str(r.get('instrument') or '').upper()!=str(cur.get('instrument') or '').upper():continue
        seen[ms]=r
    ordered=[seen[k] for k in sorted(seen)]
    if not ordered:return []
    chain=[ordered[-1]]
    for r in reversed(ordered[:-1]):
        if _ms(chain[-1]['bar_ms'])-_ms(r['bar_ms'])>allowed:break
        chain.append(r)
        if len(chain)>=25:break
    return list(reversed(chain))


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
    tape=_clean_path(edge,history)
    clue=''
    label=''
    strong=False
    # Require at least 5 sequential confirmed real updates before making claims
    # about pressure failure/response. A TV_TEST_CURRENT row is not a journey.
    if len(tape)>=5:
        first=num(tape[0].get('close'))
        last=num(tape[-1].get('close'))
        if first and last:
            move=(last-first)/first*100
            obs=tape[:-1]
            bear_share=sum(of(r.get('exec_of'))<0 for r in obs)/len(obs)
            bull_share=sum(of(r.get('exec_of'))>0 for r in obs)/len(obs)
            if bear_share>=0.70 and move>=0.24:
                label='BULLISH RECOVERY'
                clue='price is rising despite persistent bearish OF'
            elif bull_share>=0.70 and move<=-0.24:
                label='BEARISH REJECTION'
                clue='price is falling despite persistent bullish OF'
            elif bear_share>=0.70 and move<=-0.35:
                label='BEARISH' if htf<=0 else 'BEARISH CORRECTION'
                clue='selling pressure is achieving downside progress'
                strong=htf<0 and move<=-0.8
            elif bull_share>=0.70 and move>=0.35:
                label='BULLISH'
                clue='buying pressure is achieving upside progress'
                strong=htf>0 and move>=0.8
            # FP in-zone delta progress is meaningful only if the zone is being
            # interacted with and the same zone persists between observations.
            if not clue:
                delta=[(num(r.get('fp_session_delta')),str(r.get('fp_zone_id') or ''),str(r.get('fp_interacting') or '').upper()) for r in tape]
                active=[t for t in delta if t[0] is not None and t[2] in ('TRUE','1') and t[1]]
                if len(active)>=3 and len({t[1] for t in active})==1:
                    start,end=active[0][0],active[-1][0]
                    if abs(end-start)>0 and abs(move)<0.10:
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
        'evidence_bars':str(len(tape)), 'temporal':str(len(tape)>=5)}
