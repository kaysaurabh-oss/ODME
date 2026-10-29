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
