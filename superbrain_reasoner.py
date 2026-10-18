"""WISH SuperBrain EDGE-only context engine. All values read from EDGE, not re-calculated.

No AURORA, STRUCTURE, LIQUIDITY or unverified broker inputs. ODME direction is
optional scan context; option actions belong exclusively to Level 3 app policy.
"""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional

REASONER_VERSION='SB_EDGE_ONLY_20261018_V1'


def _num(v):
    try:
        n=float(v)
        return n if n==n else None
    except (ValueError,TypeError):return None


def _obj(v):
    if isinstance(v,dict):return v
    try:
        z=json.loads(str(v or ''))
        return z if isinstance(z,dict) else {}
    except (ValueError,TypeError):return {}


def _latest_edge(evidence):
    rows=[r for r in evidence.get('tv',[]) if isinstance(r,dict) and str(r.get('source') or '').upper()=='EDGE']
    if not rows:return {}
    def score(r):
        tf=str(r.get('tf') or ''),str(r.get('edge_required_exec_tf') or r.get('exec_tf') or '')
        return (str(r.get('edge_tracking_state') or '').upper()=='ACTIVE' and tf[0]==tf[1],_num(r.get('bar_time')) or 0)
    return sorted(rows,key=score,reverse=True)[0]


def _fp(row,side):
    p='fp_d_' if side=='DEMAND' else 'fp_s_'
    def v(x):return row.get(p+x)
    return {'side':side,'id':v('zone_id'),'low':_num(v('low')),'high':_num(v('high')),
            'status':str(v('status') or '').upper(),'event':str(v('event_type') or '').upper(),
            'context':str(v('context_state') or '').upper(), 'delta':_num(v('session_delta')),
            'strength':str(v('strength') or ''),'interacting':str(v('interacting')).lower() in {'true','1'}}


def _fmt(n):
    if n is None:return '?'
    return f'{n:,.2f}' if abs(n)>=100 else f'{n:,.4f}'


def _fp_read(cur,prev,price,previous_price):
    if cur['low'] is None or cur['high'] is None:return None
    side=cur['side'];d=cur['delta'];status=cur['status'];event=cur['event']
    name=f"{side.title()} FP {_fmt(cur['low'])}–{_fmt(cur['high'])}"
    attacking=(d is not None and (d<0 if side=='DEMAND' else d>0))
    defending=(d is not None and (d>0 if side=='DEMAND' else d<0))
    rejected=(price is not None and (price>cur['high'] if side=='DEMAND' else price<cur['low']))
    adverse=(previous_price is not None and price is not None and (price<previous_price if side=='DEMAND' else price>previous_price))
    same_prev=bool(prev and prev.get('id')==cur.get('id') and prev.get('low')==cur.get('low') and prev.get('high')==cur.get('high'))
    delta_text=f" Cumulative signed-volume estimate {d:+,.0f}." if d is not None else ''
    if status=='RUN' or event=='RUN':
        detail='Confirmed RUN: this zone failed; the earlier defensive thesis no longer applies.'
    elif 'ABSORPTION' in event or (attacking and rejected):
        detail='Attack-side volume built without sustained penetration: possible absorption / early defence, not yet a guaranteed reversal.'
    elif 'AGGRESSIVE' in event or (defending and rejected):
        detail='Defensive pressure is accompanied by price rejection; watch separation from the zone.'
    elif attacking and adverse:
        detail='Attack-side pressure is achieving adverse price progress; defence is weakening.'
    elif attacking:
        detail='Attack-side pressure is present but decisive progress is not established; monitor the response.'
    elif defending:
        detail='Defender-side signed volume is present; price progress is needed to validate it.'
    else:
        detail='Zone is active; pressure direction is not conclusive from the last snapshot.'
    if same_prev and d is not None and prev['delta'] is not None:
        delta_text+=f" Change since prior observed EDGE candle {d-prev['delta']:+,.0f}."
    if 'WAITING' in cur['context'] or 'BREACH_PENDING' in cur['context']:
        detail+=' Confirmation remains pending.'
    return f'{name}: {detail}{delta_text}'


def analyze_market(evidence:Dict[str,Any],previous_evidence:Dict[str,Any],open_trades:Optional[List[Dict[str,Any]]]=None):
    current=_latest_edge(evidence)
    previous=_latest_edge(previous_evidence or {})
    price=_num(current.get('close'))
    previous_price=_num(previous.get('close'))
    of=str(current.get('exec_of') or 'UNKNOWN').upper()
    tf=str(current.get('edge_required_exec_tf') or current.get('exec_tf') or current.get('tf') or '')
    valid=bool(current and str(current.get('edge_tracking_state') or '').upper()=='ACTIVE' and
      str(current.get('tf') or '')==tf)
    macro=str(current.get('macro') or 'UNKNOWN').upper()
    raw=_obj(current.get('raw_json'))
    last_raw=_obj(previous.get('raw_json'))
    current_book=(raw.get('fp_hurdle_book') or {}) if isinstance(raw.get('fp_hurdle_book'),dict) else {}
    lines=[]
    if not valid:lines.append('EDGE feed is missing, suspended or on a non-execution timeframe; no new entry decision is safe.')
    else:
        lines.append(f"EDGE at {_fmt(price)}: execution control {of}; macro {macro}; required execution TF {tf}m.")
    def role(r):
        v=str(current.get(r+'_side') or '').upper()
        lo=_num(current.get(r+'_low'));hi=_num(current.get(r+'_high'))
        return f"{r.title()} {v} {_fmt(lo)}–{_fmt(hi)}" if v and lo is not None and hi is not None else f"{r.title()} unavailable"
    lines.append('Structural battlefield: '+role('defender')+'; '+role('challenger')+'.')
    interactions=[]
    for side in ['DEMAND','SUPPLY']:
        f=_fp(current,side);p=_fp(previous,side) if previous else None
        if f['low'] is None or not f['status'] or (not f['interacting'] and f['status']!='RUN' and not f['event']):continue
        meaning=_fp_read(f,p,price,previous_price)
        if meaning:interactions.append(meaning)
    if not interactions and current_book:
        zones=current_book.get('hurdles') or []
        if isinstance(zones,list):
            valid_z=[z for z in zones if isinstance(z,dict) and z.get('low') is not None and z.get('high') is not None]
            valid_z.sort(key=lambda z:abs(float(z.get('low') or 0)-(price or 0)))
            for z in valid_z[:2]:
                interactions.append(f"Nearest {z.get('side','FP')} {_fmt(_num(z.get('low')))}–{_fmt(_num(z.get('high')))}; state {z.get('status') or z.get('context') or 'active'}; strength {z.get('strength') or 'not reported'}.")
    lines.extend(interactions[:2])
    if not interactions:lines.append('No material FP interaction currently reported. Wait for the next confirmed EDGE event.')
    odme=evidence.get('odme') or {}
    if odme:
        lines.append(f"ODME reference: POC {_fmt(_num(odme.get('option_poc')))}, CE wall {_fmt(_num(odme.get('ce_wall')))}, PE wall {_fmt(_num(odme.get('pe_wall')))}, safer CE {_fmt(_num(odme.get('safer_sell_ce')))}, safer PE {_fmt(_num(odme.get('safer_sell_pe')))}. Use only after fresh ODME scan.")
    return {'reasoner_version':REASONER_VERSION,'price':price,'macro':macro,'exec_of':of,
      'edge_active':valid,'posture':'EDGE_ONLY_SCAN_ODME' if valid else 'NO_ENTRY_STALE_EDGE',
      'lines':lines,'defender':{'side':current.get('defender_side'),'low':_num(current.get('defender_low')),'high':_num(current.get('defender_high'))},
      'challenger':{'side':current.get('challenger_side'),'low':_num(current.get('challenger_low')),'high':_num(current.get('challenger_high'))},
      'aurora':{'state':'MANUAL_ONLY'},'liquidity':{},'fork':{},'trade_plan':{},'open_campaign_count':len(open_trades or [])}
