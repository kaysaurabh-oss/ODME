from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from angel_connector import AngelConnector, load_angel_credentials
from data_store import create_store
from email_notifier import send_email
from runtime_config import get_list, get_secret
from scan_service import run_odme_scan

IST = ZoneInfo("Asia/Kolkata")
DUE_GRACE_MINUTES = 60


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_scan_times(value: Any) -> List[str]:
    out: List[str] = []
    for raw in str(value or "").split(","):
        s = raw.strip()
        if not s:
            continue
        try:
            hh, mm = [int(x) for x in s.split(":", 1)]
        except Exception:
            continue
        if 0 <= hh <= 23 and 0 <= mm <= 59 and mm % 5 == 0:
            out.append(f"{hh:02d}:{mm:02d}")
    return sorted(set(out))


def _slot_id(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def _find_due_slot(scan_times: List[str], last_run_slot: str, now_ist: datetime) -> Optional[datetime]:
    """Return the latest unprocessed selected slot within the grace window.

    GitHub scheduled workflows can start later than the nominal cron time. A wider
    grace window prevents a delayed job from being discarded. If more than one slot
    is pending, ODME uses the latest one rather than back-filling stale scans.

    Weekdays, weekends and holidays are treated the same. If market data is
    unchanged, scan_service refuses to save a duplicate snapshot, so the prior
    trading-session anchor remains intact.
    """
    candidates: List[datetime] = []
    for day in [now_ist.date() - timedelta(days=1), now_ist.date()]:
        for hhmm in scan_times:
            hh, mm = [int(x) for x in hhmm.split(":")]
            dt = datetime.combine(day, time(hh, mm), tzinfo=IST)
            age_min = (now_ist - dt).total_seconds() / 60.0
            if 0 <= age_min <= DUE_GRACE_MINUTES and _slot_id(dt) != str(last_run_slot or ""):
                candidates.append(dt)
    if not candidates:
        return None
    return sorted(candidates)[-1]


def _fmt_num(value: Any, decimals: int = 0) -> str:
    try:
        x = float(value)
        return f"{x:,.{decimals}f}"
    except Exception:
        return "NA"


def _first_line(text: Any, max_len: int = 220) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    if len(line) > max_len:
        line = line[: max_len - 3].rstrip() + "..."
    return line


def _expansion_label(score: float) -> str:
    if score >= 75:
        return "HIGH"
    if score >= 55:
        return "ELEVATED"
    if score >= 35:
        return "WATCH"
    return "LOW"


def _move_arrow(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "higher" in text or "up" in text:
        return "↑"
    if "lower" in text or "down" in text:
        return "↓"
    if "same" in text or "unchanged" in text or "stable" in text:
        return "="
    return ""


def _instrument_summary(instrument: str, expiry: str, outcome: Dict[str, Any]) -> str:
    result = outcome.get("result", {}) or {}
    scores = result.get("scores", {}) or {}
    cards = result.get("cards", {}) or {}
    path = result.get("path_risk", {}) or {}

    expansion = float(scores.get("Expansion", 0) or 0)

    ce_wall = result.get("ce_wall")
    pe_wall = result.get("pe_wall")
    safer_ce = result.get("safer_sell_ce")
    safer_pe = result.get("safer_sell_pe")
    poc = result.get("poc")

    ce_arrow = _move_arrow(result.get("ce_wall_move"))
    pe_arrow = _move_arrow(result.get("pe_wall_move"))
    poc_arrow = _move_arrow(result.get("poc_move"))

    up_path = path.get("upside", {}) or {}
    dn_path = path.get("downside", {}) or {}

    premium_alert = _first_line(result.get("premium_alert"))
    action = _first_line(result.get("hero_action") or result.get("final_action"))

    poc_card = cards.get("poc", {}) or {}
    poc_state = _first_line(poc_card.get("state"))

    read_bits: List[str] = []

    if poc_state:
        read_bits.append(poc_state)

    if premium_alert:
        clean_premium = premium_alert.replace("Premium alert:", "").strip()
        if clean_premium:
            read_bits.append(clean_premium)

    read_text = " ".join(read_bits).strip()

    lines = [
        f"{instrument} | {expiry} — {result.get('tilt', 'NA')}",
        "",
        (
            f"Market: {_fmt_num(outcome.get('future_ltp') or result.get('spot'), 2)}"
            f" | POC {_fmt_num(poc, 0)} {poc_arrow}"
            f" | Expansion {_expansion_label(expansion)}"
        ),
        (
            f"CE: Wall {_fmt_num(ce_wall, 0)} {ce_arrow}"
            f" | Safer {_fmt_num(safer_ce, 0)}"
        ),
        (
            f"PE: Wall {_fmt_num(pe_wall, 0)} {pe_arrow}"
            f" | Safer {_fmt_num(safer_pe, 0)}"
        ),
        (
            f"Path: Up {up_path.get('path', 'NA')}"
            f" | Down {dn_path.get('path', 'NA')}"
        ),
    ]

    if read_text:
        lines.append(f"Read: {read_text}")

    if action:
        lines.append(f"Action: {action}")

    return "\n".join(lines)


def _error_summary(instrument: str, expiry: str, exc: Exception) -> str:
    return f"{instrument} | Expiry {expiry}\nSCAN ERROR: {type(exc).__name__}: {exc}"


def _build_email(now_ist: datetime, blocks: List[str]) -> Tuple[str, str]:
    when = now_ist.strftime("%d %b %Y %H:%M IST")
    subject = f"ODME Scheduled Summary — {now_ist.strftime('%H:%M')} IST — {len(blocks)} instrument(s)"
    header = [
        "ODME SCHEDULED SUMMARY",
        when,
        "",
    ]
    body = "\n".join(header) + ("\n\n" + ("\n\n" + "-" * 72 + "\n\n").join(blocks) if blocks else "No due alert instruments.")
    return subject, body


def run(now_ist: Optional[datetime] = None) -> int:
    now_ist = (now_ist or datetime.now(IST)).astimezone(IST)
    store = create_store()
    cleanup = store.cleanup_expired_data(tz_name="Asia/Kolkata")
    if cleanup.get("deleted_snapshots") or cleanup.get("cleared_scan_settings"):
        print(f"Expired cleanup: {cleanup}")

    settings = store.list_instrument_settings(active_only=True)
    if settings is None or settings.empty:
        print("No active instruments configured.")
        return 0

    due: List[Tuple[Dict[str, Any], datetime]] = []
    for _, row in settings.iterrows():
        item = row.to_dict()
        if not _as_bool(item.get("scan_enabled")):
            continue
        expiry = str(item.get("selected_expiry", "")).strip()
        times = _parse_scan_times(item.get("scan_times", ""))
        if not expiry or not times:
            continue
        slot = _find_due_slot(times, str(item.get("last_run_slot", "")), now_ist)
        if slot is not None:
            due.append((item, slot))

    if not due:
        print(f"No scheduled ODME scans due at {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}.")
        return 0

    print(f"{len(due)} scheduled instrument(s) due. Starting automatic Angel login...")
    angel = AngelConnector(load_angel_credentials())
    angel.login_automatic()
    master = angel.load_instrument_master()
    print(f"Angel login successful. Instrument master rows: {len(master):,}")

    alert_blocks: List[str] = []
    processed: List[Tuple[str, datetime]] = []

    for item, slot in due:
        instrument = str(item.get("instrument", "")).upper().strip()
        expiry = str(item.get("selected_expiry", "")).strip()
        try:
            outcome = run_odme_scan(store, angel, master, instrument, expiry, save_only_if_changed=True)
            print(f"{instrument} {expiry}: scan OK; changed={outcome.get('changed')} saved={outcome.get('saved')}")
            if _as_bool(item.get("email_alert")):
                alert_blocks.append(_instrument_summary(instrument, expiry, outcome))
            processed.append((instrument, slot))
        except Exception as exc:
            print(f"{instrument} {expiry}: ERROR: {exc}", file=sys.stderr)
            if _as_bool(item.get("email_alert")):
                alert_blocks.append(_error_summary(instrument, expiry, exc))
            processed.append((instrument, slot))

    # One consolidated email for every scheduled batch.
    if alert_blocks:
        sender = str(get_secret("GMAIL_SENDER", "") or "").strip()
        password = str(get_secret("GMAIL_APP_PASSWORD", "") or "")
        recipients = get_list("ALERT_EMAILS")
        subject, body = _build_email(now_ist, alert_blocks)
        sent = send_email(sender, password, recipients, subject, body)
        print(f"Email sent to {sent} recipient(s).")

    # Mark slots only after the consolidated email step succeeds. If email sending
    # fails, the scheduler can retry within the grace window.
    for instrument, slot in processed:
        store.upsert_instrument_setting(instrument, last_run_slot=_slot_id(slot))

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
