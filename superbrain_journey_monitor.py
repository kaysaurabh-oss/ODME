"""Display-only journey quality and immutable pre-arrival forecast journal.

No new trade signal, order, campaign or Telegram authority. Forecasts are
qualitative hypotheses, not calibrated probabilities. Sheet ledger records
only genuine live forecasts and outcomes; no raw history duplication.
"""
from __future__ import annotations
import json
from typing import Any
from superbrain_zone_forecast import (_n, _ms, _roles, _current_book, _fmt,
    _tf, fp_event_ledger, forecast_zone_encounter)

VERSION='JOURNEY_MONITOR_20261124_V1'
RULEBOOK_VERSION='SB_EDGE_ODME_20261022_V2'


def _key(z):
    return '|'.join(str(z.get(k) or '') for k in ('tf','side','zone_id'))


def _name(z):
    if not z:return 'no verified mapped barrier'
    kind='FP ' if z.get('source') in ('LIVE_BOOK','SELECTED_FP') else ''
    return f'{_tf(z.get("tf")) or "current-TF"} {kind}{z["side"].lower()} {_fmt(z["low"])}–{_fmt(z["high"])}'


def _matching(e,z):
    if _key(e)!=_key(z):return False
    # IDs can be reused after a source reset; require overlapping original levels.
    lo,hi=_n(e.get('low')),_n(e.get('high'))
    return lo is not None and hi is not None and max(lo,z['low'])<=min(hi,z['high'])


def _rows(edge,tape,asof):
    symbol=str(edge.get('instrument') or '').upper()
    now=_ms(edge.get('bar_time')) or 0
    out={}
    for r in list(tape or [])+[dict(edge,bar_ms=now)]:
        if r.get('synthetic_price_only') or str(r.get('instrument') or '').upper()!=symbol:continue
        t=_ms(r.get('bar_ms'));p=_n(r.get('close'));known=_ms(r.get('captured_at')) or t
        if not t or t>now or not known or known>asof or p is None:continue
        k=(str(r.get('tf')),t)
        # A sparse duplicate must not replace the recorded hurdle book.
        if k not in out or r.get('fp_hurdles_json') or r.get('raw_json'):out[k]=dict(r,bar_ms=t,close=p)
    return sorted(out.values(),key=lambda r:r['bar_ms'])


def _continuous(rows,tf,end=None):
    n=_n(tf)
    if not n or n<=0:return []
    rows=[r for r in rows if str(r.get('tf'))==str(tf) and (end is None or r['bar_ms']<=end)]
    run=[]
    for r in rows:
        if run and r['bar_ms']-run[-1]['bar_ms']!=int(n*60000):run=[]
        run.append(r)
    return run


def _targets(edge,tape):
    p=_n(edge.get('close'))
    book=_current_book(edge,tape)
    zones=book['zones']+[dict(z,zone_id=z['role']) for z in _roles(edge) if z['source']=='STRUCTURAL']
    result={}
    for side,d in [('SUPPLY',1),('DEMAND',-1)]:
        zs=[dict(z,distance=max(0,z['low']-p if d>0 else p-z['high'])) for z in zones
            if z['side']==side and (z['high']>=p if d>0 else z['low']<=p)]
        result[d]=min(zs,key=lambda z:(z['distance'],z['source']=='STRUCTURAL',z['high']-z['low'])) if zs else None
    return result,book


def _encounter(z,events,rows,now):
    """Separate actual encounters from repeated event snapshots; no inferred RUN."""
    ev=[e for e in events if _matching(e,z) and e['at']<=now]
    ev.sort(key=lambda e:e['at'])
    # Every confirmed outcome is a separate episode; probes before/after are preserved.
    holds=[e for e in ev if e['type']=='DEFENCE_CONFIRMED']
    breaks=[e for e in ev if e['type']=='RUN_CONFIRMED']
    latest=ev[-1] if ev else None
    prior=holds[-1] if holds else None
    tf=_n(z.get('tf')) or 0; direction=1 if z['side']=='DEMAND' else -1
    boundary=z['high'] if direction>0 else z['low']
    progress=None;peak=None;retention=None
    if prior and tf:
        seg=_continuous([r for r in rows if r['bar_ms']>=prior['at']],z['tf'])
        if seg and seg[0]['bar_ms']==prior['at'] and seg[-1]['bar_ms']==now:
            progress=(seg[-1]['close']-boundary)*direction
            peak=max((r['close']-boundary)*direction for r in seg)
            if peak>0:retention=progress/peak
    return {'latest':latest,'holds':len(holds),'breaks':len(breaks),'prior_hold':prior,
            'progress':progress,'peak':peak,'retention':retention,'events':ev}


def _quality(edge,z,rows,events,forecast,roles,now):
    d=1 if z['side']=='SUPPLY' else -1
    tf=_n(edge.get('tf')) or 0
    recent=_continuous(rows,edge.get('tf'),now)[-4:]
    near=z['distance']==0
    toward=False;slow=False;extension=None
    if len(recent)>=4 and recent[-1]['bar_ms']==now:
        changes=[d*(b['close']-a['close']) for a,b in zip(recent,recent[1:])]
        net=sum(changes);path=sum(abs(v) for v in changes)
        toward=net>0
        # Descriptive comparison, not a calibrated signal or OF threshold.
        slow=toward and (changes[-1]<=0 or changes[-1]<changes[0] or net<path)
        extension=path
    enc=_encounter(z,events,rows,now)
    latest=enc['latest']; latest_at=latest['at'] if latest else 0
    fresh=bool(tf and latest and 0<=now-latest_at<=3*tf*60000 and str(z.get('tf'))==str(edge.get('tf')))
    structural=[r for r in roles if r['side']==z['side'] and max(z['low'],r['low'])<=min(z['high'],r['high'])]
    style=forecast.get('style') or {}; support=bool(style.get('support_ladder'))
    opposed=style.get('against_events',0)>0
    pressure=enc['retention'] is not None and enc['retention']<=0.25 and enc['peak']>0
    notes=[]
    if structural:notes.append('The barrier overlaps '+('the active '+structural[0]['role'] if structural[0]['source']=='STRUCTURAL' else 'a mapped higher-timeframe reference')+'.')
    if slow:notes.append('The approach is losing clean price progress.')
    elif toward:notes.append('The approach is still making price progress.')
    if support:notes.append('Observed supporting FP zones form a continuation route; they are potential obstacles to a deep countertrend reaction.')
    elif style.get('name')=='UNSUPPORTED_DISPLACEMENT':notes.append('The advance has broken barriers without a newly mapped supporting FP staircase; a deeper counter-move is possible, not established.')
    if pressure:notes.append('The previous confirmed defence has surrendered most of its recorded price progress.')
    elif enc['holds']:notes.append('The zone has defended before; that does not prove the next defence.')
    if opposed:notes.append('The recorded structural journey includes opposing FP outcomes.')
    opt=forecast.get('odme') or {}
    if opt.get('valid') and opt.get('overlaps'):notes.append('Fresh matching-expiry options positioning marks the same barrier.')
    if opt.get('valid') and opt.get('opposes'):notes.append('Fresh options positioning opposes the approach.')
    elif opt.get('valid') and opt.get('supports'):notes.append('Fresh options positioning supports the approach.')
    # Use the existing source-verified zone forecast for context, not a new
    # numeric weighting model. Recent pressure conflicts override a favoured hold.
    first=forecast.get('first_reaction',''); resolution=forecast.get('resolution','CONTESTED')
    expected='UNCERTAIN'
    if pressure:expected='PENETRATION_WATCH'
    elif first=='FIRST_REACTION_FAVOURED' and (structural or slow or opposed):expected='REACTION_WATCH'
    elif resolution=='BREAK_FAVOURED' and support and not structural and not slow:expected='PENETRATION_WATCH'
    elif structural and slow:expected='REACTION_WATCH'
    if fresh and latest['type'] in ('DEFENCE_AGGRESSIVE','DEFENCE_ABSORPTION'):
        state='EARLY_DEFENCE'
    elif fresh and latest['type']=='BREACH_TEST':state='BREACH_TEST'
    elif fresh and latest['type']=='DEFENCE_CONFIRMED':state='DEFENCE_CONFIRMED'
    elif fresh and latest['type']=='RUN_CONFIRMED':state='RUN_CONFIRMED'
    else:state='APPROACH' if toward and not near else 'TESTING' if near else 'MAPPED'
    # A distance-based approach watch is bounded by actual recent price travel.
    approaching=bool(toward and z['distance']>0 and extension and z['distance']<=extension and not fresh)
    return dict(zone=z,expected=expected,state=state,approaching=approaching,notes=notes,
                encounter=enc,forecast=forecast,pressure=pressure,asof=now)


LEDGER_TAB='SB_JOURNEY_LEDGER'
LEDGER_HEADERS=['event_key','instrument','record_type','zone_key','forecast_bar','observed_bar','created_ms','version','payload_json']


def ledger_row(symbol, kind, zone, bar, observed, created, version, payload):
    # Stable identity; first issued forecast is immutable even on rerenders.
    import hashlib
    identity=json.dumps([symbol,kind,zone,int(bar),int(observed),version],separators=(',',':'))
    key=hashlib.sha256(identity.encode()).hexdigest()
    encoded=json.dumps(payload,default=str,separators=(',',':'))
    if len(encoded)>49000:raise ValueError('Journey ledger payload exceeds cell limit')
    return [key,symbol,kind,zone,int(bar),int(observed),int(created),version,encoded]


class JourneyJournal:
    """Append-only Sheet forecast ledger. Existing tapes remain raw history.

    No tab creation, edits, orders, or SQLite dependency in the live app.
    Duplicate physical rows (e.g. ambiguous network retry/concurrent sessions)
    are read as one logical record, preserving the earliest recorded forecast.
    """
    def __init__(self,store):
        self.ws=store.sheet.worksheet(LEDGER_TAB)
        if self.ws.row_values(1)!=LEDGER_HEADERS:
            raise ValueError('Journey ledger schema mismatch')
        self.records=None

    def load(self):
        if self.records is None:
            rows=self.ws.get_all_values()[1:]
            records={}
            for r in rows:
                if not r or not r[0]:continue
                if len(r)!=len(LEDGER_HEADERS):raise ValueError('Incomplete journey ledger record')
                d=dict(zip(LEDGER_HEADERS,r))
                for f in ('forecast_bar','observed_bar','created_ms'):d[f]=int(d[f])
                d['payload']=json.loads(d['payload_json'])
                k=d['event_key']
                if k not in records or d['created_ms']<records[k]['created_ms']:records[k]=d
            self.records=records
        return self.records

    def append(self,row):
        records=self.load()
        if row[0] in records:return
        self.ws.append_row(row,value_input_option='RAW')
        d=dict(zip(LEDGER_HEADERS,row));d['payload']=json.loads(d['payload_json'])
        records[row[0]]=d

    def forecasts(self,symbol,bar,asof):
        records=(d for d in self.load().values() if d['instrument']==symbol and d['record_type']=='FORECAST' and d['version']==VERSION and d['forecast_bar']<bar and d['created_ms']<asof)
        return sorted(records,key=lambda d:d['created_ms'],reverse=True)

    def previous(self,symbol,z,bar,asof):
        for d in self.forecasts(symbol,bar,asof):
            f=d['payload']
            if d['zone_key']==_key(z) and _matching(f['zone'],z):return f
        return None

    def recent(self,symbol,bar,asof):
        result=[]
        for d in self.forecasts(symbol,bar,asof):
            f=d['payload'];tf=_n(f['zone'].get('tf'))
            if not tf or bar-f['bar']>8*tf*60000:continue
            if not any(_matching(x['zone'],f['zone']) for x in result):result.append(f)
        return result

    def save_reconciliation(self,symbol,f,now,note,asof=None):
        if not note:return
        payload={'forecast':f,'observed_bar':now,'note':note}
        self.append(ledger_row(symbol,'RECONCILIATION',_key(f['zone']),f['bar'],now,asof or now,VERSION,payload))

    def record(self,symbol,z,bar,asof,q):
        f={'zone':z,'bar':bar,'created':asof,'expected':q['expected'],'notes':q['notes'],'opposing_destination':q.get('opposing_destination'),'resolution':(q.get('forecast') or {}).get('resolution'),'rulebook':RULEBOOK_VERSION,'version':VERSION}
        self.append(ledger_row(symbol,'FORECAST',_key(z),bar,0,asof,VERSION,f))


def reconcile(previous,q,now):
    if not previous:return ''
    z=q['zone'];tf=_n(z.get('tf'))
    if not tf or now-previous['bar']>8*tf*60000:return ''
    # An event must occur after forecast was issued, not just after its bar.
    actual=[e for e in q['encounter']['events'] if e['at']>previous['created']]
    if not actual:return ''
    last=actual[-1];typ=last['type'];expected=previous['expected']
    if expected=='REACTION_WATCH':
        if typ=='RUN_CONFIRMED':
            if any(e['type']=='DEFENCE_CONFIRMED' for e in actual[:-1]):return 'The earlier reaction occurred, but EDGE now confirms a subsequent break; the reaction and eventual resolution differ.'
            return 'The earlier reaction watch has failed: EDGE confirms a break.'
        if q['pressure']:
            if any(e['type']=='DEFENCE_CONFIRMED' for e in actual):return 'The earlier reaction materialised but has lost most of its price progress; downgrade the recovery.'
            return 'The reaction watch remains unconfirmed and the prior defence has lost progress.'
        if typ=='DEFENCE_CONFIRMED':return 'The earlier reaction watch is supported by confirmed defence; further travel still needs confirmation.'
        if typ in ('DEFENCE_AGGRESSIVE','DEFENCE_ABSORPTION'):return 'The anticipated reaction is beginning, but the hold and its depth are not yet confirmed.'
    if expected=='PENETRATION_WATCH':
        if typ=='RUN_CONFIRMED':return 'The earlier penetration watch is supported by a confirmed break.'
        if typ=='DEFENCE_CONFIRMED':return 'The earlier penetration watch is challenged by confirmed defence; reassess follow-through.'
        if typ=='BREACH_TEST':return 'The earlier vulnerability watch is supported by a breach test, not yet a confirmed break.'
    return ''


def enhance_outlook(outlook,edge,tape,optional,odme,l2,live_odme,*,journal=None,asof=None):
    """Overlay plain-English route intelligence; preserve all decision fields."""
    if not outlook or outlook.get('label')=='UNAVAILABLE':return outlook
    now=_ms(edge.get('bar_time'));spot=_n(edge.get('close'));asof=asof or now
    if not now or spot is None or not asof or asof<now:return outlook
    symbol=str(edge.get('instrument') or '').upper()
    if not symbol:return outlook
    decision=outlook.get('journey_evidence') or {};ctx=decision.get('context') or {}
    memory_ok=False
    if journal:
        try:
            # Raw observations are already in the continuous Sheet tapes.
            # The journal stores only forecasts/outcomes, never duplicate bars.
            journal.load()
            memory_ok=True
        except Exception:journal=None
    rows=_rows(edge,tape,asof)
    odme=[r for r in odme if (_ms(r.get('ts')) or asof+1)<=asof]
    # Actual visibility cutoff; only retained observations known by this scan.
    l2=[e for e in l2 if (_ms(e.get('known_at')) or asof+1)<=asof]
    observed=[r for r in tape if not r.get('synthetic_price_only') and
              (_ms(r.get('captured_at')) or _ms(r.get('bar_ms')) or asof+1)<=asof
              and (_ms(r.get('bar_ms')) or now+1)<=now]
    events=fp_event_ledger(edge,observed,l2,asof=asof)
    events=[e for e in events if e['at']<=now]
    targets,book=_targets(edge,rows);roles=_roles(edge);paths={}
    # A breach test may close just beyond its zone. Keep that observed active
    # contested FP as the local interaction rather than jumping to the next FP.
    for z in book['zones']:
        e=_encounter(z,events,rows,now)['latest'];tf=_n(z.get('tf')) or 0
        if e and tf and str(z.get('tf'))==str(edge.get('tf')) and 0<=now-e['at']<=tf*60000 and e['type'] in ('BREACH_TEST','DEFENCE_AGGRESSIVE','DEFENCE_ABSORPTION','DEFENCE_CONFIRMED'):
            d=1 if z['side']=='SUPPLY' else -1
            curr=targets.get(d)
            if not curr or _key(curr)!=_key(z):
                pz=_encounter(curr,events,rows,now)['latest'] if curr else None
                if not pz or e['at']>pz['at']:
                    targets[d]=dict(z,distance=max(0,z['low']-spot if d>0 else spot-z['high']))
    for d,z in targets.items():
        if not z:continue
        f=forecast_zone_encounter(edge,rows,l2,odme,live_odme or {},ctx,d,decision,target_override=z)
        paths[d]=_quality(edge,z,rows,events,f,roles,now)
        q=paths[d];liq=decision.get('liquidity') or {};aur=decision.get('aurora') or {}
        if (d>0 and liq.get('buy_sweep_rejected')) or (d<0 and liq.get('sell_sweep_reclaimed')):
            q['notes'].append('Fresh liquidity sweep rejection makes the approach vulnerable.')
        elif (d>0 and liq.get('accepted_buy')) or (d<0 and liq.get('accepted_sell')):
            q['notes'].append('Fresh accepted liquidity runs support continuation.')
        relation=aur.get('relation')
        if relation in ('SUPPORTS_LONG','SUPPORTS_SHORT'):
            q['notes'].append('Fresh Candle AURORA '+('supports' if relation==('SUPPORTS_LONG' if d>0 else 'SUPPORTS_SHORT') else 'opposes')+' the approach.')
    for d,q in paths.items():
        q['opposing_destination']=(paths.get(-d) or {}).get('zone')
    side=outlook.get('side') or 0
    if not paths and not events:return outlook
    # The currently tested FP is not necessarily the next destination of the
    # strategic route. A vulnerable defence deserves explicit local commentary.
    tested=[q for q in paths.values() if q['state'] in ('EARLY_DEFENCE','BREACH_TEST','DEFENCE_CONFIRMED')]
    tested.sort(key=lambda q:q['encounter']['latest']['at'],reverse=True)
    active=tested[0] if tested else paths.get(side)
    if not active:
        approaching=[q for q in paths.values() if q['approaching']]
        active=min(approaching,key=lambda q:q['zone']['distance']) if approaching else None
    # A genuine confirmed RUN can remove the failed FP from the current book.
    # Retain its original levels as a retest candidate, never as a live zone.
    tf=_n(edge.get('tf')) or 0
    fresh_breaks=[e for e in events if e['type']=='RUN_CONFIRMED' and str(e['tf'])==str(edge.get('tf'))
                  and e.get('low') is not None and e.get('high') is not None
                  and tf and 0<=now-e['at']<=3*tf*60000
                  and (spot<e['low'] if e['side']=='DEMAND' else spot>e['high'])]
    if fresh_breaks and (not tested or fresh_breaks[-1]['at']>tested[0]['encounter']['latest']['at']):
        br=fresh_breaks[-1]; bd=-1 if br['side']=='DEMAND' else 1
        failed=dict(br,source='LIVE_BOOK'); dest=paths.get(bd); alt=paths.get(-bd)
        failed_name=_name(failed); dest_name=_name(dest['zone']) if dest else 'no farther active barrier is mapped'
        previous=None; memory_note=''
        if journal:
            try:previous=journal.previous(symbol,failed,now,asof)
            except Exception:memory_ok=False
        if previous and br['at']>previous['created'] and now-previous['bar']<=8*tf*60000:
            if previous['expected']=='REACTION_WATCH':
                held=any(e['type']=='DEFENCE_CONFIRMED' and _matching(e,failed) and previous['created']<e['at']<br['at'] for e in events)
                memory_note=(' The earlier reaction occurred, but the zone has now broken.' if held else ' The earlier reaction watch has been invalidated by the confirmed break.')
            elif previous['expected']=='PENETRATION_WATCH':memory_note=' The earlier penetration watch is confirmed by EDGE.'
        if journal and previous and memory_note:
            try:journal.save_reconciliation(symbol,previous,now,memory_note.strip(),asof)
            except Exception:memory_ok=False
        first=outlook['commentary'].split('\n\n')[0]
        main=f'**Most likely:** Confirmed break of {failed_name} favours local {"bearish" if bd<0 else "bullish"} continuation towards {dest_name}.'+memory_note
        risk=f'**Alternative route:** Reclaim of the broken zone would challenge continuation.'+(f' Opposing mapped barrier: {_name(alt["zone"])}.' if alt else '')
        renko=f'**Renko AURORA:** Prefer a qualifying new FP {"supply" if bd<0 else "demand"} or retest of {failed_name} from {"below" if bd<0 else "above"}; wait for a verified response. Do not chase. Existing entry and campaign rules apply.'
        why='EDGE has confirmed the break; the previous zone is a retest candidate, not confirmed new support or resistance. The wider structural origin remains a separate question.'
        return dict(outlook,commentary='\n\n'.join([first,main,risk,renko]),why_commentary=why,journey_monitor={'version':VERSION,'paths':paths,'reconciliation':memory_note,'memory_ok':memory_ok,'calibration':'EXPERIMENTAL'})
    if not active:return outlook
    z=active['zone'];d=1 if z['side']=='SUPPLY' else -1;reaction=-d
    other=paths.get(-d);other_name=_name(other['zone']) if other else 'the next verified opposing barrier (not currently mapped)'
    name=_name(z);prior=None;recon=''
    if journal:
        try:
            prior=journal.previous(symbol,z,now,asof);recon=reconcile(prior,active,now)
            if active['approaching'] and active['state']=='APPROACH':journal.record(symbol,z,now,asof,active)
        except Exception:memory_ok=False
    # Reconcile prior encounters even when the next mapped destination has
    # changed. Only actual source events and contiguous closes may add progress.
    if journal:
        try:
            for f in journal.recent(symbol,now,asof):
                old=f['zone'];enc=_encounter(old,events,rows,now)
                after=[e for e in enc['events'] if e['at']>f['created']]
                if not after:continue
                probe={'zone':old,'encounter':enc,'pressure':enc['retention'] is not None and enc['retention']<=0.25}
                message=reconcile(f,probe,now)
                held=[e for e in after if e['type']=='DEFENCE_CONFIRMED']
                if f['expected']=='REACTION_WATCH' and held and after[-1]['type']!='RUN_CONFIRMED' and not probe['pressure']:
                    d0=1 if old['side']=='DEMAND' else -1
                    seg=_continuous([r for r in rows if r['bar_ms']>=held[-1]['at']],old['tf'])
                    if len(seg)>=2 and seg[0]['bar_ms']==held[-1]['at'] and seg[-1]['bar_ms']==now and d0*(seg[-1]['close']-seg[0]['close'])>0:
                        message='The earlier reaction at '+_name(old)+' is making further recorded price progress.'
                        target=f.get('opposing_destination')
                        if target:
                            level=target['low'] if d0>0 else target['high']
                            if any(d0*(r['close']-level)>=0 for r in seg):message+=' Its first mapped destination was reached on a recorded close.'
                            else:message+=' The earlier mapped destination was '+_name(target)+'; arrival is not yet established.'
                if message:
                    journal.save_reconciliation(symbol,f,now,message,asof)
                    if not recon or _matching(old,z):recon=message
        except Exception:memory_ok=False
    state=active['state'];expected=active['expected'];pressure=active['pressure'];notes=active['notes']
    # Preserve structural statement and verified live options sentence, but
    # replace verbose duplicated route/forecast sections with a single route.
    first=outlook['commentary'].split('\n\n')[0]
    label=outlook['label'];renko=outlook['commentary'].split('\n\n')[-1]
    if pressure and state in ('EARLY_DEFENCE','BREACH_TEST','DEFENCE_CONFIRMED'):
        label='BEARISH PRESSURE AT VULNERABLE DEMAND' if d<0 else 'BULLISH PRESSURE AT VULNERABLE SUPPLY'
        first=f'The previous defence of {name} has lost most of its recorded progress. The wider structural journey is not automatically reversed.'
        if live_odme:
            opt=active['forecast'].get('odme') or {}
            if opt.get('supports'):first+=' Fresh options positioning supports the pressure.'
            elif opt.get('opposes'):first+=' Fresh options positioning opposes the pressure.'
        main=f'**Most likely:** Renewed pressure on {name}; a break remains unconfirmed.'
        renko='**Renko AURORA:** Wait for a credible renewed defence or a verified break and qualifying pullback. Do not chase into the barrier.'
    elif state in ('EARLY_DEFENCE','DEFENCE_CONFIRMED'):
        first=f'{name} is showing '+('confirmed defence.' if state=='DEFENCE_CONFIRMED' else 'an early defence; the hold is not yet confirmed.')+' '+first
        main=f'**Most likely:** A {"bullish" if reaction>0 else "bearish"} reaction is possible towards {other_name}; sustained progress must confirm its depth.'
        if state=='DEFENCE_CONFIRMED' and not pressure:main=f'**Most likely:** Watch the confirmed reaction towards {other_name}; follow-through determines whether it extends beyond a tactical move.'
        renko=f'**Renko AURORA:** Watch {"bullish" if reaction>0 else "bearish"} confirmation for a tactical reaction; only act if the locked entry gates qualify. Manage existing exposure first.'
    elif state=='BREACH_TEST':
        main=f'**Most likely:** {name} is under a breach test. Await accepted break or effective defence; do not treat the test as a RUN.'
        renko='**Renko AURORA:** Wait for verified resolution; after a break prefer a qualifying new FP zone or retest.'
    else:
        if expected=='REACTION_WATCH':
            main=f'**Most likely:** Watch for a {"bearish" if reaction<0 else "bullish"} reaction at {name}. If it develops, the first mapped opposing barrier is {other_name}.'
            if active['approaching']:renko=f'**Renko AURORA:** Prepare for a possible {"bearish" if reaction<0 else "bullish"} countertrade at the zone; await qualifying confirmation. Do not chase into it.'
        elif expected=='PENETRATION_WATCH':
            main=f'**Most likely:** {name} appears vulnerable; continuation requires a verified break. A first reaction is still possible.'
        else:
            main=f'**Most likely:** The next mapped barrier is {name}; reaction versus penetration remains unresolved. Watch how price approaches it.'
            if active['approaching']:renko='**Renko AURORA:** Wait for a qualifying response at the approaching barrier; do not assume either rejection or breakout.'
    # Actual next same-direction barrier excludes the currently contested zone.
    allzones=book['zones']+[dict(r,zone_id=r['role']) for r in roles if r['source']=='STRUCTURAL']
    beyond=[r for r in allzones if r['side']==z['side'] and (r['low']>z['high'] if d>0 else r['high']<z['low'])]
    nxt=min(beyond,key=lambda r:r['low']-z['high'] if d>0 else z['low']-r['high']) if beyond else None
    if state in ('EARLY_DEFENCE','BREACH_TEST','DEFENCE_CONFIRMED') or expected=='PENETRATION_WATCH':
        risk=f'**Alternative route:** A verified break of {name} opens the route towards {_name(nxt)}. Prefer a new FP {"demand" if d>0 else "supply"} or qualifying retest of the broken zone; no assumed role reversal.' if nxt else f'**Alternative route:** A verified break of {name} favours continuation, but no farther active barrier is mapped. Prefer a new FP zone or qualifying retest.'
        if pressure: risk+=f' Effective renewed defence could instead move towards {other_name}.'
    else:risk=f'**Alternative route:** A confirmed break of {name} could extend towards {_name(nxt)}.' if nxt else f'**Alternative route:** A confirmed break of {name} could extend the journey; no farther verified level is mapped.'
    if expected in ('PENETRATION_WATCH','UNCERTAIN') or state=='BREACH_TEST':risk+=f' Effective rejection could instead move towards {other_name}.'
    if recon:main+=' '+recon
    if expected=='REACTION_WATCH' and other and not pressure:
        main+=' A deeper counter-move needs a verified break of that opposing barrier.'
    why=' '.join(notes[:4]) or 'The available recorded journey does not yet settle the next encounter.'
    # Keep original source-grounded structural explanation, not raw counts.
    original=str(outlook.get('why_commentary') or '').split('\n\n')[0]
    if original:why=original+' '+why
    if book['truncated']:why+=' The current FP map is incomplete.'
    if not memory_ok:why+=' No earlier saved forecast is assumed.'
    r=dict(outlook,label=label,commentary='\n\n'.join([first,main,risk,renko]),why_commentary=why,
           journey_monitor={'version':VERSION,'paths':paths,'reconciliation':recon,'memory_ok':memory_ok,'calibration':'EXPERIMENTAL'})
    # Display only: side, trade_authority, renko_preference and entry logic untouched.
    return r
