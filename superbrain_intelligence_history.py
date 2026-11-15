"""Read-only SuperBrain intelligence from the independently recorded optional tape.

The Apps Script Level-2 scheduler continuously records real optional source
observations; app scans only READ that tape, EDGE history and ODME snapshots.
No setup, strike, management, signal, broker or Telegram code imports this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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



# Persistent explanatory location, distinct from active-TF execution authority.
# A recorded higher-TF observation establishes the origin; a memory record only
# carries the origin forward and is rechecked against today's authoritative EDGE.
def _higher_tf_minutes(tf: Any) -> Optional[int]:
    """Recognize the Pine timeframes in recorded EDGE role metadata.

    A historical daily/weekly origin can coexist with a 3m execution handoff.
    Numeric values remain minutes; no inferred timeframes from macro labels.
    """
    s=str(tf or '').strip().upper()
    if s in ('D','1D'):return 1440
    if s in ('W','1W'):return 10080
    if s in ('M','1MO'):return 43200
    if s.endswith('H'):
        v=_num(s[:-1]);return int(v*60) if v and v>0 else None
    if s.endswith('M') and s[:-1].isdigit():return int(s[:-1])
    v=_num(s)
    return int(v) if v and v>0 else None


def _archived_higher_tf_reference(edge: Dict[str,Any],
                                  historical_edge_rows: List[Dict[str,Any]],
                                  now: int, spot: float, execution_tf: float) -> Dict[str,Any]:
    """Recover a historical HTF reference when the capped EDGE tape lost its touch.

    Unlike a VERIFIED_ORIGIN, an archived EDGE zone without an observed touch
    is explicitly reference-only. Never promote it to a live FP defence or
    to a directional evidence vote. The original row must actually have
    contained the zone/TF at or before the current confirmed observation.
    """
    inst=str(edge.get('instrument') or '').upper()
    choices=[]
    for row in historical_edge_rows or []:
        if not isinstance(row,dict) or str(row.get('source') or '').upper()!='EDGE':continue
        if str(row.get('instrument') or '').upper()!=inst:continue
        tm=_ms(row.get('bar_time'));old_price=_num(row.get('close'))
        if not tm or not old_price or not 0<=now-tm<=30*86400_000:continue
        for role in ('defender','challenger'):
            pref=role+'_'
            tf=_higher_tf_minutes(row.get(pref+'tf'))
            side=str(row.get(pref+'side') or '').upper()
            lo=_num(row.get(pref+'low'));hi=_num(row.get(pref+'high'))
            state=str(row.get(pref+'state') or '').upper()
            if not tf or tf<60 or tf<execution_tf or side not in ('DEMAND','SUPPLY'):
                continue
            if lo is None or hi is None or lo<=0 or lo>hi or state in ('RUN','BROKEN','INACTIVE','NONE'):
                continue
            gap=max(lo-old_price,0,old_price-hi)
            if gap/old_price>0.02:continue  # zone must be near its archived price, not a remote map level
            # Only an actually observed favorable displacement from the earlier
            # snapshot can qualify it as a candidate move-away reference.
            favorable=(side=='DEMAND' and spot>=old_price and spot>hi) or (
                side=='SUPPLY' and spot<=old_price and spot<lo)
            if not favorable:continue
            choices.append((tf,tm,role,side,lo,hi,old_price))
    if not choices:return {}
    tf,tm,role,side,lo,hi,old_price=max(choices,key=lambda x:(x[0],x[1]))
    return {'instrument':inst,'origin_time':tm,'checked_at':now,
            'origin_price':old_price,'side':side,'tf':tf,'low':lo,'high':hi,
            'origin_verified':False,'status':'REFERENCE_ONLY',
            'phase':('REFERENCE_ABOVE' if spot>hi else 'REFERENCE_BELOW'),
            'source':'ARCHIVED_EDGE_ROLE','execution_tf':int(execution_tf) if execution_tf else None,
            'progress_pct':round(100*(spot/old_price-1),3)}


def evolve_strategic_context(edge: Dict[str,Any], tape: List[Dict[str,Any]],
                             previous: Optional[Dict[str,Any]]=None,
                             historical_edge_rows: Optional[List[Dict[str,Any]]]=None) -> Dict[str,Any]:
    now=_ms(edge.get('bar_time'));spot=_num(edge.get('close'))
    if not now or spot is None or spot<=0:return {}
    inst=str(edge.get('instrument') or '').upper().strip()
    execution_tf=_num(edge.get('tf')) or 0
    prior=previous if isinstance(previous,dict) else {}
    roles=[]
    for role in ('defender','challenger'):
        side=str(edge.get(role+'_side') or '').upper()
        tf=_higher_tf_minutes(edge.get(role+'_tf'))
        low=_num(edge.get(role+'_low'));high=_num(edge.get(role+'_high'))
        state=str(edge.get(role+'_state') or '').upper()
        if (side in ('DEMAND','SUPPLY') and tf and tf>=60 and tf>=execution_tf
                and low is not None and high is not None and 0<low<=high
                and state not in ('RUN','BROKEN','INACTIVE')):
            roles.append(dict(role=role,side=side,tf=int(tf),low=low,high=high))
    candidates=[]
    # The current EDGE row contains the authoritative higher-TF zone identity.
    # The tape contains actual earlier closes, including previous execution TFs.
    rows=[r for r in (tape or []) if isinstance(r,dict) and not r.get('synthetic_price_only')]
    current_sample={'instrument':inst,'bar_ms':now,'close':spot}
    for role in ('defender','challenger'):
        for k in ('side','low','high'):
            current_sample[role+'_'+k]=edge.get(role+'_'+k)
    rows.append(current_sample)
    for role in roles:
        for row in rows:
            if str(row.get('instrument') or inst).upper()!=inst:continue
            tm=_ms(row.get('bar_ms'));px=_num(row.get('close'))
            if not tm or px is None or tm>now or now-tm>14*86400_000:continue
            # A zone can be recorded without a touch. Require real observed
            # proximity before remembering it as the move's origin.
            gap=max(role['low']-px,0,px-role['high'])
            if gap/max(px,1)>0.004:continue
            # Historical price cannot be attributed to a zone that only
            # appeared later. It must already be identifiable in THAT sample.
            prefix=role['role']+'_'
            if str(row.get(prefix+'side') or '').upper()!=role['side']:continue
            old_lo=_num(row.get(prefix+'low'));old_hi=_num(row.get(prefix+'high'))
            if old_lo is None or old_hi is None:continue
            tolerance=max(0.0004*spot,0.10*(role['high']-role['low']))
            if min(abs(old_lo-role['low']),abs(old_hi-role['high']))>tolerance:continue
            if min(old_hi,role['high'])<max(old_lo,role['low']):continue
            candidates.append((tm,role,px))
    valid_prior=False
    if prior and prior.get('instrument')==inst:
        tm=_ms(prior.get('origin_time'))
        lo=_num(prior.get('low'));hi=_num(prior.get('high'))
        valid_prior=bool(tm and 0<=now-tm<=21*86400_000 and lo and hi and lo<=hi)
    def stage(origin: Dict[str,Any], recognized: bool) -> Dict[str,Any]:
        record=dict(origin)
        record['checked_at']=now
        if record.get('origin_verified') is False:
            # A reference never becomes a verified touch simply by persistence.
            # Keep its phase honest when price later revisits the region.
            lo=_num(record.get('low'));hi=_num(record.get('high'))
            role=record.get('side')
            record['status']='REFERENCE_ONLY'
            # A snapshot above demand or below supply establishes only LOCATION.
            # It does not establish direction of travel: BTC can be falling BACK
            # toward that demand. No historical touch is inferred here.
            record['phase']=('REFERENCE_ABOVE' if lo is not None and hi is not None and spot>hi
                             else 'REFERENCE_BELOW' if lo is not None and hi is not None and spot<lo
                             else 'REFERENCE_UNDER_TEST')
            record['execution_tf']=int(execution_tf) if execution_tf else None
            return record
        role=record.get('side')
        low=_num(record.get('low'));high=_num(record.get('high'))
        baseline=_num(record.get('origin_price'))
        record['execution_tf']=int(execution_tf) if execution_tf else None
        if baseline and baseline>0:
            record['progress_pct']=round(100*(spot/baseline-1),3)
        if low is None or high is None:return record
        # Crossing a distal boundary CHALLENGES the original thesis; only
        # EDGE's separately confirmed break establishes a failed zone.
        if (role=='DEMAND' and spot<low) or (role=='SUPPLY' and spot>high):
            record['status']='CHALLENGED'
            record['phase']='ORIGIN_UNDER_PRESSURE'
        elif (role=='DEMAND' and spot>high) or (role=='SUPPLY' and spot<low):
            record['status']='RECOGNIZED' if recognized else 'MOVE_AWAY'
            record['phase']='MOVE_AWAY'
        else:
            record['status']='RECOGNIZED' if recognized else 'HISTORICAL_ONLY'
            record['phase']='ORIGIN_TEST' if recognized else 'UNVERIFIED'
        return record

    # Remember the originally VERIFIED high-TF interaction across 3m/15m
    # handoffs; the zone may cease to appear in current EDGE while the market
    # is still travelling from it. Do not equate move-away with fresh entry.
    if valid_prior:
        # An independently observed NEW higher-TF reaction may replace the old
        # origin. A single touch of a later zone is insufficient: actual time
        # and price progress away from the newer zone must also be present.
        new=[]
        old_time=_ms(prior.get('origin_time')) or 0
        for tm,role,px in candidates:
            different=(role['side']!=prior.get('side') or
                       abs(role['low']-float(prior['low']))/spot>0.0002 or
                       abs(role['high']-float(prior['high']))/spot>0.0002)
            enough_time=tm>old_time and now-tm>=max(1,execution_tf)*2*60000
            moved=(role['side']=='DEMAND' and spot>role['high'] or
                   role['side']=='SUPPLY' and spot<role['low'])
            if different and enough_time and moved:new.append((tm,role,px))
        if new:
            tm,role,px=max(new,key=lambda x:(x[0],x[1]['tf']))
            return stage({'instrument':inst,'origin_time':tm,'origin_price':px,
                          'side':role['side'],'tf':role['tf'],
                          'low':role['low'],'high':role['high'],
                          'origin_verified':True},recognized=True)
        prev_role=next((r for r in roles if r['side']==prior.get('side') and
                        abs(r['low']-float(prior['low']))/spot<0.0002 and
                        abs(r['high']-float(prior['high']))/spot<0.0002),None)
        return stage(prior, recognized=bool(prev_role))
    # Use the most recent VERIFIED interaction, preferring higher TF if same time.
    if candidates:
        tm,role,px=max(candidates,key=lambda x:(x[0],x[1]['tf']))
        return stage({'instrument':inst,'origin_time':tm,'origin_price':px,
                      'side':role['side'],'tf':role['tf'],
                      'low':role['low'],'high':role['high'],
                      'origin_verified':True},recognized=True)
    return _archived_higher_tf_reference(edge,historical_edge_rows or [],now,spot,execution_tf)


def _strategic_tf_label(tf: float) -> str:
    if int(tf)==1440:return 'daily'
    if int(tf)==10080:return 'weekly'
    if int(tf)==43200:return 'monthly'
    if tf>=240 and int(tf)%60==0:return f'{int(tf//60)}h'
    return f'{int(tf)}m'


def _strategic_opening(context: Dict[str,Any], side: int, spot: float) -> str:
    if not isinstance(context,dict) or not context:return ''
    low=_num(context.get('low'));high=_num(context.get('high'))
    tf=_num(context.get('tf'))
    if low is None or high is None or not tf or not spot:return ''
    role=str(context.get('side') or '').upper()
    status=str(context.get('status') or '').upper()
    phase=str(context.get('phase') or '').upper()
    if role not in ('DEMAND','SUPPLY'):return ''
    where=f'{_strategic_tf_label(tf)} {role.lower()} around {_outlook_fmt(low)}–{_outlook_fmt(high)}'
    # When the saved tape has rolled off, an archived EDGE role supplies a
    # structural reference only; never upgrade it to an observed origin touch.
    if status=='REFERENCE_ONLY':
        if phase=='REFERENCE_UNDER_TEST':
            return (f'Earlier EDGE history recorded higher-TF {where}; price is now '
                    'revisiting that region. Its current reaction needs fresh EDGE evidence.')
        return (f'Earlier EDGE history recorded higher-TF {where}. Price is '
                f'currently {_outlook_fmt(spot)}; this is a structural reference, '
                'not a verified original reaction and not a fresh defence entry.')
    # Remember the strategic origin even if its specific event has completed
    # and no longer appears in the latest execution-TF zone list.
    if status=='MOVE_AWAY':
        return (f'Price has progressed away from earlier verified higher-TF {where}. '
                'That original reaction explains the larger journey but is not a fresh entry trigger.')
    if status=='HISTORICAL_ONLY':
        return f'An earlier reaction near higher-TF {where} is historical context; its present protection is unverified.'
    if status=='CHALLENGED':
        return f'The prior higher-TF {where} is now under pressure; its earlier reaction is not evidence of a continuing hold.'
    if phase=='MOVE_AWAY':
        if role=='DEMAND' and side>0:
            return (f'The recovery has progressed away from higher-TF {where}; '
                    'current lower-TF signals track continuation, not another entry from that original defence.')
        if role=='SUPPLY' and side<0:
            return (f'The decline has progressed away from higher-TF {where}; '
                    'current lower-TF signals track continuation, not another entry from that original defence.')
        return (f'Price has moved away from the earlier higher-TF {where} reaction; '
                'current EDGE control determines whether that larger move continues or pulls back. '
                'That reaction is not another entry from the original defence.')
    return f'Price is interacting with higher-TF {where}; current execution control must confirm its next leg.'


_IST=ZoneInfo('Asia/Kolkata')


def _odme_session(row: Dict[str,Any]) -> Optional[str]:
    tm=_ms(row.get('ts'))
    return datetime.fromtimestamp(tm/1000,tz=timezone.utc).astimezone(_IST).date().isoformat() if tm else None


def _odme_trusted_snapshot(row: Dict[str,Any]) -> bool:
    # Explicit poor/empty listed-OI coverage cannot establish wall migration.
    count=_num(row.get('usable_oi_count'))
    return count is None or count>=10


def _odme_evolution(odme: Optional[Dict[str,Any]], snapshots: List[Dict[str,Any]], edge_ms: int,
                    side: int=0) -> Dict[str,Any]:
    """Display-only previous-session and multi-session, SAME-EXPIRY narrative.

    Do not interpret the last intraday scan as 'yesterday'. Never combine expiry
    series or use a future saved snapshot as proof of an earlier market state.
    """
    live=odme if isinstance(odme,dict) else {}
    rows=sorted((dict(r) for r in (snapshots or []) if isinstance(r,dict) and
                 _ms(r.get('ts')) and _ms(r.get('ts'))<=edge_ms and
                 str(r.get('expiry') or '').strip() and _odme_trusted_snapshot(r)),
                key=lambda r:_ms(r['ts']))
    if not rows:return {'text':'','fresh_adverse':False,'trend_side':0,'session_count':0}
    expiry=str(live.get('expiry') or '').strip().upper()
    if not expiry or not any(str(r.get('expiry') or '').strip().upper()==expiry for r in rows):
        expiry=str(rows[-1].get('expiry') or '').strip().upper()
    rows=[r for r in rows if str(r.get('expiry') or '').strip().upper()==expiry]
    by_date={}
    for r in rows:by_date[_odme_session(r)]=r  # last confirmed snapshot per session
    dates=sorted(d for d in by_date if d)
    if not dates:return {'text':'','fresh_adverse':False,'trend_side':0,'session_count':0}
    recent=dates[-7:]
    samples=[by_date[d] for d in recent]
    latest=samples[-1];last_ms=_ms(latest['ts'])
    age=(edge_ms-last_ms)/3600_000
    if age>96:
        return {'text':'Saved ODME history is too old to describe current positioning.',
                'fresh_adverse':False,'trend_side':0,'session_count':len(samples),'stale':True}
    parts=[];multi_side=0;fresh_adverse=False
    if len(samples)>=3:
        direction={}
        for col in ('option_poc','pe_wall','ce_wall'):
            vals=[_num(r.get(col)) for r in samples]
            if any(v is None or v<=0 for v in vals):continue
            moves=[(b>a)-(b<a) for a,b in zip(vals,vals[1:])]
            up=moves.count(1);dn=moves.count(-1)
            if up>=2 and dn==0:direction[col]=1
            elif dn>=2 and up==0:direction[col]=-1
        if direction:
            if direction.get('option_poc')==1 and direction.get('pe_wall')==1:
                parts.append('multi-session POC and PE-wall migration is higher')
                multi_side=1
            elif direction.get('option_poc')==-1 and direction.get('ce_wall')==-1:
                parts.append('multi-session POC and CE-wall migration is lower')
                multi_side=-1
            elif direction.get('option_poc')==1:
                parts.append('multi-session POC is migrating higher')
                multi_side=1
            elif direction.get('option_poc')==-1:
                parts.append('multi-session POC is migrating lower')
                multi_side=-1
            elif direction.get('pe_wall')==1:
                parts.append('multi-session PE wall has migrated higher')
            elif direction.get('ce_wall')==-1:
                parts.append('multi-session CE wall has migrated lower')
    if len(samples)>=2:
        prev=samples[-2]
        day_gap=(datetime.fromisoformat(recent[-1]).date()-datetime.fromisoformat(recent[-2]).date()).days
        # Avoid labeling a week-old sparse comparison as a fresh expansion.
        if day_gap<=4:
            changes=[]
            for col,name in [('pe_wall','PE wall'),('ce_wall','CE wall'),('option_poc','POC')]:
                x=_num(prev.get(col));y=_num(latest.get(col))
                if x is not None and y is not None and x>0 and y>0 and x!=y:
                    changes.append(f'{name} moved {"higher" if y>x else "lower"}')
            exp0=_num(prev.get('expansion_score'));exp1=_num(latest.get('expansion_score'))
            bull0=_num(prev.get('bullish_score'));bull1=_num(latest.get('bullish_score'))
            bear0=_num(prev.get('bearish_score'));bear1=_num(latest.get('bearish_score'))
            if exp0 is not None and exp1 is not None and exp1>exp0:
                if bear1 is not None and bull1 is not None and bear1>bull1 and (bear0 is None or bear1>bear0):
                    changes.insert(0,'bearish expansion pressure increased')
                    fresh_adverse=side>0
                elif bull1 is not None and bear1 is not None and bull1>bear1 and (bull0 is None or bull1>bull0):
                    changes.insert(0,'bullish expansion pressure increased')
                    fresh_adverse=side<0
                else:
                    changes.insert(0,'expansion score increased without clear directional dominance')
            elif exp0 is not None and exp1 is not None and exp1<exp0:
                changes.insert(0,'expansion pressure eased')
            tilt0=str(prev.get('odme_tilt') or '').strip()
            tilt1=str(latest.get('odme_tilt') or '').strip()
            if tilt0 and tilt1 and tilt0!=tilt1 and not changes:
                changes.append(f'ODME tilt changed from {tilt0} to {tilt1}')
            if changes:parts.append('versus the previous recorded session, '+', '.join(changes[:2]))
    if not parts and len(samples)==1:
        parts.append('only one comparable saved ODME session is available')
    elif not parts and len(samples)>1:
        parts.append('saved ODME positioning has no clear sustained migration')
    text='; '.join(parts)
    # Explicitly identify historical-only context to avoid treating old snapshots
    # as a fresh options-chain authorization.
    if text:
        text=('Saved ODME ('+expiry+') shows '+text+'.')
        if not live or _outlook_side(live.get('direction'))==0:
            text+=' Historical positioning only; current ODME support is unverified.'
    return {'text':text,'fresh_adverse':fresh_adverse,'trend_side':multi_side,
            'session_count':len(samples),'stale':False,'expiry':expiry,
            'latest_age_hours':age}


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
            'recent_count':len(recent),'recent_closes':recent,
            'all_closes':ordered[-600:]}


def _outlook_optional(optional: List[Dict[str,Any]], edge_ms: int) -> Dict[str,Any]:
    evidence={'aurora':None,'aurora_transition':False,'liq':None,'liq_price_response':None,
              'aurora_asymmetry':0,'liq_sequence_side':0,'liq_sequence':''}
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
            ctx=latest.get('context') or _json_obj(latest.get('context_json'))
            up=str(ctx.get('eta_up_regime') or '').upper()
            down=str(ctx.get('eta_down_regime') or '').upper()
            if up=='SHARP' and down=='GRIND':evidence['aurora_asymmetry']=1
            elif down=='SHARP' and up=='GRIND':evidence['aurora_asymmetry']=-1
            same=0
            for _,r in reversed(own):
                if str(r.get('state') or '').upper()!=evidence['aurora']:break
                same+=1
            evidence['aurora_persistence_bars']=same
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
            # Distinct confirmed runs, never repeated last_event carried forward
            # on every saved observation. No inferred buy/sell aggressor volume.
            unique={}
            for t,r in own:
                ev=str(r.get('last_event') or '').upper()
                when=_ms(r.get('event_ms'))
                if not ev or not when or when>edge_ms or edge_ms-when>12*3600_000:continue
                unique[(ev,when)]=(ev,when)
            ordered=sorted(unique.values(),key=lambda z:z[1])
            recent=ordered[-6:]
            sells=sum(x[0]=='SELL_FULL_RUN' for x in recent)
            buys=sum(x[0]=='BUY_FULL_RUN' for x in recent)
            if sells>=2 and sells>buys:
                evidence['liq_sequence_side']=-1
                evidence['liq_sequence']='Repeated accepted sell-side liquidity runs remain in the recent sequence.'
            elif buys>=2 and buys>sells:
                evidence['liq_sequence_side']=1
                evidence['liq_sequence']='Repeated accepted buy-side liquidity runs remain in the recent sequence.'
    return evidence


def _outlook_odme(odme: Optional[Dict[str,Any]], snapshots: List[Dict[str,Any]], edge_ms: int) -> Dict[str,Any]:
    live=odme if isinstance(odme,dict) else {}
    direction=_outlook_side(live.get('direction'))
    strength=str(live.get('strength') or '').upper()
    latest=None;old=None
    options=[r for r in (snapshots or []) if _ms(r.get('ts')) and _ms(r.get('ts'))<=edge_ms]
    if options:
        latest=max(options,key=lambda z:_ms(z.get('ts')))
        same=[x for x in options if str(x.get('expiry') or '').upper()==str(latest.get('expiry') or '').upper() and
              str(x.get('snapshot_id') or '')!=str(latest.get('snapshot_id') or '')]
        if same:old=max(same,key=lambda z:_ms(z.get('ts')))
    tilt=str((latest or {}).get('odme_tilt') or '')
    tilt_change=(str(old.get('odme_tilt') or '')+' → '+tilt) if old and tilt and str(old.get('odme_tilt') or '')!=tilt else None
    return {'dir':direction,'strength':strength,'tilt':tilt,'tilt_change':tilt_change,
            'latest_age_hours':(edge_ms-_ms(latest.get('ts')))/3_600_000 if latest else None,
            'current_live':bool(live),'old':old,'latest':latest}


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
    if odme.get('current_live') and odme.get('latest_age_hours') is not None and 0<=odme['latest_age_hours']<=24:
        add(latest.get('pe_wall'),'ODME PE wall',1)
        add(latest.get('ce_wall'),'ODME CE wall',1)
    lowers=sorted((x for x in candidates if x[0]<spot),key=lambda z:(spot-z[0],-z[2]))
    uppers=sorted((x for x in candidates if x[0]>spot),key=lambda z:(z[0]-spot,-z[2]))
    # Prefer a recent observed boundary if it is reasonably near, otherwise nearest FP/structural.
    return {'lower':lowers[0] if lowers else None,'upper':uppers[0] if uppers else None,
            'below_next':lowers[1] if len(lowers)>1 else None,
            'above_next':uppers[1] if len(uppers)>1 else None}


def _outlook_higher_tf_destination(edge: Dict[str,Any],
                                   journey: Dict[str,Any], side: int) -> Dict[str,Any]:
    """Find the next CURRENT EDGE higher-TF zone in the observed direction.

    DISPLAY ONLY. A zone is a route destination, not a predicted reversal or
    an entry trigger. Direction must come from a contiguous sequence of real
    closes; the mere fact that spot is above demand cannot mean MOVE_AWAY.
    Historical ARCHIVED_EDGE_ROLE references are not used as live destinations.
    """
    if not side:return {}
    spot=_num(journey.get('spot'))
    tf=_num(journey.get('tf'))
    rows=journey.get('recent_closes') or []
    drift=_num(journey.get('drift'))
    width=_num(journey.get('width'))
    duration=_num(journey.get('duration_hours')) or 0
    if (not spot or not tf or len(rows)<4 or drift is None or width is None
            or duration<0.5 or side*drift<max(0.20,0.25*width)):
        return {}
    starting_price=rows[0][1]
    candidates=[]
    for role in ('defender','challenger'):
        side_of_zone=str(edge.get(role+'_side') or '').upper()
        role_tf=_higher_tf_minutes(edge.get(role+'_tf'))
        lo=_num(edge.get(role+'_low'));hi=_num(edge.get(role+'_high'))
        state=str(edge.get(role+'_state') or '').upper()
        if (not role_tf or role_tf<max(60,2*tf) or not lo or not hi or lo>hi or
            state in ('','NONE','RUN','BROKEN','INACTIVE','INVALID')):
            continue
        if side<0 and side_of_zone=='DEMAND' and spot>=lo:
            gap=max(0,spot-hi)
            start_gap=max(0,starting_price-hi)
        elif side>0 and side_of_zone=='SUPPLY' and spot<=hi:
            gap=max(0,lo-spot)
            start_gap=max(0,lo-starting_price)
        else:
            continue
        # Genuine distance reduction must be observed, not inferred from which
        # side of the line price happens to be on. Require reasonable proximity
        # for a *nearby* destination-aware outlook, not an entry veto.
        if (gap/spot>0.035 or
                start_gap-gap<max(0.001*spot,0.07*abs(starting_price-spot))):
            continue
        state_name=('TESTING' if lo<=spot<=hi else 'APPROACHING')
        zone_flow=0
        field={15:'of_15',60:'of_60',125:'of_125',240:'of_240'}.get(role_tf)
        if field:zone_flow=_outlook_side(edge.get(field))
        if not zone_flow:
            raw=_json_obj(edge.get('raw_json'))
            for item in raw.get('edge_router_map') or []:
                if isinstance(item,dict) and _higher_tf_minutes(item.get('tf'))==role_tf:
                    zone_flow=_outlook_side(item.get('of_state'))
                    break
        candidates.append({'side':side_of_zone,'tf':role_tf,'low':lo,'high':hi,
                           'gap':gap,'gap_pct':100*gap/spot,'phase':state_name,
                           'flow':zone_flow,'source':'CURRENT_ACTIVE_EDGE_ROLE',
                           'role':role,'state':state})
    if not candidates:return {}
    # Prefer the closest prospective barrier. Higher TF wins a near-tie.
    return min(candidates,key=lambda x:(round(x['gap']/spot,4),-x['tf']))


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
                             current_odme: Optional[Dict[str,Any]]=None,
                             strategic_context: Optional[Dict[str,Any]]=None,
                             selected_expiry: str='') -> Dict[str,Any]:
    """Predictive *scenario* display; never modifies executable trade decisions.

    Combines price-effectiveness, persistent EDGE OF, FP freshness, macro/MTF
    disagreements, source-fresh AURORA and liquidity, and live/historical ODME.
    Qualitative conviction is evidence agreement, NOT a calibrated probability.
    """
    j=_outlook_journey(edge,edge_tape)
    if not j.get('spot') or not j.get('now'):
        return {'label':'UNAVAILABLE','commentary':'Insufficient verified EDGE price data for a market outlook.'}
    o=_outlook_optional(optional_history,j['now'])
    # Never explain the active contract with an older expiry's positioning.
    # If no expiry is selected, _outlook_odme still compares only equal expiries.
    contract=str(selected_expiry or '').strip().upper()
    relevant_odme=[r for r in (odme_history or []) if not contract or
                   str(r.get('expiry') or '').strip().upper()==contract]
    d=_outlook_odme(current_odme,relevant_odme,j['now'])
    story=strategic_context if isinstance(strategic_context,dict) else {}
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
    # ODME trajectory is longitudinal context, not another independent live
    # direction vote. Opposing day-on-day expansion can reduce confidence.
    evolution=_odme_evolution(current_odme,relevant_odme,j['now'],side)
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
        # Evidence families rather than number of correlated EDGE labels:
        # price/OF progress is the base; location, live ODME and optional
        # indicators may corroborate, each family at most once.
        location_agrees=bool(story.get('status')=='RECOGNIZED' and
            ((side>0 and story.get('side')=='DEMAND' and j['spot']>=(_num(story.get('high')) or float('inf'))) or
             (side<0 and story.get('side')=='SUPPLY' and j['spot']<=(_num(story.get('low')) or 0))))
        odme_agrees=bool(side and d.get('current_live') and d['dir']==side)
        optional_agrees=bool(side and (aura_side==side or o.get('liq_sequence_side')==side))
        families=sum((location_agrees,odme_agrees,optional_agrees))
        if side and effective and families>=2 and not conflicts and j['real_count']>=12:
            quality='HIGH'
        if side and len(conflicts)>=2:quality='LOW'
    if side==0 and has_path and width and width<0.30:quality='LOW'
    if evolution.get('fresh_adverse') and quality=='HIGH':quality='MODERATE'
    elif evolution.get('fresh_adverse') and quality=='MODERATE':quality='LOW'
    if (story.get('status')=='CHALLENGED' and story.get('side')=='DEMAND' and side>0) or (
            story.get('status')=='CHALLENGED' and story.get('side')=='SUPPLY' and side<0):
        quality='LOW'
    # This assessment is purely explanatory; it does not enter any gate, strike
    # selection, Renko logic or campaign management.
    destination=_outlook_higher_tf_destination(edge,j,side)
    if destination and quality=='HIGH':
        quality='MODERATE'  # confidence in *approach* is not proof of a break
    side_name='BULLISH' if side>0 else 'BEARISH' if side<0 else 'RANGE'
    label=f'{side_name} {kind}' if side else kind
    if story.get('status')=='RECOGNIZED' and side>0 and story.get('side')=='DEMAND':
        label='BULLISH RECOVERY DEVELOPING'
    elif story.get('status')=='RECOGNIZED' and side<0 and story.get('side')=='SUPPLY':
        label='BEARISH DECLINE DEVELOPING'
    elif story.get('status')=='RECOGNIZED' and side<0 and story.get('side')=='DEMAND':
        label='BEARISH PULLBACK / HIGHER-TF DEMAND UNDER TEST'
    elif story.get('status')=='RECOGNIZED' and side>0 and story.get('side')=='SUPPLY':
        label='BULLISH REBOUND / HIGHER-TF SUPPLY UNDER TEST'
    elif story.get('status')=='RECOGNIZED' and not side:
        label='PAUSE NEAR HIGHER-TF CONTEXT'
    if destination:
        target_name='DEMAND' if side<0 else 'SUPPLY'
        label=(f'{side_name} APPROACH TO HIGHER-TF {target_name}'
               if destination['phase']=='APPROACHING'
               else f'{side_name} TESTING HIGHER-TF {target_name}')
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
    if d.get('tilt_change') and quality!='HIGH' and not evolution.get('text'):
        first+=' ODME positioning has recently changed.'
    if (d['dir']==-side and side and quality!='HIGH'):
        first+=' Current ODME positioning does not support the directional outlook.'
    # The opening carries the strategic origin and ODME developing picture,
    # while the familiar Most likely / risk / Renko statements stay unchanged.
    origin=_strategic_opening(story,side,j['spot'])
    if destination:
        zone=f'{_strategic_tf_label(destination["tf"])} {destination["side"].lower()} at '
        zone+=f'{_outlook_fmt(destination["low"])}–{_outlook_fmt(destination["high"])}'
        action='Sellers' if side<0 else 'Buyers'
        flow_desc=('; higher-TF order flow still supports buyers' if side<0 else
                   '; higher-TF order flow still supports sellers')
        caveat=flow_desc if destination.get('flow')==-side else ''
        if destination['phase']=='APPROACHING':
            first=f'{action} retain near-term control, but price is approaching active {zone}{caveat}.'
        else:
            first=f'{action} retain near-term control as price tests active {zone}{caveat}.'
        # A zone which explains the old journey can coexist with a *different*
        # current destination, but repeating both makes the primary instruction
        # unclear. Give the current destination priority in this display.
        if o.get('liq_sequence_side')==side and o.get('liq_sequence'):
            first+=' '+o['liq_sequence']
        if o.get('liq')=='SELL_FULL_SWEEP' and side<0 and o.get('liq_level') and j['spot']>o['liq_level']:
            first+=' A recent downside liquidity sweep was reclaimed, but a higher-TF reversal is not confirmed.'
        elif o.get('liq')=='BUY_FULL_SWEEP' and side>0 and o.get('liq_level') and j['spot']<o['liq_level']:
            first+=' A recent upside liquidity sweep reversed, but a higher-TF reversal is not confirmed.'
    else:
        if origin:first=origin+' '+first
        if (side and o.get('liq_sequence_side')==side and o.get('liq_sequence')):
            first+=' '+o['liq_sequence']
        if side and o.get('aurora_asymmetry')==-side:
            first+=' Candle AURORA indicates a sharper opposing path; follow-through needs confirmation.'
    if evolution.get('text'):first+=' '+evolution['text']
    main=''
    alternate=''
    down=levels.get('lower');up=levels.get('upper')
    if destination:
        zone=f'{_strategic_tf_label(destination["tf"])} {destination["side"].lower()}'
        boundaries=f'{_outlook_fmt(destination["low"])}–{_outlook_fmt(destination["high"])}'
        if side<0:
            if destination['phase']=='APPROACHING':
                main=(f'**Most likely:** Selling may continue toward {zone} ({boundaries}). '
                      'Avoid chasing fresh shorts into this higher-TF zone; wait for a verified '
                      'defence or accepted break. If already short, review profit protection '
                      'under the existing campaign rules.')
            else:
                main=(f'**Most likely:** The market is testing {zone} ({boundaries}). '
                      'Do not assume either a bounce or a breakdown; wait for confirmed '
                      'defence or accepted break. If already short, review profit protection '
                      'under the existing campaign rules.')
            alternate=(f'**Rebound risk:** A confirmed defence of {zone} could interrupt '
                       'the current decline.')
        else:
            if destination['phase']=='APPROACHING':
                main=(f'**Most likely:** Buying may extend toward {zone} ({boundaries}). '
                      'Avoid chasing fresh longs into this higher-TF zone; wait for a verified '
                      'rejection or accepted break. If already long, review profit protection '
                      'under the existing campaign rules.')
            else:
                main=(f'**Most likely:** The market is testing {zone} ({boundaries}). '
                      'Do not assume either rejection or breakout; wait for confirmed '
                      'rejection or accepted break. If already long, review profit protection '
                      'under the existing campaign rules.')
            alternate=(f'**Pullback risk:** A confirmed rejection at {zone} could interrupt '
                       'the current advance.')
    elif side<0:
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
            'strategic_context':story,'higher_tf_destination':destination,
            'odme_evolution':evolution,
            'trade_authority':'EDGE_ENTRY_AND_FRESH_ODME_ONLY'}
