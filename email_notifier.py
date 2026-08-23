from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable, List


def _normalise_recipients(recipients: Iterable[str] | str) -> List[str]:
    if isinstance(recipients, str):
        raw = [recipients]
    else:
        raw = list(recipients)
    cleaned = [str(x).strip() for x in raw if str(x).strip()]
    if not cleaned:
        raise ValueError("No alert email recipients configured.")
    return cleaned


def send_email(
    sender: str,
    app_password: str,
    recipients: Iterable[str] | str,
    subject: str,
    body: str,
) -> int:
    """Send a plain-text email through Gmail SMTP using a Google App Password."""
    sender = str(sender).strip()
    password = str(app_password).replace(" ", "").strip()
    to_list = _normalise_recipients(recipients)

    if not sender:
        raise ValueError("GMAIL_SENDER is empty.")
    if not password:
        raise ValueError("GMAIL_APP_PASSWORD is empty.")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)

    return len(to_list)
