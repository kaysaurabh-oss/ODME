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
DUE_GRACE_MINUTES = 10


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
    """Return the oldest unprocessed selected slot within the grace window.

    The worker is deliberately day-agnostic: weekdays, weekends and holidays are
    treated the same. If market data is unchanged, scan_service refuses to save a
    duplicate snapshot, so the prior trading-session anchor remains intact.
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
    return sorted(candidates)[0]


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


def _instrument_summary(instrument: str, expiry: str, outcome: Dict[str, Any]) -> str:
    result = outcome.get("result", {}) or {}
    scores = result.get("scores", {}) or {}
    cards = result.get("cards", {}) or {}
    path = result.get("path_risk", {}) or {}

    expansion = float(scores.get("Expansion", 0) or 0)
    bullish = int(float(scores.get("Bullish", 0) or 0))
    bearish = int(float(scores.get("Bearish", 0) or 0))
    range_score = int(float(scores.get("Range", 0) or 0))
    expansion_i = int(expansion)

    ce_card = cards.get("ce_wall", {}) or {}
    pe_card = cards.get("pe_wall", {}) or {}
    sce_card = cards.get("safer_ce", {}) or {}
    spe_card = cards.get("safer_pe", {}) or {}
    poc_card = cards.get("poc", {}) or {}

    up_path = path.get("upside", {}) or {}
    dn_path = path.get("downside", {}) or {}

    save_state = "CHANGED — snapshot saved" if outcome.get("saved") else "UNCHANGED — anchor preserved"
    if not outcome.get("anchor"):
        save_state = "NEW BASELINE — snapshot saved" if outcome.get("saved") else save_state

    lines = [
        f"{instrument} | Expiry {expiry}",
        f"Verdict: {result.get('tilt', 'NA')}",
        f"Future/Spot: {_fmt_num(outcome.get('future_ltp') or result.get('spot'), 2)} | POC: {_fmt_num(result.get('poc'), 0)} ({result.get('poc_move', 'NA')})",
        f"Scores: Bull {bullish} | Bear {bearish} | Range {range_score} | Expansion {expansion_i} [{_expansion_label(expansion)}]",
        f"CE wall: {_fmt_num(result.get('ce_wall'), 0)} ({result.get('ce_wall_move', 'NA')}) | {ce_card.get('state', 'NA')}",
        f"Safer CE: {_fmt_num(result.get('safer_sell_ce'), 0)} | {sce_card.get('state', 'NA')}",
        f"PE wall: {_fmt_num(result.get('pe_wall'), 0)} ({result.get('pe_wall_move', 'NA')}) | {pe_card.get('state', 'NA')}",
        f"Safer PE: {_fmt_num(result.get('safer_sell_pe'), 0)} | {spe_card.get('state', 'NA')}",
        f"Path: Upside {up_path.get('path', 'NA')} | Downside {dn_path.get('path', 'NA')}",
    ]

    poc_state = str(poc_card.get("state", "")).strip()
    premium_alert = _first_line(result.get("premium_alert"))
    action = _first_line(result.get("hero_action") or result.get("final_action"))
    risk_bits: List[str] = []
    if poc_state:
        risk_bits.append(poc_state)
    if premium_alert:
        risk_bits.append(premium_alert.replace("Premium alert:", "Premium:"))
    if action:
        risk_bits.append(action)
    if risk_bits:
        lines.append("Risk/Read: " + " | ".join(risk_bits))

    lines.append(f"Data state: {save_state}")
    anchor_ts = str(result.get("anchor_snapshot_ts") or "").strip()
    if anchor_ts:
        lines.append(f"Anchor: {anchor_ts}")
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
        "Anchor rule: ODME compares with the latest saved CHANGED snapshot before today. "
        "If a weekend/holiday/closed-market scan returns unchanged data, no new snapshot is saved and the old anchor is preserved.",
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
