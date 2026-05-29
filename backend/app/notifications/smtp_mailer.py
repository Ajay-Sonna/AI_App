"""Send notification emails via SMTP (Gmail / Office 365 compatible)."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    host = (os.getenv("SMTP_HOST") or "").strip()
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    from_email = (os.getenv("NOTIFICATION_FROM_EMAIL") or user).strip()
    return bool(host and user and password and from_email)


def _smtp_port() -> int:
    raw = (os.getenv("SMTP_PORT") or "587").strip()
    try:
        return int(raw)
    except ValueError:
        return 587


def _from_header() -> str:
    user = (os.getenv("SMTP_USER") or "").strip()
    from_email = (os.getenv("NOTIFICATION_FROM_EMAIL") or user).strip()
    from_name = (os.getenv("NOTIFICATION_FROM_NAME") or "Fee Schedule Team").strip()
    if from_name and from_email:
        return f"{from_name} <{from_email}>"
    return from_email


def _normalize_recipients(recipients: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for raw in recipients:
        em = str(raw or "").strip().lower()
        if not em or "@" not in em or em in seen:
            continue
        seen.add(em)
        out.append(em)
    return out


def send_email(
    *,
    to: Sequence[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
) -> None:
    """Send a plain-text email (optional HTML alternative). Raises on SMTP failure."""
    if not smtp_configured():
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and NOTIFICATION_FROM_EMAIL in .env."
        )
    recipients = _normalize_recipients(to)
    if not recipients:
        raise ValueError("No recipients")

    host = (os.getenv("SMTP_HOST") or "").strip()
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    from_email = (os.getenv("NOTIFICATION_FROM_EMAIL") or user).strip()
    port = _smtp_port()

    msg = EmailMessage()
    msg["Subject"] = subject.strip()[:998]
    msg["From"] = _from_header()
    msg["To"] = ", ".join(recipients)
    msg.set_content(body_text.strip())
    if body_html:
        msg.add_alternative(body_html.strip(), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(user, password)
        server.send_message(msg, from_addr=from_email, to_addrs=recipients)
    logger.info("Notification email sent to %s: %s", ", ".join(recipients), subject)
