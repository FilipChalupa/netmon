"""Email sending via smtplib. Same SMTP_* variables as the old version:

SMTP_HOST (required), SMTP_TO (required, comma-separated), SMTP_PORT,
SMTP_TLS = starttls (default, 587) | ssl/smtps (465) | none (25),
SMTP_USER / SMTP_PASS, SMTP_FROM (default = SMTP_USER or netmon@localhost).

SMTP_DRYRUN=1 → the email is not sent, it is saved as .eml (for testing).
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("netmon.mailer")


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_TO"))


def send_email(subject: str, text_body: str,
               attachments: list[tuple[str, bytes, str, str]] | None = None,
               out_dir: str = ".") -> bool:
    """attachments: (filename, data, maintype, subtype). Returns True when sent/saved."""
    host = os.environ.get("SMTP_HOST", "")
    to = os.environ.get("SMTP_TO", "")
    if not host or not to:
        log.info("SMTP is not configured (SMTP_HOST/SMTP_TO) — not sending email.")
        return False

    tls = os.environ.get("SMTP_TLS", "starttls").lower()
    default_port = {"ssl": 465, "smtps": 465, "none": 25}.get(tls, 587)
    port = int(os.environ.get("SMTP_PORT", default_port))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("SMTP_FROM") or user or "netmon@localhost"
    recipients = [r.strip() for r in to.replace(",", " ").split() if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    for filename, data, maintype, subtype in attachments or []:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    if os.environ.get("SMTP_DRYRUN"):
        path = os.path.join(out_dir, f"netmon-{subject.replace(' ', '_').replace('/', '-')}.eml")
        with open(path, "wb") as f:
            f.write(bytes(msg))
        log.info("SMTP_DRYRUN: email saved to %s", path)
        return True

    if tls in ("ssl", "smtps"):
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if tls == "starttls":
            server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)
        log.info("Email sent to %s", ", ".join(recipients))
        return True
    finally:
        server.quit()
