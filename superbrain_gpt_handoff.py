"""Read-only analysis handoff; not a scan worker, trading rule or order API."""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from urllib.parse import urlencode

VERSION = 'SB_GPT_COMMENTARY_20261124_V1'
TAB = 'SB_GPT_SCAN'
HEADERS = ['scan_id','instrument','completed_at_utc','rulebook_version','commentary_version',
           'expiry','edge_live','odme_live','odme_scan_at_utc','status','edge_context_json',
           'tv_rows_json','odme_json','entry_matrix_json','campaign_json','context_json']
MAX_CELL = 45000


def _plain(x):
    if x is None or isinstance(x, (str, bool, int)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _plain(v) for k,v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if type(x).__name__ in {'NAType','NaTType'}:
        return None
    if hasattr(x, 'item'):
        return _plain(x.item())
    raise TypeError('Unsupported handoff evidence type: '+type(x).__name__)


def json_cell(value):
    s=json.dumps(_plain(value),ensure_ascii=False,allow_nan=False,separators=(',',':'))
    if len(s)>MAX_CELL:
        raise ValueError('Handoff evidence exceeds safe Sheet cell size; no incomplete receipt published.')
    return s


def build_row(packet, *, rulebook_version, edge, edge_live, odme_summary, assessment,
              level_map, entries, campaigns, context, strikes):
    """Preserve exact scan identity, actual live ODME result even if not persisted.

    An incomplete ODME scan is marked blocked, without asserting old saved positioning.
    """
    if packet.get('_scan_failed') or not packet.get('_gpt_scan_id') or not packet.get('_gpt_completed_at'):
        raise ValueError('No successful scan receipt.')
    symbol=str(packet.get('instrument') or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_:-]+',symbol):
        raise ValueError('Invalid instrument')
    mapping=packet.get('mapping') or {}
    option_enabled=bool(mapping.get('odme_scan_enabled'))
    expiry=str(mapping.get('selected_expiry') or '') if option_enabled else ''
    outcome=packet.get('odme_outcome') or {}
    meta=outcome.get('meta') or {}
    live=outcome.get('result') or {}
    notes=str(meta.get('notes') or '')
    unfetched=re.search(r'\bunfetched=(\d+)\b',notes)
    odme_time=str(meta.get('ts') or live.get('ts') or '') if option_enabled else ''
    odme_live=bool(option_enabled and packet.get('odme_live') and odme_time and expiry
                   and str(meta.get('expiry') or '')==expiry
                   and str(meta.get('instrument') or '').upper()==symbol
                   and int(outcome.get('usable') or 0)>=10 and int(outcome.get('contracts') or 0)>0
                   and float(outcome.get('future_ltp') or 0)>0
                   and unfetched is not None and int(unfetched.group(1))==0
                   and not live.get('error') and strikes)
    session=context.get('session') or {}
    edge_live=bool(edge_live and session.get('is_open') is True)
    status='READY' if edge_live and (odme_live or not option_enabled) else 'REVIEW_ONLY'
    # No stale summary masquerading as current ODME; carry actual partial results
    # explicitly labelled as unverified when the original scan returned them.
    odme={'verified':odme_live,'scan_meta':meta if option_enabled else {},
          'usable_oi_count':outcome.get('usable'),'contracts':outcome.get('contracts'),
          'unfetched_count':int(unfetched.group(1)) if unfetched else None,
          'saved_changed_snapshot':bool(outcome.get('saved')),'positioning_changed':bool(outcome.get('changed')),
          'summary':odme_summary if option_enabled and packet.get('odme_live') else {},
          'assessment':assessment if odme_live else {},'listed_strikes':strikes if odme_live else [],
          'error':('ODME unavailable or incomplete; no live options authorisation.' if option_enabled and not odme_live else '')}
    tv=packet.get('tv_rows')
    tv=tv.to_dict('records') if hasattr(tv,'to_dict') else (tv or [])
    # The exact authoritative raw EDGE book is in edge_context_json. All other
    # current typed rows remain available in TV_TEST_CURRENT, linked by TF/bar.
    # Avoid duplicating huge raw payloads in this single-cell index.
    tv_index=[{k:r.get(k) for k in ('instrument','source','tf','bar_time','updated_at','close','exec_of','exec_of_score','edge_required_exec_tf','edge_tracking_state')} for r in tv if isinstance(r,dict)]
    ctx=dict(context,option_enabled=option_enabled,level_map=level_map,
             selected_expiry=expiry,scan_mode=packet.get('mode'),
             setup_error=bool(packet.get('setup_error')),superbrain_build=packet.get('superbrain_build'))
    return [str(packet['_gpt_scan_id']),symbol,str(packet['_gpt_completed_at']),rulebook_version,VERSION,
            expiry,str(bool(edge_live)).upper(),str(odme_live).upper(),odme_time,status,
            json_cell(edge),json_cell(tv_index),json_cell(odme),json_cell(entries),json_cell(campaigns),json_cell(ctx)]


def prompt_for(row):
    scan_id,symbol,completed,rulebook,protocol,expiry,edge_live,odme_live,odme_at,status=row[:10]
    return (f'SuperBrain scan {symbol}. Exact app scan ID: {scan_id}; completed UTC: {completed}; '
            f'expiry: {expiry or "EDGE-only"}; ODME scan UTC: {odme_at or "not verified"}; '
            f'rulebook: {rulebook}; commentary protocol: {protocol}; receipt: {status}. '
            'Read ODME_Angel_Memory SUPERBRAIN_RULES and SB_RULE_METADATA (all GPT_* rules), '
            'match this exact SB_GPT_SCAN receipt, verify latest EDGE/expiry/campaigns and data freshness, '
            'retrieve relevant historical tapes and Level 2 alerts, then independently analyse in the exact '
            'app commentary format including Active campaigns management and New entry where applicable. '
            'Use natural app language; no internal class labels. If stale or superseded, request a fresh app scan. '
            'Do not trigger a new scan or place orders.')


def publish(store,row):
    """One append-only receipt, RAW cells. Never modify other worksheets."""
    if len(row)!=len(HEADERS):
        raise ValueError('Wrong handoff column count')
    ws=store.sheet.worksheet(TAB)
    if list(ws.row_values(1))!=HEADERS:
        raise ValueError('SB_GPT_SCAN schema mismatch')
    # Idempotent retries after uncertain API responses; no full historical payload read.
    if str(row[0]) in ws.col_values(1):
        raise ValueError('Scan ID already exists; rescan rather than re-publishing an ambiguous receipt.')
    result=ws.append_row(row,value_input_option='RAW',insert_data_option='INSERT_ROWS',table_range='A1:P')
    if int((result.get('updates') or {}).get('updatedRows') or 0)!=1:
        raise RuntimeError('Sheet did not confirm one written receipt')
    return prompt_for(row)


def chatgpt_url(prompt):
    return 'https://chatgpt.com/?'+urlencode({'q':prompt})


def recent_receipt(completed_at, *, now=None, max_minutes=5):
    try:
        t=datetime.fromisoformat(str(completed_at).replace('Z','+00:00'))
        age=((now or datetime.now(timezone.utc))-t).total_seconds()
        return 0 <= age <= max_minutes*60
    except (TypeError,ValueError):
        return False
