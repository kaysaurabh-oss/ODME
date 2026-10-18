"""Optional candle AURORA and liquidity relevance filter.

EDGE, ODME and manual Renko AURORA remain decision authorities as before.
No external feed can create orders, a new entry, roll or exit by itself.
Input is a compact list of TV_TEST_CURRENT rows; absent/stale data is silent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import math


def _number(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def _ms(x: Any) -> Optional[float]:
    v = _number(x)
    if v is None or v <= 0:
        return None
    return v * 1000 if v < 1e11 else v


def _minutes(value: Any) -> Optional[float]:
    x = str(value or '').upper().strip()
    if x.endswith('M') and x[:-1].isdigit():
        return float(x[:-1])
    if x.endswith('H') and x[:-1].isdigit():
        return float(x[:-1]) * 60
    if x in ('D', '1D'):
        return 1440.0
    if x in ('W', '1W'):
        return 10080.0
    try:
        v = float(x)
        return v if v > 0 else None
    except ValueError:
        return None


def _fresh(row: Dict[str, Any], edge: Dict[str, Any], now_ms: float) -> bool:
    t = _ms(row.get('bar_time'))
    e = _ms(edge.get('bar_time'))
    tf = _minutes(row.get('tf'))
    etf = _minutes(edge.get('tf'))
    if t is None or e is None or tf is None or etf is None:
        return False
    # Evaluate a confirmed optional bar against EDGE's latest confirmed bar.
    # A higher-TF snapshot is naturally older than an EDGE execution bar.
    # Cap permitted lag even for multi-day charts: stale context is silent.
    max_lag = min(120.0, max(30.0, 1.7 * tf, etf)) * 60_000
    if t - e > 15 * 60_000 or e - t > max_lag:
        return False
    max_wall_age = min(180.0, max(40.0, 2.5 * tf)) * 60_000
    if now_ms - t > max_wall_age or t - now_ms > 10 * 60_000:
        return False
    return True


def _best(rows: List[Dict[str, Any]], source: str, edge: Dict[str, Any], now_ms: float):
    qualified = [x for x in rows if isinstance(x, dict) and
                 str(x.get('source') or '').upper() == source and _fresh(x, edge, now_ms)]
    return max(qualified, key=lambda x: _ms(x.get('bar_time')) or -1) if qualified else {}


def _bool(x: Any) -> bool:
    return str(x or '').strip().lower() in ('true','1','yes','on','active') or x is True


def _box_read(aurora: Dict[str, Any], edge: Dict[str, Any]):
    active = _bool(aurora.get('htf_box_active')) or _bool(aurora.get('htf_range_active'))
    lo = _number(aurora.get('htf_box_low'))
    hi = _number(aurora.get('htf_box_high'))
    if lo is None:
        lo = _number(aurora.get('htf_range_low'))
    if hi is None:
        hi = _number(aurora.get('htf_range_high'))
    px = _number(edge.get('close'))
    if not active or lo is None or hi is None or px is None or lo >= hi:
        return None
    of = str(edge.get('exec_of') or '').upper()
    bullish = 'BULL' in of and 'BEAR' not in of
    bearish = 'BEAR' in of
    status = str(aurora.get('htf_box_state') or aurora.get('htf_box_breakout') or '').upper()
    confirmed = _bool(aurora.get('htf_box_break_confirmed')) or 'CONFIRMED' in status or 'ACCEPTED' in status
    if px > hi:
        return (f"HTF box upside breakout {'confirmed' if confirmed else 'being tested'}; " +
                ('bullish EDGE control supports continuation.' if bullish else
                 'EDGE control does not confirm the breakout yet.'),
                'notice' if confirmed and bullish else 'caution')
    if px < lo:
        return (f"HTF box downside breakout {'confirmed' if confirmed else 'being tested'}; " +
                ('bearish EDGE control supports continuation.' if bearish else
                 'EDGE control does not confirm the breakout yet.'),
                'notice' if confirmed and bearish else 'caution')
    position = (px - lo) / (hi - lo)
    if position <= 0.25:
        return ('HTF range: near lower edge; BUY only after EDGE Demand defence and manual Renko AURORA agreement.', 'caution')
    if position >= 0.75:
        return ('HTF range: near upper edge; SELL only after EDGE Supply defence and manual Renko AURORA agreement.', 'caution')
    return ('HTF range: price inside the box; avoid directional entries in the middle.', 'caution')


def _aurora_read(aurora: Dict[str, Any], edge: Dict[str, Any]):
    state = str(aurora.get('aurora_state') or '').strip().upper()
    box = _box_read(aurora, edge)
    if box:
        return {'source': 'AURORA', 'severity': box[1], 'text': box[0]}
    if state == 'WHITE':
        return {'source': 'AURORA', 'severity': 'caution',
                'text': 'Candle AURORA WHITE: sideways/choppy; avoid fresh directional entries until price/EDGE confirms a clean move.'}
    if state not in ('GREEN', 'RED', 'YELLOW', 'PINK'):
        return None
    of = str(edge.get('exec_of') or '').upper()
    strong_bull = 'BULL' in of and 'BEAR' not in of
    strong_bear = 'BEAR' in of
    disagreement = (strong_bull and state in ('RED', 'PINK')) or (strong_bear and state in ('GREEN', 'YELLOW'))
    if disagreement:
        return {'source': 'AURORA', 'severity': 'caution',
                'text': f'Candle AURORA {state} conflicts with EDGE OF; avoid assuming an uninterrupted directional move.'}
    change = str(aurora.get('aurora_change') or '').upper()
    if change in ('FRESH_TURN', 'STATE_CHANGE', 'CONFIRMED_TURN', 'FLIP') and (
       (strong_bull and state == 'GREEN') or (strong_bear and state == 'RED')):
        return {'source': 'AURORA', 'severity': 'notice',
                'text': f'Fresh candle AURORA {state} turn supports current EDGE control.'}
    return None


def _liquidity_read(row: Dict[str, Any], edge: Dict[str, Any]):
    edge_ms = _ms(edge.get('bar_time'))
    tf = _minutes(row.get('tf')) or 15
    if edge_ms is None:
        return None
    events = []
    for n in range(1, 6):
        p = f'liq_event{n}_'
        kind = str(row.get(p+'type') or '').upper()
        when = _ms(row.get(p+'confirm_time'))
        if not kind or 'FULL' not in kind or when is None:
            continue
        # Do not replay a historical event simply because the LIQUIDITY state
        # contains a preserved event ledger.
        if when > edge_ms + 15*60_000 or edge_ms - when > min(180.0, max(35.0, 2.5 * tf)) * 60_000:
            continue
        if not ('RUN' in kind or 'SWEEP' in kind):
            continue
        events.append({'kind':kind,'at':when})
    if not events:
        return None
    events.sort(key=lambda x:x['at'], reverse=True)
    latest = events[0]['kind']
    bull = 'BUY' in latest
    run = 'RUN' in latest
    if run:
        text = ('Buy-side liquidity RUN: upside acceptance and bullish continuation pressure.' if bull else
                'Sell-side liquidity RUN: downside acceptance and bearish continuation pressure.')
    else:
        text = ('Buy-side liquidity SWEEP: price rejected the upside attempt; downside reversal risk.' if bull else
                'Sell-side liquidity SWEEP: price reclaimed after downside sweep; upside reversal potential.')
    # A genuine 2-event sequence adds insight; do not merely repeat a pool list.
    if len(events) > 1 and 0 < events[0]['at'] - events[1]['at'] <= 3 * tf * 60_000:
        first = events[1]['kind']
        if first != latest and ('SWEEP' in first or 'RUN' in first):
            one = ('buy-side' if 'BUY' in first else 'sell-side') + (' sweep' if 'SWEEP' in first else ' run')
            two = ('buy-side' if bull else 'sell-side') + (' run' if run else ' sweep')
            text = f'Liquidity sequence {one} → {two}: ' + (
                'upside acceptance.' if run and bull else 'downside acceptance.' if run else
                'rejection/reversal risk.')
    return {'source':'LIQUIDITY','severity':'notice','text':text}


def relevant_optional_context(rows: List[Dict[str, Any]], edge: Dict[str, Any],
                              now_ms: Optional[float] = None) -> List[Dict[str, str]]:
    """Return at most 2 independent material observations; no missing-feed chatter."""
    if not edge or str(edge.get('edge_tracking_state') or '').upper() != 'ACTIVE':
        return []
    if str(edge.get('tf') or '') != str(edge.get('edge_required_exec_tf') or edge.get('exec_tf') or ''):
        return []
    now_ms = float(now_ms) if now_ms is not None else datetime.now(timezone.utc).timestamp()*1000
    a = _best(rows, 'AURORA', edge, now_ms)
    l = _best(rows, 'LIQUIDITY', edge, now_ms)
    out = []
    if a:
        item = _aurora_read(a, edge)
        if item: out.append(item)
    if l:
        item = _liquidity_read(l, edge)
        if item: out.append(item)
    # Priority: exceptional caution first; a routine alignment or old event
    # should never crowd out the actual EDGE+ODME decision.
    out.sort(key=lambda x: 0 if x.get('severity')=='caution' else 1)
    return out[:2]
