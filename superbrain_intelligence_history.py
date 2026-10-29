"""Read-only SuperBrain intelligence from the independently recorded optional tape.

The Apps Script Level-2 scheduler continuously records real optional source
observations; app scans only READ that tape, EDGE history and ODME snapshots.
No setup, strike, management, signal, broker or Telegram code imports this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any, Dict, List, Optional

OPTIONAL_TAB = "SB_OPTIONAL_TAPE"
OPTIONAL_HEADERS = [
    "observation_key", "recorded_at", "instrument", "source", "tf",
    "bar_ms", "confirmed_close_ms", "price", "state", "change",
    "last_event", "event_ms", "context_json",
]
MAX_SCAN_HISTORY = 15000  # supports EDGE-style 600 per instrument+source across many instruments
MAX_ODME_HISTORY = 10000
MAX_AGE_MS = 90 * 24 * 3600 * 1000


def _num(x: Any) -> Optional[float]:
    try:
        f=float(x)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError):
        return None


def _ms(x: Any) -> Optional[int]:
    if isinstance(x,str) and not x.strip().replace('.','',1).isdigit():
        try:
            s=x.replace('Z','+00:00')
            dt=datetime.fromisoformat(s)
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp()*1000)
        except (ValueError, TypeError):return None
    n=_num(x)
    if n is None or n<=0:return None
    return int(n*1000 if n<1e11 else n)


def _json_obj(x: Any) -> Dict[str,Any]:
    if isinstance(x,dict):return x
    try:
        y=json.loads(str(x or ''))
        return y if isinstance(y,dict) else {}
    except (ValueError,TypeError):return {}


def _as_records(rows: Any) -> List[Dict[str,Any]]:
    if isinstance(rows,list):return [r for r in rows if isinstance(r,dict)]
    if hasattr(rows,'to_dict'):
        try:return rows.to_dict('records')
        except Exception:return []
    return []


def read_optional_history(store: Any, instrument: str, edge_row: Dict[str,Any]) -> List[Dict[str,Any]]:
    """READ ONLY. Return confirmed, nonfuture optional history; no app-side writes.

    Read in reverse chunks to avoid fetching a large shared tape unnecessarily.
    The tape retains up to 600 rows per (instrument, source) upstream.
    """
    edge_ms=_ms(edge_row.get('bar_time'))
    if not edge_ms:return []
    try:
        ws=store.sheet.worksheet(OPTIONAL_TAB)
        if ws.row_values(1)[:len(OPTIONAL_HEADERS)]!=OPTIONAL_HEADERS:return []
        count=len(ws.col_values(1))
        if count<=1:return []
        out=[]
        wanted=str(instrument).upper()
        end=count
        floor=max(2,count-MAX_SCAN_HISTORY+1)
        while end>=floor:
            start=max(floor,end-1999)
            rows=ws.get(f'A{start}:M{end}')
            for v in rows:
                d={k:v[i] if i<len(v) else '' for i,k in enumerate(OPTIONAL_HEADERS)}
                t=_ms(d['confirmed_close_ms'])
                if (str(d['instrument']).upper()==wanted and
                    str(d['source']).upper() in ('AURORA','LIQUIDITY') and
                    t and edge_ms-MAX_AGE_MS<=t<=edge_ms):
                    d['context']=_json_obj(d['context_json'])
                    out.append(d)
            if sum(str(r['source']).upper()=='AURORA' for r in out)>=180 and \
               sum(str(r['source']).upper()=='LIQUIDITY' for r in out)>=180:
                break
            end=start-1
        out.sort(key=lambda x:int(x['confirmed_close_ms']))
        # No false AURORA state changes across source-timeframe migrations.
        selected=[]
        for src in ('AURORA','LIQUIDITY'):
            own=[r for r in out if str(r['source']).upper()==src]
            if own:
                latest_tf=str(own[-1]['tf'])
                selected+= [r for r in own if str(r['tf'])==latest_tf][-180:]
        selected.sort(key=lambda x:int(x['confirmed_close_ms']))
        return selected
    except Exception:return []


def read_odme_snapshot_history(store: Any, instrument: str,
                               edge_row: Dict[str,Any], enabled: bool=True) -> List[Dict[str,Any]]:
    """Reuses existing odme_snapshots. No duplicate storage, no raw chain scans."""
    if not enabled:return []
    edge_ms=_ms(edge_row.get('bar_time'))
    if not edge_ms:return []
    try:
        df=store.load_odme_history(limit=MAX_ODME_HISTORY)
        rows=_as_records(df)
        arr=[]
        for r in rows:
            ms=_ms(r.get('ts'))
            if str(r.get('instrument') or '').upper()!=instrument.upper() or not ms:
                continue
            # Never read a future ODME reading into a historical as-of EDGE candle.
            if edge_ms-MAX_AGE_MS<=ms<=edge_ms+max(60_000,int((_num(edge_row.get('tf')) or 15)*60_000)):arr.append(dict(r, _ts_ms=ms))
        arr.sort(key=lambda x:x['_ts_ms'])
        return arr[-45:]
    except Exception:return []


def _window_evidence(edge: Dict[str,Any], tape: List[Dict[str,Any]], hours: float=4) -> Dict[str,Any]:
    now=_ms(edge.get('bar_time'))
    px=_num(edge.get('close'))
    tf=_num(edge.get('tf'))
    if not now or px is None or not tf:return {}
    g=[]
    for r in tape:
        t=_ms(r.get('bar_ms'))
        p=_num(r.get('close'))
        if not t or p is None or r.get('synthetic_price_only') or t>now:continue
        if str(r.get('instrument') or edge.get('instrument') or '').upper()!=str(edge.get('instrument') or '').upper():continue
        g.append((t,p,str(r.get('exec_of') or '')))
    g.append((now,px,str(edge.get('exec_of') or '')))
    g=sorted({z[0]:z for z in g}.values())
    target=now-int(hours*3600*1000)
    near=[z for z in g if z[0]<=target]
    if not near:return {}
    anchor=near[-1]
    if target-anchor[0]>max(2*tf*60000,20*60000):return {}
    interval=[z for z in g if anchor[0]<=z[0]<=now]
    if len(interval)<4:return {}
    # Not interpreting missing closed-session bars as actual pressure readings.
    max_gap=max((b[0]-a[0] for a,b in zip(interval,interval[1:])),default=0)
    if max_gap>max(4*tf*60000,45*60000):return {}
    start=anchor[1]
    drift=100*(px/start-1) if start else None
    high=max(z[1] for z in interval)
    low=min(z[1] for z in interval)
    width=100*(high-low)/start if start else None
    sign=-1 if 'BEAR' in str(edge.get('exec_of') or '').upper() else 1 if 'BULL' in str(edge.get('exec_of') or '').upper() else 0
    agreement=sum(1 for _,_,s in interval if ('BEAR' in s and sign==-1) or ('BULL' in s and sign==1)) / len(interval) if sign else 0
    return {'hours':hours,'count':len(interval),'drift_pct':drift,'range_pct':width,
            'flow_agreement':agreement,'dir':sign}


def interpret_historical_context(edge: Dict[str,Any], edge_tape: List[Dict[str,Any]],
                                 optional_history: List[Dict[str,Any]],
                                 odme_history: List[Dict[str,Any]]) -> str:
    """Display-only evidence, not a score/gate, prediction or trade instruction."""
    paragraphs=[]
    window=_window_evidence(edge,edge_tape)
    if window:
        move=window['drift_pct']
        span=window['range_pct']
        dir=window['dir']
        agree=window['flow_agreement']
        if dir and agree>=0.75 and span>=0.50 and abs(move)<=0.32*span:
            pressure='selling' if dir==-1 else 'buying'
            paragraphs.append(f'**Pressure versus price:** {pressure.capitalize()} dominated most of the last four hours, '
                              f'but net price progress was only {move:+.2f}% across a {span:.2f}% observed range. '
                              'The pressure is not translating into sustained progress; consolidation or reversal risk deserves attention. '
                              'This does not confirm a reversal.')
        elif dir and agree>=0.75 and ((dir==-1 and move<=-0.6) or (dir==1 and move>=0.6)):
            paragraphs.append(f'**Pressure versus price:** EDGE control produced {move:+.2f}% net progress '
                              'over the last four observed hours; continuation is currently effective, not just a pressure label.')
    event=str(edge.get('fp_event_type') or '').upper()
    age=_num(edge.get('fp_bars_since_event'))
    if event=='RUN_CONFIRMED' and age is not None and age>3:
        paragraphs.append(f'**FP maturity:** The reported {str(edge.get("fp_side") or "").lower()} FP break is '
                          f'{int(age)} confirmed EDGE bars old. It is structural history, not a new breakout trigger.')
    if odme_history:
        latest=odme_history[-1]
        last_ms=latest.get('_ts_ms') or _ms(latest.get('ts'))
        edge_ms=_ms(edge.get('bar_time'))
        if last_ms and edge_ms and edge_ms-last_ms<=4*24*3600*1000:
            expiry=str(latest.get('expiry') or '').upper()
            old=next((v for v in reversed(odme_history[:-1]) if
                      str(v.get('expiry') or '').upper()==expiry and
                      str(v.get('snapshot_id'))!=str(latest.get('snapshot_id'))),None)
            tilt=str(latest.get('odme_tilt') or '').strip()
            if old:
                previous=str(old.get('odme_tilt') or '').strip()
                if tilt and previous and tilt!=previous:
                    paragraphs.append(f'**ODME evolution:** Positioning changed from {previous} to {tilt} '
                                      'across saved snapshots. This tempers conviction; it does not override EDGE.')
                else:
                    ce0=_num(old.get('ce_wall'));ce1=_num(latest.get('ce_wall'))
                    pe0=_num(old.get('pe_wall'));pe1=_num(latest.get('pe_wall'))
                    if ce0 is not None and ce1 is not None and pe0 is not None and pe1 is not None:
                        parts=[]
                        if ce1!=ce0:parts.append('CE wall moved '+('higher' if ce1>ce0 else 'lower'))
                        if pe1!=pe0:parts.append('PE wall moved '+('higher' if pe1>pe0 else 'lower'))
                        if parts:paragraphs.append('**ODME evolution:** '+', '.join(parts)+'. Watch whether price confirms the shift.')
            else:
                paragraphs.append('**ODME history:** One saved snapshot is available; no positioning trend can be inferred.')
    # Evaluate actual confirmed optional sequences—not only the current color or
    # last event label. Never infer an event from carried-forward snapshots.
    edge_ms=_ms(edge.get('bar_time'))
    for src in ('AURORA','LIQUIDITY'):
        own=sorted((r for r in optional_history if str(r.get('source') or '').upper()==src),
                   key=lambda r:_ms(r.get('confirmed_close_ms')) or 0)
        if len(own)<2 or not edge_ms:continue
        latest=own[-1]
        latest_ms=_ms(latest.get('confirmed_close_ms'))
        tf=_num(latest.get('tf')) or _num(edge.get('tf')) or 60
        # An abandoned optional feed never gets to masquerade as live context.
        if not latest_ms or edge_ms-latest_ms>max(2*tf*60_000,90*60_000):continue
        if src=='AURORA':
            state=str(latest.get('state') or '').upper()
            if not state:continue
            same=0
            for r in reversed(own):
                if str(r.get('state') or '').upper()!=state:break
                same+=1
            if same<len(own) and same<=8:
                old=str(own[-same-1].get('state') or '').upper()
                if old:
                    paragraphs.append(f'**Candle AURORA journey:** {old} → {state} '
                                      f'({same} observed {int(tf)}m bar(s) in the new state). '
                                      'A confirmed context transition, not a trade trigger.')
            elif state=='WHITE' and same>=4:
                paragraphs.append(f'**Candle AURORA journey:** WHITE has persisted across '
                                  f'{same} saved {int(tf)}m bars. Treat the color as '
                                  'persistent range/chop context, not an automatic reversal.')
        else:
            unique=[]
            seen=set()
            for r in own:
                typ=str(r.get('last_event') or '').upper()
                t=_ms(r.get('event_ms'))
                if not typ or not t or t>edge_ms or edge_ms-t>24*3600_000:continue
                k=(typ,t)
                if k not in seen:
                    seen.add(k);unique.append((typ,t,r))
            if not unique:continue
            typ,t,first=unique[-1]
            if len(unique)>1:
                previous=unique[-2][0]
                message=f'**Liquidity journey:** {previous} → {typ} across the latest distinct confirmed sweeps.'
            else:
                message=f'**Liquidity journey:** Last distinct observed sweep was {typ}.'
            baseline=_num(first.get('price'))
            current=_num(edge.get('close'))
            if baseline and current is not None:
                move=100*(current/baseline-1)
                message+=f' Price moved {move:+.2f}% since the first saved observation after that event.'
            paragraphs.append(message+' This is observed follow-through, not proof of the next move.')
    return '\n\n'.join(paragraphs[:6])

# -----------------------------------------------------------------------------
# SuperBrain market-outlook adjudicator. DISPLAY ONLY — not an entry or risk gate.
# -----------------------------------------------------------------------------
def _outlook_side(text: Any) -> int:
    s=str(text or '').upper()
    if 'BEARISH' in s or s in ('RED','PINK'):return -1
    if 'BULLISH' in s or s in ('GREEN','YELLOW'):return 1
    return 0


def _outlook_fmt(price: float) -> str:
    return f'{price:,.2f}'.rstrip('0').rstrip('.')


def _outlook_journey(edge: Dict[str,Any], tape: List[Dict[str,Any]]) -> Dict[str,Any]:
    """Use genuine completed observations of the current execution TF only.

    No interpolation across market closure, TF handoffs or missing sessions.
    Previously recorded rows are not assumed to be actual high/low wicks.
    """
    now=_ms(edge.get('bar_time'))
    tf=_num(edge.get('tf'))
    spot=_num(edge.get('close'))
    if not now or not tf or not spot or tf<=0:return {}
    series=[]
    for r in tape or []:
        if str(r.get('instrument') or edge.get('instrument') or '').upper()!=str(edge.get('instrument') or '').upper():continue
        if str(r.get('tf') or '') != str(edge.get('tf') or ''):continue
        if r.get('synthetic_price_only'):continue
        tm=_ms(r.get('bar_ms'));px=_num(r.get('close'))
        if tm and px and tm<=now:series.append((tm,px,_outlook_side(r.get('exec_of'))))
    series.append((now,spot,_outlook_side(edge.get('exec_of'))))
    ordered=sorted({r[0]:r for r in series}.values())
    if len(ordered)<2:return {'spot':spot,'now':now,'tf':tf,'real_count':len(ordered)}
    allowed_gap=max(3*tf*60_000,15*60_000)
    contiguous=[ordered[-1]]
    for x in reversed(ordered[:-1]):
        if contiguous[0][0]-x[0]>allowed_gap:break
        contiguous.insert(0,x)
    # For the decision, use a 4-hour window, bounded to the real contiguous tape.
    target=now-4*3_600_000
    recent=[r for r in contiguous if r[0]>=target]
    earlier=[r for r in contiguous if r[0]<target]
    if earlier and target-earlier[-1][0]<=allowed_gap:recent.insert(0,earlier[-1])
    if len(recent)<4:recent=contiguous[-max(4,min(16,int(240/max(tf,1)))):]
    duration_hours=(recent[-1][0]-recent[0][0])/3_600_000 if len(recent)>1 else 0
    drift=(spot/recent[0][1]-1)*100 if len(recent)>=4 and recent[0][1] else None
    width=(max(r[1] for r in recent)-min(r[1] for r in recent))/recent[0][1]*100 if len(recent)>=4 else None
    current_side=_outlook_side(edge.get('exec_of'))
    match=sum(r[2]==current_side for r in recent)/len(recent) if len(recent)>=4 and current_side else 0
    # Price boundaries are explicitly CLOSE-based rather than fabricated OHLC pivots.
    lookback=min(len(contiguous)-1,max(12,min(24,int(240/max(tf,1)))))
    look=contiguous[-(lookback+1):-1] if lookback>=4 else []
    up=max((r[1] for r in look),default=None)
    down=min((r[1] for r in look),default=None)
    return {'spot':spot,'now':now,'tf':tf,'real_count':len(contiguous),
            'duration_hours':duration_hours,'drift':drift,'width':width,
            'match':match,'dir':current_side,'recent_high':up,'recent_low':down,
            'recent_count':len(recent),'all_closes':ordered[-600:]}


def _outlook_optional(optional: List[Dict[str,Any]], edge_ms: int) -> Dict[str,Any]:
    evidence={'aurora':None,'aurora_transition':False,'liq':None,'liq_price_response':None}
    for src in ('AURORA','LIQUIDITY'):
        own=[]
        for r in optional or []:
            if str(r.get('source') or '').upper()!=src:continue
            ms=_ms(r.get('confirmed_close_ms'))
            if ms and ms<=edge_ms:own.append((ms,r))
        own.sort(key=lambda v:v[0])
        if not own:continue
        ms,latest=own[-1]
        tf=_num(latest.get('tf')) or 60
        if edge_ms-ms>max(2*tf*60_000,90*60_000):continue
        if src=='AURORA':
            evidence['aurora']=str(latest.get('state') or '').upper()
            if len(own)>=2:
                previous=str(own[-2][1].get('state') or '').upper()
                evidence['aurora_transition']=bool(previous and previous!=evidence['aurora'])
        else:
            last_event=str(latest.get('last_event') or '').upper()
            evt=_ms(latest.get('event_ms'))
            if evt and 0<=edge_ms-evt<=24*3_600_000:
                evidence['liq']=last_event
                ctx=latest.get('context') or _json_obj(latest.get('context_json'))
                current_event=next((v for v in (ctx.get('recent_events') or []) if
                                    str(v.get('type') or '').upper()==last_event and
                                    _ms(v.get('at_ms'))==evt),None)
                evidence['liq_level']=_num((current_event or {}).get('level'))
                relevant=[r for t,r in own if _ms(r.get('event_ms'))==evt and str(r.get('last_event') or '').upper()==last_event]
                first=relevant[0] if relevant else latest
                start=_num(first.get('price'))
                end=_num(latest.get('price'))
                if start and end and len(relevant)>=2:evidence['liq_price_response']=(end/start-1)*100
    return evidence


def _outlook_odme(odme: Optional[Dict[str,Any]], snapshots: List[Dict[str,Any]], edge_ms: int) -> Dict[str,Any]:
    live=odme if isinstance(odme,dict) else {}
    direction=_outlook_side(live.get('direction'))
    strength=str(live.get('strength') or '').upper()
    latest=None;old=None
    options=[r for r in (snapshots or []) if _ms(r.get('ts')) and _ms(r.get('ts'))<=edge_ms+60_000]
    if options:
        latest=max(options,key=lambda z:_ms(z.get('ts')))
        same=[x for x in options if str(x.get('expiry') or '').upper()==str(latest.get('expiry') or '').upper() and
              str(x.get('snapshot_id') or '')!=str(latest.get('snapshot_id') or '')]
        if same:old=max(same,key=lambda z:_ms(z.get('ts')))
    tilt=str((latest or {}).get('odme_tilt') or '')
    tilt_change=(str(old.get('odme_tilt') or '')+' → '+tilt) if old and tilt and str(old.get('odme_tilt') or '')!=tilt else None
    return {'dir':direction,'strength':strength,'tilt':tilt,'tilt_change':tilt_change,
            'latest_age_hours':(edge_ms-_ms(latest.get('ts')))/3_600_000 if latest else None,
            'old':old,'latest':latest}


def _outlook_levels(edge: Dict[str,Any], journey: Dict[str,Any], odme: Dict[str,Any]) -> Dict[str,Any]:
    """Use only observed prices, TV FP/structural levels and available ODME walls.

    A price-level observation is a conditional reference, not a forecasted fill.
    """
    spot=journey.get('spot')
    if spot is None:return {}
    candidates=[]
    def add(value: Any,kind: str,weight: int):
        p=_num(value)
        if p is None or p<=0 or abs(p-spot)/spot<0.0001:return
        if abs(p-spot)/spot>0.17:return  # remote strategic geometry is not a near-term trigger
        if any(abs(z[0]-p)/spot<0.0002 for z in candidates):return
        candidates.append((p,kind,weight))
    add(journey.get('recent_low'),'observed recent closing low',5)
    add(journey.get('recent_high'),'observed recent closing high',5)
    for k in ('fp_low','fp_high','defender_low','defender_high','challenger_low','challenger_high'):
        add(edge.get(k),k,3)
    raw=_json_obj(edge.get('raw_json'))
    book=raw.get('fp_hurdle_book') or {}
    if isinstance(book,dict):
        for h in (book.get('hurdles') or [])[:12]:
            if isinstance(h,dict) and str(h.get('status') or '').upper()!='RUN':
                add(h.get('low'),'EDGE FP hurdle',2);add(h.get('high'),'EDGE FP hurdle',2)
    # Current ODME walls are barriers, never fresh EDGE entry authority.
    latest=odme.get('latest') or {}
    if odme.get('latest_age_hours') is not None and odme['latest_age_hours']<=24:
        add(latest.get('pe_wall'),'ODME PE wall',1)
        add(latest.get('ce_wall'),'ODME CE wall',1)
    lowers=sorted((x for x in candidates if x[0]<spot),key=lambda z:(spot-z[0],-z[2]))
    uppers=sorted((x for x in candidates if x[0]>spot),key=lambda z:(z[0]-spot,-z[2]))
    # Prefer a recent observed boundary if it is reasonably near, otherwise nearest FP/structural.
    return {'lower':lowers[0] if lowers else None,'upper':uppers[0] if uppers else None,
            'below_next':lowers[1] if len(lowers)>1 else None,
            'above_next':uppers[1] if len(uppers)>1 else None}


def _outlook_historical_check(journey: Dict[str,Any], side: int) -> Dict[str,Any]:
    """Conservative, walk-forward-like price-only evidence.

    Return no pattern vote unless repeated comparable, non-overlapping examples
    and an advantage over the naive same-direction baseline are present.
    """
    rows=journey.get('all_closes') or []
    if len(rows)<70 or not side:return {}
    tf=journey.get('tf') or 60
    bars=max(4,min(20,int(240/tf)))
    patterns=[];previous=-1
    for i in range(bars,len(rows)-bars):
        if i<previous+bars:continue
        before=rows[i-bars];mid=rows[i];after=rows[i+bars]
        if (mid[0]-before[0]>bars*tf*60_000*1.5 or
                after[0]-mid[0]>bars*tf*60_000*1.5):continue
        # Same observed OF direction, and price-progress direction as now.
        if mid[2]!=side or before[1]<=0 or mid[1]<=0:continue
        drift=(mid[1]/before[1]-1)*100
        current=journey.get('drift')
        if current is None:continue
        if (drift>0)-(drift<0)!=(current>0)-(current<0):continue
        fwd=(after[1]/mid[1]-1)*100
        patterns.append((fwd,mid[2]))
        previous=i
    # Too few fully independent examples for a statistical forecast.
    if len(patterns)<25:return {'similar_cases':len(patterns),'calibrated':False}
    success=sum((f>0 if side>0 else f<0) for f,_ in patterns)/len(patterns)
    return {'similar_cases':len(patterns),'directional_accuracy':success,'calibrated':False}


def formulate_market_outlook(edge: Dict[str,Any], edge_tape: List[Dict[str,Any]],
                             optional_history: List[Dict[str,Any]], odme_history: List[Dict[str,Any]],
                             current_odme: Optional[Dict[str,Any]]=None) -> Dict[str,Any]:
    """Predictive *scenario* display; never modifies executable trade decisions.

    Combines price-effectiveness, persistent EDGE OF, FP freshness, macro/MTF
    disagreements, source-fresh AURORA and liquidity, and live/historical ODME.
    Qualitative conviction is evidence agreement, NOT a calibrated probability.
    """
    j=_outlook_journey(edge,edge_tape)
    if not j.get('spot') or not j.get('now'):
        return {'label':'UNAVAILABLE','commentary':'Insufficient verified EDGE price data for a market outlook.'}
    o=_outlook_optional(optional_history,j['now'])
    d=_outlook_odme(current_odme,odme_history,j['now'])
    levels=_outlook_levels(edge,j,d)
    flow=j.get('dir') or _outlook_side(edge.get('exec_of'))
    macro=_outlook_side(edge.get('macro'))
    drift=j.get('drift');width=j.get('width')
    agreement=j.get('match') or 0
    has_path=drift is not None and width is not None and j.get('recent_count',0)>=4
    # Price action outranks static pressure labels. Magnitude adapts to actual
    # observed range while imposing a small minimum signal-to-noise floor.
    threshold=max(0.18,0.30*width) if has_path else None
    price_side=(1 if drift>=threshold else -1 if drift<=-threshold else 0) if has_path else 0
    effective=bool(flow and price_side==flow and agreement>=.60)
    counter=bool(flow and price_side==-flow and agreement>=.60)
    flat=not has_path or price_side==0
    # Extra source context can temper and corroborate, never produce an EDGE entry.
    mtf=[_outlook_side(edge.get(k)) for k in ('of_15','of_60','of_125','of_240')]
    mtf=[x for x in mtf if x]
    share=sum(v==price_side for v in mtf)/len(mtf) if mtf and price_side else 0
    aura_side=_outlook_side(o.get('aurora'))
    event=str(edge.get('fp_event_type') or '').upper()
    event_side=str(edge.get('fp_side') or '').upper()
    age=_num(edge.get('fp_bars_since_event'))
    fresh_event=age is not None and 0<=age<=3
    fp_favours=(1 if event=='RUN_CONFIRMED' and event_side=='SUPPLY' else
                -1 if event=='RUN_CONFIRMED' and event_side=='DEMAND' else
                1 if event=='DEFENCE_CONFIRMED' and event_side=='DEMAND' else
                -1 if event=='DEFENCE_CONFIRMED' and event_side=='SUPPLY' else 0)
    if not fresh_event:fp_favours=0
    if counter:
        side=price_side
        kind='REVERSAL DEVELOPING'
    elif effective:
        side=flow
        kind='CONTINUATION'
    elif has_path and price_side and agreement<.60:
        side=price_side
        kind='CONTINUATION'
    else:
        side=0
        kind='RANGE / CONSOLIDATION'
    # Fresh EDGE event can support the emerging directional thesis but must not
    # force price reversal when price/pressure are both unconvincing.
    supporting=[];conflicts=[]
    if side:
        if macro==side:supporting.append('macro trend')
        elif macro==-side:conflicts.append('macro trend')
        if mtf and share>=.75:supporting.append('nearby timeframes')
        if mtf and share<=.25:conflicts.append('nearby timeframes')
        if aura_side==side:supporting.append('Candle AURORA')
        elif aura_side==-side:conflicts.append('Candle AURORA')
        if d['dir']==side:supporting.append('current ODME')
        elif d['dir']==-side:conflicts.append('current ODME')
        if fp_favours==side:supporting.append('fresh EDGE FP event')
        elif fp_favours==-side:conflicts.append('fresh EDGE FP event')
    if o.get('aurora')=='WHITE':conflicts.append('range-state Candle AURORA')
    # Confirmed liquidity RUN is useful only when price has accepted beyond the
    # reported event level; a SWEEP cannot be assumed directional from its name.
    liq_event=o.get('liq') or ''
    liq_level=o.get('liq_level')
    if liq_event and liq_level and side:
        response=0
        if 'SELL' in liq_event and 'RUN' in liq_event and j['spot']<liq_level:response=-1
        elif 'BUY' in liq_event and 'RUN' in liq_event and j['spot']>liq_level:response=1
        elif 'SELL' in liq_event and 'SWEEP' in liq_event and j['spot']>liq_level:response=1
        elif 'BUY' in liq_event and 'SWEEP' in liq_event and j['spot']<liq_level:response=-1
        if response==side:supporting.append('accepted liquidity response')
        elif response==-side:conflicts.append('liquidity rejection')
    # Repeated negative delta is not necessarily a forecast until price response
    # confirms it. Liquidity sweep labels are informational, not binary votes.
    quality='LOW'
    if has_path and j.get('duration_hours',0)>=1:
        quality='MODERATE'
        if side and len(supporting)>=3 and not conflicts and j['real_count']>=12:
            quality='HIGH'
        if side and len(conflicts)>=2:quality='LOW'
    if side==0 and has_path and width and width<0.30:quality='LOW'
    side_name='BULLISH' if side>0 else 'BEARISH' if side<0 else 'RANGE'
    label=f'{side_name} {kind}' if side else kind
    # Main expected path, followed by only a meaningful conditional alternative.
    if side<0:
        first=(f'Sellers are making downside progress ({drift:+.2f}% in {j["duration_hours"]:.1f} observed hours).'
               if effective else 'Price is recovering less effectively than sellers are pressing; downside risk dominates.'
               if not counter else 'Price is retreating despite earlier opposing pressure; downside control is developing.')
    elif side>0:
        first=(f'Buyers are making upward progress ({drift:+.2f}% in {j["duration_hours"]:.1f} observed hours).'
               if effective else 'Price is advancing despite opposing pressure, suggesting sellers are losing effectiveness.'
               if counter else 'Price is advancing, with higher price acceptance developing.')
    else:
        first=('Price is not making sustained progress despite persistent EDGE pressure; consolidation is more likely.'
               if has_path and flow and agreement>=.60 else
               'Price and directional evidence are mixed; a range is more likely until a confirmed escape.')
    if side and macro==-side:first+=' The broader macro trend still opposes the move.'
    elif side and macro==side and quality=='HIGH':first+=' Higher-timeframe structure agrees.'
    if d.get('tilt_change') and quality!='HIGH':first+=' ODME positioning has recently changed.'
    if (d['dir']==-side and side and quality!='HIGH'):
        first+=' Current ODME positioning does not support the directional outlook.'
    main=''
    alternate=''
    down=levels.get('lower');up=levels.get('upper')
    if side<0:
        if down:
            main=f'**Most likely:** Weakness remains favoured while below {_outlook_fmt(up[0]) if up else _outlook_fmt(j["spot"])}. A sustained break below **{_outlook_fmt(down[0])}** would strengthen downside continuation.'
            if levels.get('below_next'):main+=f' Next reference: {_outlook_fmt(levels["below_next"][0])}.'
        else:main='**Most likely:** Continued downside pressure, but no reliable nearby break level is available from the recorded data.'
        if up and (macro>0 or counter or quality!='HIGH'):
            alternate=f'**Rebound risk:** A sustained reclaim above **{_outlook_fmt(up[0])}** would weaken the bearish thesis.'
    elif side>0:
        if up:
            main=f'**Most likely:** Further upside is favoured while above {_outlook_fmt(down[0]) if down else _outlook_fmt(j["spot"])}. A sustained reclaim above **{_outlook_fmt(up[0])}** would strengthen the rebound.'
            if levels.get('above_next'):main+=f' Next reference: {_outlook_fmt(levels["above_next"][0])}.'
        else:main='**Most likely:** A rebound is developing, but no reliable nearby upside breakout level is available.'
        if down and (macro<0 or counter or quality!='HIGH'):
            alternate=f'**Downside risk:** Losing **{_outlook_fmt(down[0])}** would challenge the rebound.'
    else:
        if down and up:
            main=f'**Most likely:** Choppy trading between **{_outlook_fmt(down[0])}–{_outlook_fmt(up[0])}** until price establishes acceptance outside the range.'
            if flow<0:alternate=f'**Bearish trigger:** A sustained move below **{_outlook_fmt(down[0])}** would favour downside continuation.'
            elif flow>0:alternate=f'**Bullish trigger:** A sustained move above **{_outlook_fmt(up[0])}** would favour upside continuation.'
        else:
            main='**Most likely:** Sideways or unreliable movement. Wait for clearer EDGE price progress and a confirmed structural boundary.'
    if side<0:renko='**Renko AURORA:** Prefer bearish confirmation; a bullish Renko reversal would challenge the downside thesis.'
    elif side>0:renko='**Renko AURORA:** Prefer bullish confirmation; a bearish Renko reversal would challenge the rebound thesis.'
    else:renko='**Renko AURORA:** Wait for a clean directional break rather than relying on a neutral or changing Renko state.'
    lines=[first,main]
    if alternate:lines.append(alternate)
    lines.append(renko)
    # This result is NEVER consumed by order, setup, campaign or strike logic.
    return {'label':label, 'commentary':'\n\n'.join(lines),
            'evidence_quality':quality,'side':side,'scenario':kind,
            'lower_level':down[0] if down else None,'upper_level':up[0] if up else None,
            'sample_size':j.get('real_count',0),
            'history_pattern':_outlook_historical_check(j,side),
            'trade_authority':'EDGE_ENTRY_AND_FRESH_ODME_ONLY'}
