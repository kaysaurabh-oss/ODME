"""Pre-contact FP barrier intelligence — display only, v20261124.

Never alters EDGE/Pine, ODME, Renko, trade eligibility, campaigns or alerts.
Sources: latest confirmed EDGE + entire available instrument tape, archived
Level 2 FP events (across actual TF handoffs), and same-expiry ODME evolution.
Every forecast is conditioned on information *already observed* at the scan.
This module is deliberately conservative about missing/truncated FP books.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple


def _n(x: Any) -> Optional[float]:
    try:
        y=float(x)
        return y if math.isfinite(y) else None
    except (ValueError, TypeError):
        return None


def _ms(x: Any) -> Optional[int]:
    if x is None or x == '':return None
    if isinstance(x,str) and not x.replace('.','',1).isdigit():
        try:
            dt=datetime.fromisoformat(x.strip().replace('Z','+00:00'))
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp()*1000)
        except (ValueError,TypeError):return None
    y=_n(x)
    if not y or y<=0:return None
    return int(y*1000 if y<1e11 else y)


def _obj(raw: Any) -> Dict[str,Any]:
    if isinstance(raw,dict):return raw
    try:
        v=json.loads(str(raw or ''))
        return v if isinstance(v,dict) else {}
    except (ValueError,TypeError):return {}


def _list(raw:Any) -> List[Any]:
    if isinstance(raw,list):return raw
    try:
        v=json.loads(str(raw or ''))
        return v if isinstance(v,list) else []
    except (ValueError,TypeError):return []


def _fmt(x: Any) -> str:
    y=_n(x)
    if y is None:return '?'
    return f'{y:,.2f}'


def _tf(s:Any) -> str:
    s=str(s or '').strip().upper()
    if s in ('W','1W','WEEK','WEEKLY','10080'):return 'Weekly'
    if s in ('D','1D','DAY','DAILY','1440'):return 'Daily'
    a=_n(s)
    return f'{int(a)}m' if a and a>0 else s


def _active(zone:Dict[str,Any]) -> bool:
    state=str(zone.get('status') or zone.get('context') or '').upper()
    return (state not in ('RUN','BROKEN','INVALID','INACTIVE','NONE','CONSUMED')
            and zone.get('valid') is not False and str(zone.get('valid') or '').lower()!='false'
            and str(zone.get('event_type') or '').upper()!='RUN_CONFIRMED')


def _headings(zone:Dict[str,Any]) -> str:
    return f"{_tf(zone.get('tf'))} FP {str(zone.get('side') or '').title()} {_fmt(zone.get('low'))}–{_fmt(zone.get('high'))}"


def read_level2_history(store:Any, instrument:str, edge:Dict[str,Any]) -> List[Dict[str,Any]]:
    """Read every retained Level 2 event for this instrument, across TFs.

    The log is append-only and already exists. No new worksheet or writer.
    An alert is evidence only once it was *available*, not retrospectively at
    a past candle timestamp. Scheduled delivery timestamps can follow bars.
    """
    now=_ms(edge.get('bar_time'))
    if not now:return []
    symbol=str(instrument or '').upper()
    try:
        ws=store.sheet.worksheet('SB_L2_ALERT_LOG')
        hdr=ws.row_values(1)
        wanted=('alert_key','queued_at','instrument','alert_type','event_bar_ms','status')
        if not all(k in hdr for k in wanted):return []
        data=ws.get_all_records()
        result=[]
        for z in data:
            if str(z.get('instrument') or '').upper()!=symbol:continue
            typ=str(z.get('alert_type') or '').upper()
            if typ not in ('DEFENCE_CONFIRMED','RUN_CONFIRMED','BREACH_TEST',
                           'DEFENCE_ABSORPTION','DEFENCE_AGGRESSIVE','TF_CHANGE'):
                continue
            at=_ms(z.get('event_bar_ms'));queued=_ms(z.get('queued_at'))
            if not at or at>now:continue
            # A bar is visible on the chart at its confirmed close; the alert
            # cannot serve as pre-event knowledge before it was recorded.
            if queued and queued>now+2*60_000:continue
            key=str(z.get('alert_key') or '').split('|')
            if typ=='TF_CHANGE':
                result.append({'kind':'TF_CHANGE','at':at,'known_at':queued or at,
                               'source':'LEVEL2','key':str(z.get('alert_key') or ''),
                               'tf_from':key[2] if len(key)>3 else '',
                               'tf_to':key[3] if len(key)>3 else ''})
                continue
            if len(key)<6 or key[0]!='FP':continue
            pair=key[3].split(':',1)
            if len(pair)!=2 or pair[0] not in ('DEMAND','SUPPLY'):continue
            # Values are sourced from the actual Level 2 event text when
            # available; they are never reconstructed from present prices.
            message=str(z.get('message') or '')
            levels=re.search(r'<b>FP:</b>\s*([\d,.]+)\s*[–-]\s*([\d,.]+)',message)
            lo=_n(levels.group(1).replace(',','')) if levels else None
            hi=_n(levels.group(2).replace(',','')) if levels else None
            px=re.search(r'<b>Price:</b>\s*([\d,.]+)',message)
            p=_n(px.group(1).replace(',','')) if px else None
            result.append({'kind':'FP','at':at,'known_at':queued or at,
                           'source':'LEVEL2','tf':key[2], 'side':pair[0],
                           'zone_id':pair[1],'type':typ,'low':lo,'high':hi,
                           'price':p,'key':str(z.get('alert_key') or '')})
        return sorted(result,key=lambda z:(z['at'],z['known_at']))
    except Exception:
        # Loss of alert access lowers evidence, not a trading gate bypass.
        return []


def fp_event_ledger(edge:Dict[str,Any], tape:List[Dict[str,Any]],
                    l2:List[Dict[str,Any]], *, asof:Optional[int]=None) -> List[Dict[str,Any]]:
    """Event-sourced cross-TF FP ledger, without duplicated alerts/snapshots.

    Includes older L2 events beyond the selected execution TF. Source-based
    zone identity includes TF; identical zone ID on another TF is NOT merged.
    Failed holds and later breaks are retained as chronological outcomes.
    """
    now=asof or _ms(edge.get('bar_time')) or 0
    symbol=str(edge.get('instrument') or '').upper()
    events={}
    def add(z:Dict[str,Any],tf:Any,known:Optional[int],src:str):
        at=_ms(z.get('at') or z.get('event_time') or z.get('fp_event_time'))
        if not at or at>now or known is None or known>now or at>known+120_000:return
        typ=str(z.get('type') or z.get('event_type') or z.get('fp_event_type') or '').upper()
        if typ not in ('DEFENCE_CONFIRMED','RUN_CONFIRMED','DEFENCE_ABSORPTION',
                       'DEFENCE_AGGRESSIVE','BREACH_TEST'):return
        side=str(z.get('side') or z.get('fp_side') or '').upper()
        zid=str(z.get('zone_id') or z.get('fp_zone_id') or '').strip()
        if side not in ('DEMAND','SUPPLY') or not zid:return
        low=_n(z.get('low') if z.get('low') is not None else z.get('fp_low'))
        high=_n(z.get('high') if z.get('high') is not None else z.get('fp_high'))
        if low is not None and high is not None and low>high:return
        tf=str(tf or '')
        if not tf:return
        # Event identity is immutable even if its boundaries get reduced.
        key=(tf,side,zid,typ,at)
        value={'at':at,'known_at':known,'tf':tf,'side':side,'zone_id':zid,
               'type':typ,'kind':'BREAK' if typ=='RUN_CONFIRMED' else 'HOLD' if typ=='DEFENCE_CONFIRMED' else 'PROBE',
               'low':low,'high':high,'source':src}
        # Level 2 contains original event boundaries and is preferable to a
        # later reduced selected FP boundary when timestamps match.
        old=events.get(key)
        if old is None or (src=='LEVEL2' and old['source']!='LEVEL2'):
            events[key]=value
    for z in l2 or []:
        if z.get('kind')=='FP':add(z,z.get('tf'),_ms(z.get('known_at')),'LEVEL2')
    for row in list(tape or [])+[dict(edge,bar_ms=edge.get('bar_time'))]:
        if not isinstance(row,dict) or row.get('synthetic_price_only'):continue
        if str(row.get('instrument') or '').upper()!=symbol:continue
        seen=_ms(row.get('bar_ms'))
        if not seen or seen>now:continue
        add(row,row.get('tf'),seen,'EDGE_TAPE')
        blob=row.get('fp_hurdles_json')
        if not blob:
            blob=_obj(row.get('raw_json')).get('fp_hurdle_book')
        book=_obj(blob)
        hurdles=blob if isinstance(blob,list) else _list(blob) if isinstance(blob,str) and blob.lstrip().startswith('[') else book.get('hurdles') or []
        for z in hurdles:
            if isinstance(z,dict):add(z,row.get('tf'),seen,'EDGE_HURDLE')
    return sorted(events.values(),key=lambda v:(v['at'],v['known_at'],v['tf'],v['zone_id']))


def _current_book(edge:Dict[str,Any], tape:List[Dict[str,Any]]) -> Dict[str,Any]:
    raw=_obj(edge.get('raw_json'))
    book=_obj(raw.get('fp_hurdle_book'))
    # Archived tape rows can carry the same source-confirmed hurdle book in AH.
    if not book and edge.get('fp_hurdles_json'):
        old=_list(edge.get('fp_hurdles_json'))
        if old:book={'hurdles':old}
    zones=[];keys=set()
    def take(z:Dict[str,Any],tf:Any,source:str):
        if not isinstance(z,dict) or str(z.get('side') or '').upper() not in ('DEMAND','SUPPLY'):return
        if not _active(z):return
        lo=_n(z.get('low'));hi=_n(z.get('high'))
        if lo is None or hi is None or lo>hi:return
        side=str(z.get('side')).upper()
        zid=str(z.get('zone_id') or f'{lo}:{hi}')
        key=(str(tf),side,zid)
        if key in keys:return
        keys.add(key)
        zones.append(dict(side=side,zone_id=zid,tf=str(tf),low=lo,high=hi,
                          formation_time=_ms(z.get('formation_time')),
                          status=str(z.get('status') or z.get('context') or ''),
                          strength=_n(z.get('strength_score')),source=source,
                          event_type=str(z.get('event_type') or '').upper(),
                          touched=bool(z.get('interacting'))))
    for z in book.get('hurdles') or []:take(z,edge.get('tf'),'LIVE_BOOK')
    if str(edge.get('fp_side') or '').upper() in ('DEMAND','SUPPLY'):
        take({'side':edge.get('fp_side'),'zone_id':edge.get('fp_zone_id'),
              'low':edge.get('fp_low'),'high':edge.get('fp_high'),
              'status':edge.get('fp_status') or edge.get('fp_context_state'),
              'event_type':edge.get('fp_event_type'),
              'strength_score':edge.get('fp_strength_score'),
              'formation_time':edge.get('fp_formation_time'),
              'interacting':edge.get('fp_interacting'),
              'valid':edge.get('fp_valid')},edge.get('tf'),'SELECTED_FP')
    # The live book can list only a limited number of hurdles. Do not promote
    # absent historical zones to LIVE, and do not assume it is exhaustive.
    return {'zones':zones,'truncated':bool(book.get('supply_truncated') or book.get('demand_truncated')),
            'has_book':bool(book.get('hurdles') or zones),'supply_total':book.get('supply_total'),
            'demand_total':book.get('demand_total')}


def _roles(edge:Dict[str,Any]) -> List[Dict[str,Any]]:
    roles=[]
    for role in ('defender','challenger'):
        side=str(edge.get(role+'_side') or '').upper()
        lo=_n(edge.get(role+'_low'));hi=_n(edge.get(role+'_high'))
        state=str(edge.get(role+'_state') or '').upper()
        if state in ('RUN','BROKEN','INVALID','INACTIVE','NONE'):continue
        if side not in ('DEMAND','SUPPLY') or lo is None or hi is None or lo>=hi:continue
        roles.append({'role':role,'side':side,'low':lo,'high':hi,'tf':str(edge.get(role+'_tf') or ''),'source':'STRUCTURAL'})
    # Full TradingView router-map references, across every supplied TF. These
    # are structural location references, not FP signals and not proof that a
    # historical zone is still actively defended. Current selected roles above
    # remain the stronger active-status evidence.
    raw=_obj(edge.get('raw_json'))
    now=_ms(edge.get('bar_time')) or 0
    for item in raw.get('edge_router_map') or []:
        if not isinstance(item,dict):continue
        tf=str(item.get('tf') or '')
        for key,side in (('d1','DEMAND'),('s1','SUPPLY')):
            z=item.get(key)
            if not isinstance(z,dict):continue
            lo=_n(z.get('low'));hi=_n(z.get('high'));at=_ms(z.get('time'))
            if lo is None or hi is None or lo>=hi or (at and at>now):continue
            if any(x['side']==side and x['tf']==tf and x['low']==lo and x['high']==hi for x in roles):continue
            roles.append({'role':'router_'+key,'side':side,'low':lo,'high':hi,
                          'tf':tf,'source':'ROUTER_REFERENCE'})
    return roles


def _approach(edge:Dict[str,Any],book:Dict[str,Any],side:int,roles:List[Dict[str,Any]]) -> Dict[str,Any]:
    spot=_n(edge.get('close'))
    if spot is None or spot<=0:return {}
    cands=[]
    for z in book.get('zones') or []:
        if z['side']=='SUPPLY' and z['high']>=spot:
            cands.append(dict(z,distance=max(0,z['low']-spot),stage=('TESTING' if z['low']<=spot<=z['high'] else 'BEFORE_TOUCH')))
        elif z['side']=='DEMAND' and z['low']<=spot:
            cands.append(dict(z,distance=max(0,spot-z['high']),stage=('TESTING' if z['low']<=spot<=z['high'] else 'BEFORE_TOUCH')))
    byside={}
    for typ in ('DEMAND','SUPPLY'):
        xs=[z for z in cands if z['side']==typ]
        byside[typ]=min(xs,key=lambda z:(z['distance'],z['high']-z['low'])) if xs else None
    forward='SUPPLY' if side>0 else 'DEMAND' if side<0 else ''
    chosen=byside.get(forward) if forward else None
    tested=[z for z in cands if z['stage']=='TESTING' or z.get('touched')]
    if tested:
        selected=str(edge.get('fp_zone_id') or '')
        chosen=min(tested,key=lambda z:(0 if selected and z['zone_id']==selected else 1,z['high']-z['low']))
        chosen['stage']='TESTING'
    if not chosen:
        chosen=min(cands,key=lambda z:z['distance']) if cands else None
    # A missing FP map should not imply there are no obstacles. Structural
    # location can supply an explicitly identified next zone, not an FP claim.
    if not chosen:
        r=[]
        for role in roles:
            if role['side']=='SUPPLY' and role['high']>=spot:
                r.append(dict(role,zone_id=role['role'],distance=max(0,role['low']-spot),stage='BEFORE_TOUCH'))
            elif role['side']=='DEMAND' and role['low']<=spot:
                r.append(dict(role,zone_id=role['role'],distance=max(0,spot-role['high']),stage='BEFORE_TOUCH'))
        if forward:
            aligned=[x for x in r if x['side']==forward]
            chosen=min(aligned,key=lambda z:z['distance']) if aligned else None
        if not chosen and r:chosen=min(r,key=lambda z:z['distance'])
    if chosen:
        relevant=[z for z in cands if z['side']==chosen['side']]
        # Count unique, still-observed hurdles in the forward route; overlapping
        # physical zones are one barrier, never multiple votes on one level.
        independent=[]
        for z in sorted(relevant,key=lambda x:x['low']):
            if not independent or z['low']>independent[-1][1]:
                independent.append([z['low'],z['high']])
            else:
                independent[-1][1]=max(independent[-1][1],z['high'])
        chosen['route_barrier_count']=len(independent)
        chosen['touch_cluster']=(sum(max(z['low'],chosen['low'])<=min(z['high'],chosen['high']) for z in relevant)>1)
        for role in roles:
            if role['side']!=chosen['side']:continue
            # Overlapping structural zone provides authority; nearby is not
            # inferred from ATR or an arbitrary percentage price movement.
            overlap=max(chosen['low'],role['low'])<=min(chosen['high'],role['high'])
            if overlap:
                matched=chosen.setdefault('structural_matches',[])
                matched.append({'role':role['role'],'tf':role['tf'],'low':role['low'],
                                'high':role['high'],'source':role['source']})
                if role['source']=='STRUCTURAL' or not chosen.get('structural_overlap'):
                    chosen['structural_overlap']={'role':role['role'],'tf':role['tf'],
                                                 'low':role['low'],'high':role['high'],
                                                 'source':role['source']}
        chosen['directional_approach']=(side>0 and chosen['side']=='SUPPLY') or (side<0 and chosen['side']=='DEMAND')
    return {'target':chosen,'nearest_supply':byside['SUPPLY'],'nearest_demand':byside['DEMAND'],
            'side':side,'book_truncated':book.get('truncated',False)}


def _dedupe_confirmed(ledger:List[Dict[str,Any]], since:Optional[int], now:int) -> List[Dict[str,Any]]:
    results={}
    for e in ledger:
        if e['type'] not in ('DEFENCE_CONFIRMED','RUN_CONFIRMED'):continue
        if e['at']>now or (since and e['at']<since):continue
        key=(e['tf'],e['side'],e['zone_id'])
        old=results.get(key)
        if old is None or e['at']>=old['at']:
            results[key]=e
    return sorted(results.values(),key=lambda x:x['at'])


def _style(edge:Dict[str,Any],book:Dict[str,Any],ledger:List[Dict[str,Any]],
           direction:int,origin_at:Optional[int],target:Dict[str,Any]) -> Dict[str,Any]:
    now=_ms(edge.get('bar_time')) or 0
    events=_dedupe_confirmed(ledger,origin_at,now)
    # A new structural origin resets the *current journey*, but does not erase
    # the regime that preceded it. When no supporting FP evidence has developed
    # since the origin, that earlier regime is a pre-encounter warning, NOT a
    # claim that the old Challenger still controls the new journey.
    prior=[]
    if origin_at:
        prior=[e for e in _dedupe_confirmed(ledger,None,now)
               if e['at']<origin_at and e.get('known_at',e['at'])<origin_at][-8:]
    prior_against=sum(
        1 for e in prior if (
            ((e['side']=='DEMAND' and e['type']=='RUN_CONFIRMED') or
             (e['side']=='SUPPLY' and e['type']=='DEFENCE_CONFIRMED')) if direction>0 else
            ((e['side']=='SUPPLY' and e['type']=='RUN_CONFIRMED') or
             (e['side']=='DEMAND' and e['type']=='DEFENCE_CONFIRMED'))
        )
    ) if direction else 0
    # Read every retained event, but give only the latest distinct encounters
    # influence over the *present* approach. Older events remain available.
    events=events[-12:]
    supply_breaks=[x for x in events if x['side']=='SUPPLY' and x['type']=='RUN_CONFIRMED']
    demand_breaks=[x for x in events if x['side']=='DEMAND' and x['type']=='RUN_CONFIRMED']
    supply_holds=[x for x in events if x['side']=='SUPPLY' and x['type']=='DEFENCE_CONFIRMED']
    demand_holds=[x for x in events if x['side']=='DEMAND' and x['type']=='DEFENCE_CONFIRMED']
    in_favour=(supply_breaks+demand_holds if direction>0 else demand_breaks+supply_holds)
    against=(demand_breaks+supply_holds if direction>0 else supply_breaks+demand_holds)
    spot=_n(edge.get('close')) or 0
    support_side='DEMAND' if direction>0 else 'SUPPLY'
    # Only observed, still-live supports from Pine's live hurdle book, never
    # a claim of exhaustive zone formation from a potentially truncated book.
    support=[]
    for z in book.get('zones') or []:
        if z['side']!=support_side:continue
        if direction>0 and z['high']>spot:continue
        if direction<0 and z['low']<spot:continue
        born=z.get('formation_time')
        if not born or born>now or (origin_at and born<origin_at):continue
        support.append(z)
    support.sort(key=lambda z:z.get('formation_time') or 0)
    rising=(len(support)>=2 and all((a['low']<b['low'] if direction>0 else a['high']>b['high'])
                  for a,b in zip(support,support[1:])) and
            all(z.get('formation_time') for z in support))
    latest=events[-1] if events else None
    resolved_opposite=False
    if latest and direction:
        resolved_opposite=((latest['side']=='SUPPLY' and latest['type']=='DEFENCE_CONFIRMED' and direction>0) or
                           (latest['side']=='DEMAND' and latest['type']=='DEFENCE_CONFIRMED' and direction<0))
    # Explicit failed opposing Holds followed by Runs are informative at the
    # next barrier; the RUN itself supplies the evidence, no second OF vote.
    failed_opposing=[]
    raw_holds=[e for e in ledger if e['type']=='DEFENCE_CONFIRMED'
               and e['at']<=now and e.get('known_at',e['at'])<=now
               and ((direction>0 and e['side']=='SUPPLY') or (direction<0 and e['side']=='DEMAND'))
               and (not origin_at or e['at']>=origin_at)]
    for h in raw_holds:
        if any(x['tf']==h['tf'] and x['zone_id']==h['zone_id'] and x['at']>h['at']
               for x in (supply_breaks if direction>0 else demand_breaks)):
            failed_opposing.append(h)
    failed_opposing=sorted({(e['tf'],e['side'],e['zone_id']):e for e in failed_opposing}.values(),key=lambda e:e['at'])
    # A break in a formerly strong trend should not be assumed from the
    # absence of newly formed support; a clean displacement can persist.
    if origin_at and prior_against>=2 and len(in_favour)<=1 and not support:
        style='RECOVERY_AGAINST_PRIOR_CONTROL'
    elif len(in_favour)>=2 and not support:
        style='UNSUPPORTED_DISPLACEMENT'
    elif rising and len(in_favour)>=1:
        style='SUPPORTED_STAIRCASE'
    elif len(in_favour)>=2 and not against:
        style='DOMINANT_CONTINUATION'
    elif len(against)>=2:
        style='CONTESTED'
    elif not in_favour and not support:
        style='UNPROVEN_APPROACH'
    else:
        style='MIXED_APPROACH'
    return {'name':style,'supports':support,'supports_observed':len(support),
            'support_ladder':bool(rising),'support_book_truncated':book.get('truncated',False),
            'favour_events':len(in_favour),'against_events':len(against),
            'supply_breaks':len(supply_breaks),'demand_breaks':len(demand_breaks),
            'supply_holds':len(supply_holds),'demand_holds':len(demand_holds),
            'failed_opposing_holds':len(failed_opposing),'prior_regime_opposing_results':prior_against,
            'prior_regime_events':len(prior),'confirmed_events':len(events),
            'most_recent':latest,'opposite_latest':resolved_opposite}


def _odme_check(current:Dict[str,Any], history:List[Dict[str,Any]],
                target:Dict[str,Any],direction:int, now:int) -> Dict[str,Any]:
    """Only the caller's freshness-/expiry-validated ODME is live evidence.

    Historical snapshots may explain wall migration for this same expiry;
    earlier expired contracts must NEVER confirm today's option barrier.
    """
    if not current:return {'valid':False,'why':'ODME not fresh / matching expiry; no options confirmation.'}
    expiry=str(current.get('expiry') or '').strip().upper()
    if not expiry:return {'valid':False,'why':'ODME expiry unavailable.'}
    rows=[]
    for x in history or []:
        if str(x.get('expiry') or '').strip().upper()!=expiry:continue
        at=_ms(x.get('_ts_ms') or x.get('ts'))
        if at and at<=now+60_000:rows.append((at,x))
    rows.sort(key=lambda x:x[0])
    current_at=_ms(current.get('ts'))
    older=[x for t,x in rows if current_at and t<current_at and str(x.get('snapshot_id'))!=str(current.get('snapshot_id'))]
    prev=older[-1] if older else None
    ce=_n(current.get('active_ce_wall')) or _n(current.get('ce_wall'))
    pe=_n(current.get('active_pe_wall')) or _n(current.get('pe_wall'))
    lo=_n(target.get('low'));hi=_n(target.get('high'))
    atwall=(ce if target.get('side')=='SUPPLY' else pe)
    overlap=bool(lo is not None and hi is not None and atwall is not None and lo<=atwall<=hi)
    change=''
    if prev:
        old=_n(prev.get('active_ce_wall') if target.get('side')=='SUPPLY' else prev.get('active_pe_wall'))
        if old is None:old=_n(prev.get('ce_wall') if target.get('side')=='SUPPLY' else prev.get('pe_wall'))
        if old is not None and atwall is not None and old!=atwall:
            change='wall shifted higher' if atwall>old else 'wall shifted lower'
    tilt=str(current.get('odme_tilt') or '').upper()
    positioning='UNCLEAR'
    explicit=str(current.get('direction') or '').upper()
    if explicit in ('BULLISH','LONG','UPSIDE'):positioning='BULLISH'
    elif explicit in ('BEARISH','SHORT','DOWNSIDE'):positioning='BEARISH'
    elif 'MIXED' not in tilt and 'NO CLEAN EDGE' not in tilt:
        if 'BULLISH' in tilt:positioning='BULLISH'
        elif 'BEARISH' in tilt:positioning='BEARISH'
    return {'valid':True,'expiry':current.get('expiry'),'ce_wall':ce,'pe_wall':pe,
            'overlaps':overlap,'wall':atwall,'wall_change':change,
            'direction':positioning,'same_expiry_history':len(rows),
            'supports':(positioning=='BULLISH' and direction>0) or (positioning=='BEARISH' and direction<0),
            'opposes':(positioning=='BEARISH' and direction>0) or (positioning=='BULLISH' and direction<0),
            'why':('Matching-expiry '+('CE' if target.get('side')=='SUPPLY' else 'PE')+
                   ' wall '+(_fmt(atwall) if atwall is not None else 'unavailable')+
                   (' lies inside the approaching zone' if overlap else ' is an independent option reference')+
                   (f'; {change} since a prior same-expiry snapshot' if change else '')+
                   '. An OI wall is not a guaranteed rejection.')}


def _prior_cases(ledger:List[Dict[str,Any]],tape:List[Dict[str,Any]],asof:int,
                 target:Dict[str,Any],direction:int, style:str) -> Dict[str,Any]:
    """Conservative analog history. No invented failures from missing alerts.

    A complete case requires an actual pre-encounter EDGE snapshot, then a
    source-confirmed defence or break. The same zone's successive alerts are
    one case. Outcomes are measured at the source event timestamp, never using
    later price changes to fabricate a break.
    """
    groups={}
    for z in ledger:
        if z['at']>=asof or z['type'] not in ('DEFENCE_CONFIRMED','RUN_CONFIRMED','DEFENCE_AGGRESSIVE',
                                            'DEFENCE_ABSORPTION','BREACH_TEST'):continue
        key=(z['tf'],z['side'],z['zone_id'])
        groups.setdefault(key,[]).append(z)
    actuals=[]
    for key,events in groups.items():
        if key[1]!=target.get('side') or _tf(key[0])!=_tf(target.get('tf')):continue
        events.sort(key=lambda z:z['at'])
        first=events[0]['at']
        earlier=[r for r in tape or [] if isinstance(r,dict) and not r.get('synthetic_price_only')
                 and str(r.get('tf') or '')==str(key[0]) and
                 (_ms(r.get('bar_ms')) or 0)<first and
                 0<first-(_ms(r.get('bar_ms')) or 0)<=4*(_n(key[0]) or 15)*60_000]
        if not earlier:continue
        pre=max(earlier,key=lambda r:_ms(r.get('bar_ms')) or 0)
        pre_px=_n(pre.get('close'))
        if pre_px is None:continue
        first_resolved=[e for e in events if e['type'] in ('DEFENCE_CONFIRMED','RUN_CONFIRMED')]
        if not first_resolved:continue
        outcome=first_resolved[0]['type']
        # Only use truly comparable pre-touch episodes: actual approach on the
        # relevant side, with the same broad event-led directional expectation.
        # In absence of pre-touch original boundaries this remains UNKNOWN.
        low=next((e['low'] for e in events if e.get('low') is not None),None)
        high=next((e['high'] for e in events if e.get('high') is not None),None)
        if low is None or high is None:continue
        if key[1]=='SUPPLY' and pre_px>=low or key[1]=='DEMAND' and pre_px<=high:continue
        # Case features MUST be formed before this first interaction. Cross-
        # TF source alerts are allowed as earlier evidence but never labelled
        # as if they arrived before their queued timestamp.
        pre_at=_ms(pre.get('bar_ms')) or 0
        before=[z for z in ledger if z['at']<=pre_at and z.get('known_at',z['at'])<=pre_at]
        pre_style=_style(pre,_current_book(pre,[]),before,direction,None,target)['name']
        if pre_style!=style:continue
        # Do not claim a historical outcome of 'hold' if the level broke later.
        actuals.append({'key':key,'first':first,'outcome':outcome,
                        'eventual_break':any(e['type']=='RUN_CONFIRMED' for e in events)})
    n=len(actuals)
    r=sum(z['outcome']=='DEFENCE_CONFIRMED' for z in actuals)
    b=sum(z['outcome']=='RUN_CONFIRMED' for z in actuals)
    # Only a meaningful number of independent event cases may contribute a
    # small tie-breaker. No global/asset-universal win rate is invented.
    usable=n>=8
    return {'comparable_cases':n,'first_reaction_confirmed':r,'first_break_confirmed':b,
            'sample_sufficient':usable,'first_reaction_rate':r/n if usable else None,
            'first_break_rate':b/n if usable else None,
            'future_outcome_labels_are_not_assumed':True}


def forecast_zone_encounter(edge:Dict[str,Any],tape:List[Dict[str,Any]],
                            l2_events:List[Dict[str,Any]],odme_history:List[Dict[str,Any]],
                            fresh_odme:Dict[str,Any],structural:Dict[str,Any],
                            current_side:int,optional_context:Optional[Dict[str,Any]]=None) -> Dict[str,Any]:
    """Pre-zone forecast, never a trading gate or guaranteed price outcome."""
    now=_ms(edge.get('bar_time')) or 0
    if not now or _n(edge.get('close')) is None:return {}
    ledger=fp_event_ledger(edge,tape,l2_events)
    origin_at=_ms(structural.get('origin')) if structural.get('origin_valid') else None
    origin_side=1 if structural.get('recovery') else -1 if structural.get('decline') else 0
    # Source-confirmed FP history may indicate direction if the strategic
    # origin has not been verified, but a single stale event cannot confer it.
    confirmed=_dedupe_confirmed(ledger,origin_at,now)
    recent=confirmed[-5:]
    fav=sum(1 if ((e['side']=='SUPPLY')==(e['type']=='RUN_CONFIRMED')) else -1 for e in recent)
    # Actual present confirmed counter-control outranks an *older* origin;
    # origin still supplies the structural battlefield, not automatic vetoes.
    direction=current_side or origin_side or (1 if fav>=2 else -1 if fav<=-2 else 0)
    book=_current_book(edge,tape)
    roles=_roles(edge)
    route=_approach(edge,book,direction,roles)
    target=route.get('target')
    if not target:return {'available':False,'direction':direction,
                          'summary':'No current live FP or structural destination is mapped.',
                          'events':len(ledger)}
    style=_style(edge,book,ledger,direction,origin_at,target)
    odme=_odme_check(fresh_odme,odme_history,target,direction,now)
    cases=_prior_cases(ledger,tape,now,target,direction,style['name'])
    co=target.get('structural_overlap') or {}
    stage=target.get('stage') or 'BEFORE_TOUCH'
    # A current or earlier selected event might already be a touch; no pre-
    # encounter prediction is falsely advertised after a confirmed reaction.
    target_events=[e for e in ledger if e['tf']==str(target.get('tf')) and
                   e['side']==target['side'] and e['zone_id']==str(target.get('zone_id')) and
                   e['at']<=now]
    already_touched=any(e['type'] in ('DEFENCE_CONFIRMED','RUN_CONFIRMED','DEFENCE_AGGRESSIVE',
                                     'DEFENCE_ABSORPTION','BREACH_TEST') for e in target_events)
    if already_touched and stage=='BEFORE_TOUCH':stage='RETEST'
    if target.get('source') in ('STRUCTURAL','ROUTER_REFERENCE'):
        name=f"{_tf(target.get('tf'))} {target['side'].title()} {_fmt(target['low'])}–{_fmt(target['high'])}"
    else:name=_headings(target)
    # Two distinct forecasts. These are ordinal *hypotheses*, never fabricated
    # percentages; historical analogy can modify them only at sufficient n.
    attack=style['favour_events']
    defence=style['against_events']
    supported=style['support_ladder'] or style['supports_observed']>=2
    convergence=bool(co)
    if stage=='TESTING':
        reaction=('FIRST_REACTION_FAVOURED' if style['name']=='RECOVERY_AGAINST_PRIOR_CONTROL'
                  or convergence or defence>=2 else 'FIRST_REACTION_POSSIBLE')
    elif stage=='RETEST':
        reaction='RETEST_REACTION_POSSIBLE'
    elif convergence or defence>=2:
        reaction='FIRST_REACTION_FAVOURED'
    elif style['name']=='UNSUPPORTED_DISPLACEMENT':
        # A vertical advance can break Supply quickly and *still* suffer a
        # sharp first reaction. Never conflate those two forecast horizons.
        reaction='FIRST_REACTION_POSSIBLE'
    elif supported or (attack>=2 and defence==0):
        reaction='FIRST_REACTION_LESS_FAVOURED'
    else:reaction='FIRST_REACTION_POSSIBLE'
    if direction and (attack>=2 and defence==0 and not convergence):
        resolution='BREAK_FAVOURED'
    elif direction and supported and defence==0 and not convergence:
        resolution='BREAK_FAVOURED'
    elif (convergence or target.get('touch_cluster')) and defence>=1:
        resolution='HOLD_FAVOURED'
    elif defence>=2 and attack<=1:
        resolution='HOLD_FAVOURED'
    else:resolution='CONTESTED'
    # An unsupported move with repeated successful supply breaches is not
    # classified as bearish: it can be powerful displacement with pullback risk.
    if style['name']=='UNSUPPORTED_DISPLACEMENT' and resolution=='BREAK_FAVOURED':
        risk='BREAKOUT_WITHOUT_NEW_SUPPORT'
    elif style['name']=='UNPROVEN_APPROACH':risk='UNSUPPORTED_FIRST_TEST'
    elif convergence:risk='OPPOSING_STRUCTURAL_LOCATION'
    else:risk='ORDINARY_ZONE_REACTION'
    if odme.get('valid'):
        if odme.get('overlaps'):
            if reaction=='FIRST_REACTION_POSSIBLE' and not odme.get('supports'):
                reaction='FIRST_REACTION_FAVOURED'
            if resolution=='BREAK_FAVOURED' and odme.get('opposes'):
                resolution='CONTESTED'
        elif odme.get('opposes') and resolution=='BREAK_FAVOURED':
            resolution='CONTESTED'
        elif (odme.get('supports') and resolution=='CONTESTED' and
              attack>=2 and defence==0 and not convergence):
            resolution='BREAK_FAVOURED'
        # Options walls which retreat in the direction of a valid journey can
        # lower the expected resistance; only in conjunction with actual FP
        # evidence, never on an ODME wall shift alone.
        moved=odme.get('wall_change') or ''
        if target['side']=='SUPPLY' and direction>0 and moved=='wall shifted higher' and attack>=1 and not convergence:
            if resolution=='CONTESTED':resolution='BREAK_FAVOURED'
        if target['side']=='DEMAND' and direction<0 and moved=='wall shifted lower' and attack>=1 and not convergence:
            if resolution=='CONTESTED':resolution='BREAK_FAVOURED'
    # Distinct observed liquidity / Candle AURORA evidence can refine the
    # *forecast* if already source-verified by the existing freshness logic.
    # Neither optional source is ever a prerequisite, and neither is a new
    # execution authorisation or an OF double-count.
    optional=optional_context or {}
    liq=optional.get('liquidity') or {}
    aur=optional.get('aurora') or {}
    liq_aligned=(direction>0 and liq.get('accepted_buy') or
                 direction<0 and liq.get('accepted_sell'))
    liq_rejected=(direction>0 and liq.get('buy_sweep_rejected') or
                  direction<0 and liq.get('sell_sweep_reclaimed'))
    aur_relation=str(aur.get('relation') or '').upper()
    aur_aligned=aur_relation==('SUPPORTS_LONG' if direction>0 else 'SUPPORTS_SHORT')
    aur_opposed=aur_relation==('SUPPORTS_SHORT' if direction>0 else 'SUPPORTS_LONG')
    if liq_rejected:
        if reaction=='FIRST_REACTION_POSSIBLE':reaction='FIRST_REACTION_FAVOURED'
        if resolution=='BREAK_FAVOURED':resolution='CONTESTED'
    if liq_aligned and attack>=1 and not convergence:
        if resolution=='CONTESTED' and not odme.get('opposes'):
            resolution='BREAK_FAVOURED'
    if aur_opposed and attack<=1 and resolution=='BREAK_FAVOURED':
        resolution='CONTESTED'
    if aur_aligned and attack>=2 and defence==0 and not convergence:
        if resolution=='CONTESTED' and not odme.get('opposes'):
            resolution='BREAK_FAVOURED'
    if cases.get('sample_sufficient'):
        if cases['first_reaction_rate']>=0.7 and reaction=='FIRST_REACTION_POSSIBLE':
            reaction='FIRST_REACTION_FAVOURED'
        elif cases['first_break_rate']>=0.7 and resolution=='CONTESTED' and not convergence:
            resolution='BREAK_FAVOURED'
    # Explicit interpretation of both scenarios with a single actionable main
    # takeaway; avoids contradictory 'high confidence break' + 'high rejection'.
    if resolution=='BREAK_FAVOURED':
        primary=f'{name} looks vulnerable to a break'
        if reaction=='FIRST_REACTION_FAVOURED':primary+=', although an initial reaction is favoured'
        elif reaction!='FIRST_REACTION_LESS_FAVOURED':primary+=', though a first-touch reaction remains possible'
    elif resolution=='HOLD_FAVOURED':
        primary=f'{name} is more likely to produce a meaningful defence'
    elif reaction=='FIRST_REACTION_FAVOURED':
        primary=f'{name} is likely to produce an initial reaction; a lasting reversal is unproven'
    else:primary=f'{name} may react on contact; a durable hold versus break is unresolved'
    reasons=[]
    if origin_side:reasons.append('verified structural origin remains the journey anchor')
    elif direction:reasons.append('direction inferred from recorded FP outcomes; strategic origin unverified')
    if attack:reasons.append(f'{attack} distinct aligned FP results')
    if defence:reasons.append(f'{defence} opposing FP results')
    if supported:reasons.append(f'{style["supports_observed"]} observed supporting FP zones'+(' forming a staircase' if style['support_ladder'] else ''))
    elif style['name']=='UNSUPPORTED_DISPLACEMENT':reasons.append('multiple confirmed breaks without newly observed supporting FP zones: sharp pullback risk even if breakout continues')
    elif style['name']=='UNPROVEN_APPROACH':reasons.append('no fresh supporting FP zone observed; a reaction may reach deeper support')
    elif style['name']=='RECOVERY_AGAINST_PRIOR_CONTROL':
        reasons.append(f'{style["prior_regime_opposing_results"]} confirmed opposing FP results PRECEDED this structural interaction; post-origin support is not yet established')
    if convergence:
        marker='active structural' if co.get('source')=='STRUCTURAL' else 'router-mapped structural'
        reasons.append(f'barrier overlaps {_tf(co.get("tf"))} {marker} {target["side"].title()}')
    matches=target.get('structural_matches') or []
    if len(matches)>=2:
        frames=', '.join(dict.fromkeys(_tf(x['tf']) for x in matches))
        reasons.append(f'same-level structural references appear on {frames}')
    if style['failed_opposing_holds']:reasons.append(f'{style["failed_opposing_holds"]} prior opposing defence(s) subsequently broke')
    barriers=target.get('route_barrier_count')
    if barriers and barriers>1:
        reasons.append(f'{barriers} distinct observed FP barrier clusters lie in the path')
    if target.get('touch_cluster'):
        reasons.append('next FP barrier overlaps another active FP zone')
    other=route.get('nearest_demand') if target['side']=='SUPPLY' else route.get('nearest_supply')
    if other:
        reasons.append(f'nearest observed opposite FP {other["side"].title()} {_fmt(other["low"])}–{_fmt(other["high"])}')
    if book['truncated']:reasons.append('live FP hurdle book is truncated; absent zones cannot be assumed nonexistent')
    if cases['sample_sufficient']:
        reasons.append(f'{cases["comparable_cases"]} observed previous barrier outcomes provide limited historical context')
    else:
        reasons.append(f'only {cases["comparable_cases"]} independently comparable pre-contact cases; no calibrated probability')
    if odme.get('valid'):reasons.append(odme['why'])
    if liq_aligned:reasons.append('fresh source-verified liquidity runs favour the journey')
    if liq_rejected:reasons.append('fresh liquidity sweep rejection warns of a first-touch reaction')
    if aur_aligned:reasons.append('fresh optional Candle AURORA agrees')
    elif aur_opposed:reasons.append('fresh optional Candle AURORA disagrees')
    reaction_text={'FIRST_REACTION_FAVOURED':'First contact: a tactical reaction is favoured.',
                   'FIRST_REACTION_LESS_FAVOURED':'First contact: immediate rejection is less favoured.',
                   'RETEST_REACTION_POSSIBLE':'Retest: an earlier interaction is recorded; treat the zone as tested.',
                   'FIRST_REACTION_POSSIBLE':'First contact: a tactical reaction is possible.'}[reaction]
    if style['name']=='UNSUPPORTED_DISPLACEMENT':
        execution='No fresh intermediate FP support is visible in the returned book; do not chase directly into the barrier. A sharp pullback is possible.'
    elif style['name']=='RECOVERY_AGAINST_PRIOR_CONTROL':
        execution='The new recovery has not yet overcome the previously recorded opposing FP regime. Treat an initial rejection as credible, not a proven trend reversal.'
    elif convergence:execution='Significant opposing structural location increases the need for a conservative Renko entry.'
    elif resolution=='BREAK_FAVOURED':execution='Continuation is favoured, but use a fresh qualified Renko setup rather than assuming the break.'
    else:execution='A confirmed tactical defence can be traded with a tight valid Renko stop and conservative nearby target.'
    return {'available':True,'at':now,'name':name,'target':target,'route':route,
            'style':style,'first_reaction':reaction,'resolution':resolution,'risk':risk,
            'direction':direction,'stage':stage,'primary':primary+'.',
            'reaction_text':reaction_text,'execution':execution,
            'why':'; '.join(reasons)+'.','odme':odme,'historical_cases':cases,
            'confirmed_alert_events':sum(x.get('source')=='LEVEL2' for x in ledger),
            'all_events':len(ledger),'source_version':'ZONE_ENCOUNTER_FORECAST_20261124'}
