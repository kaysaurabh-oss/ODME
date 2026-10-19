"""Exchange session context, only for historical display and price-gap checks.

2026 schedules from NSE / MCX published calendars. Always treat this as an
advisory calendar: unexpected closures/special sessions can happen. Never use
this module to authorize orders or to manufacture execution/zone/OF signals.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

IST=ZoneInfo('Asia/Kolkata')
NSE_HOLIDAYS_2026=set('''2026-01-15 2026-01-26 2026-03-03 2026-03-26 2026-03-31 2026-04-03 2026-04-14 2026-05-01 2026-05-28 2026-06-26 2026-09-14 2026-10-02 2026-10-20 2026-11-10 2026-11-24 2026-12-25'''.split())
MCX_FULL_2026=set('''2026-01-26 2026-04-03 2026-10-02 2026-12-25'''.split())
MCX_MORNING_2026=set('''2026-03-03 2026-03-26 2026-03-31 2026-04-14 2026-05-01 2026-05-28 2026-06-26 2026-09-14 2026-10-20 2026-11-10 2026-11-24'''.split())
MCX_EVENING_2026={'2026-01-01'}
NSE_INDEXES={'NIFTY','NIFTY50','BANKNIFTY','MIDCPNIFTY','FINNIFTY','NIFTYNXT50','NIFTYIT','NIFTYBANK','SENSEX','BANKEX'}
MCX_NAMES={'SILVERM','SILVER','SILVERMIC','GOLD','GOLDM','GOLDPETAL','GOLDGUINEA',
           'COPPER','ZINC','LEAD','NICKEL','ALUMINIUM','ALUMINI','CRUDEOIL','CRUDEOILM',
           'NATURALGAS','NATGASMINI','COTTON','COTTONCNDY','MENTHAOIL'}
CRYPTO_PREFIX=('BTC','ETH','SOL','XRP','DOGE','ADA','LTC','BCH','BNB')


def market_of(instrument: str) -> str:
    s=str(instrument or '').upper().strip().split(':')[-1].split('!')[0]
    if s in NSE_INDEXES or str(instrument).upper().startswith(('NSE:','BSE:')):return 'NSE'
    if s in MCX_NAMES or str(instrument).upper().startswith('MCX:'):return 'MCX'
    if s.endswith(('USD','USDT')) and s.startswith(CRYPTO_PREFIX):return 'CRYPTO'
    return 'UNKNOWN'


def _mcx_close(d:date) -> int:
    # 2026-11-02 is the published MCX non-agri changeover to 23:55 IST.
    return 23*60+(55 if d>=date(2026,11,2) else 30)


def _periods(exchange:str,d:date):
    """Minutes-after-midnight of regular published exchange sessions."""
    if d.weekday()>=5:return []
    iso=d.isoformat()
    if exchange=='NSE':
        if d.year==2026 and iso in NSE_HOLIDAYS_2026:return []
        return [(9*60+15,15*60+30)]
    if exchange=='MCX':
        if d.year==2026 and iso in MCX_FULL_2026:return []
        close=_mcx_close(d)
        if d.year==2026 and iso in MCX_MORNING_2026:return [(17*60,close)]
        if d.year==2026 and iso in MCX_EVENING_2026:return [(9*60,17*60)]
        return [(9*60,close)]
    return []


def session_status(instrument:str,at:Optional[datetime]=None) -> Dict[str,Any]:
    """Identifies a scheduled closure, not whether an exchange feed is healthy."""
    at=(at or datetime.now(timezone.utc)).astimezone(IST)
    exchange=market_of(instrument)
    if exchange=='CRYPTO':return dict(exchange='CRYPTO',state='CONTINUOUS',is_open=True,known=True,reason='24/7 market')
    if exchange=='UNKNOWN':return dict(exchange='UNKNOWN',state='UNKNOWN',is_open=None,known=False,reason='No verified exchange schedule')
    mm=at.hour*60+at.minute
    periods=_periods(exchange,at.date())
    open_now=any(a<=mm<b for a,b in periods)
    if open_now:
        return dict(exchange=exchange,state='OPEN',is_open=True,known=True,reason='Scheduled trading session')
    if at.weekday()>=5:reason='Weekend'
    elif exchange=='NSE' and at.year==2026 and at.date().isoformat() in NSE_HOLIDAYS_2026:reason='Exchange holiday'
    elif exchange=='MCX' and at.year==2026 and at.date().isoformat() in MCX_FULL_2026:reason='Full-session exchange holiday'
    elif exchange=='MCX' and at.year==2026 and at.date().isoformat() in MCX_MORNING_2026 and mm<17*60:reason='Morning holiday; evening session starts 17:00 IST'
    else:reason='Outside scheduled trading hours'
    return dict(exchange=exchange,state='CLOSED',is_open=False,known=True,reason=reason)


def _as_dt(ms:Any)->Optional[datetime]:
    try:
        n=float(ms)
        if not (n>0):return None
        return datetime.fromtimestamp(n/1000 if n>=1e11 else n,tz=timezone.utc)
    except (TypeError,ValueError,OverflowError,OSError):return None


def last_session_snapshot(instrument:str,bar_ms:Any,now:Optional[datetime]=None)->bool:
    """Recent real EDGE candle is suitable for a labelled historical review.

    A closed market does not make a week-old unexplained feed outage valid.
    """
    now=now or datetime.now(timezone.utc)
    state=session_status(instrument,now)
    if state['state']!='CLOSED':return False
    bar=_as_dt(bar_ms)
    if bar is None:return False
    age=(now.astimezone(timezone.utc)-bar).total_seconds()
    if age < -5*60 or age > 7*86400:return False
    bar_local=bar.astimezone(IST)
    periods=_periods(state['exchange'],bar_local.date())
    mm=bar_local.hour*60+bar_local.minute
    # EDGE timestamp is candle CLOSE: the end-of-session bar is admissible.
    return any(a<=mm<=b+5 for a,b in periods)


def scheduled_trading_minutes(instrument:str,start_ms:Any,end_ms:Any)->Optional[float]:
    """Count open exchange minutes between two REAL bar closes.

    Returns None when no verified session model exists. Used only for deciding
    whether an observed price journey can bridge a closed interval; never fills
    bars across closure. A maximum of 14 days bounds the small calculation.
    """
    exchange=market_of(instrument)
    if exchange=='UNKNOWN':return None
    a=_as_dt(start_ms);b=_as_dt(end_ms)
    if a is None or b is None or b<a:return None
    if exchange=='CRYPTO':return (b-a).total_seconds()/60
    if b-a>timedelta(days=14):return None
    a=a.astimezone(IST);b=b.astimezone(IST)
    day=a.date();total=0.0
    while day<=b.date():
        for x,y in _periods(exchange,day):
            midnight=datetime(day.year,day.month,day.day,tzinfo=IST)
            lo=max(a,midnight+timedelta(minutes=x))
            hi=min(b,midnight+timedelta(minutes=y))
            if hi>lo:total+=(hi-lo).total_seconds()/60
        day+=timedelta(days=1)
    return total


def is_open_candle(instrument:str,bar_ms:Any)->bool:
    dt=_as_dt(bar_ms)
    if dt is None:return False
    market=market_of(instrument)
    if market=='CRYPTO':return True
    if market=='UNKNOWN':return False  # never interpolate without a calendar
    local=dt.astimezone(IST)
    mm=local.hour*60+local.minute
    return any(a<mm<=b for a,b in _periods(market,local.date()))
