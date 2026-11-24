"""Display-only, source-grounded next-barrier and continuation-watch commentary.

No trading eligibility, alerts, zone status, or Pine calculations are changed.
"""
from __future__ import annotations
from typing import Any, Dict, List
from superbrain_zone_forecast import _current_book, _roles, _n


def _fmt(v: float) -> str:
    return f'{v:,.2f}'.rstrip('0').rstrip('.') if v != int(v) else f'{v:,.0f}'


def _name(z: Dict[str, Any]) -> str:
    prefix = 'FP' if z.get('source') in ('LIVE_BOOK', 'SELECTED_FP') else 'structural'
    return f'{z.get("tf") or "current-TF"} {prefix} {z["side"].title()} {_fmt(z["low"])}–{_fmt(z["high"])}'


def journey_destinations(edge: Dict[str, Any], tape: List[Dict[str, Any]],
                         side: int, decision: Dict[str, Any]) -> Dict[str, str]:
    """Return two scenario paths; no synthetic FP events or guessed levels."""
    spot = _n(edge.get('close'))
    if spot is None or spot <= 0:
        return {}
    book = _current_book(edge, tape)
    fp = book['zones']
    structural = [dict(z, zone_id=z['role']) for z in _roles(edge)
                  if z.get('source') == 'STRUCTURAL']
    # Router map is reference-only, not a verified active destination.
    def nearest(direction: int):
        typ = 'DEMAND' if direction < 0 else 'SUPPLY'
        def eligible(z):
            return (z['side'] == typ and
                    (z['low'] <= spot if direction < 0 else z['high'] >= spot))
        choices = [z for z in fp if eligible(z)]
        if not choices:
            choices = [z for z in structural if eligible(z)]
        if not choices:
            return None
        return min(choices, key=lambda z: (max(0, spot-z['high']) if direction < 0
                                           else max(0, z['low']-spot),
                                           z['high']-z['low']))
    lower, upper = nearest(-1), nearest(1)
    def destination(direction):
        zone = lower if direction < 0 else upper
        if zone:
            return _name(zone)
        return ('No verified lower demand level in the current active FP/structural book'
                if direction < 0 else
                'No verified higher supply level in the current active FP/structural book')
    if side not in (-1, 1):
        return {'primary': 'Directional journey unresolved; do not infer a destination from an unconfirmed event.',
                'alternative': f'Lower reference: {destination(-1)}. Upper reference: {destination(1)}.'}
    primary = destination(side)
    alternate = destination(-side)
    if side < 0:
        main = (f'**Downside destination:** {primary}. A defence there could interrupt the decline; '
                'only a verified break supports extension to the next lower demand.')
        other = (f'**Upside alternative:** A confirmed reclaim of opposing supply would weaken the bearish route. '
                 f'Next observed upside reference: {alternate}.')
        watch = ('**Preferred continuation opportunity:** Following a verified FP Demand break, '
                 'watch for newly observed FP Supply or a retest of the broken Demand from below. '
                 'Treat either as candidate resistance until a new qualifying bearish FP event confirms it. '
                 'Do not chase the break; Renko and existing entry gates still control execution.')
    else:
        main = (f'**Upside destination:** {primary}. An initial supply reaction is possible; '
                'only a verified break supports extension to the next higher supply.')
        other = (f'**Downside alternative:** A confirmed failure of supporting demand would weaken the bullish route. '
                 f'Next observed downside reference: {alternate}.')
        watch = ('**Preferred continuation opportunity:** Following a verified FP Supply break, '
                 'watch for newly observed FP Demand or a retest of broken Supply from above. '
                 'Treat either as candidate support until a new qualifying bullish FP event confirms it. '
                 'Do not chase the break; Renko and existing entry gates still control execution.')
    # This is conditional guidance, never an assertion that a break occurred.
    return {'primary': main, 'alternative': other, 'continuation_watch': watch,
            'primary_zone': primary, 'alternative_zone': alternate,
            'book_truncated': book['truncated']}
