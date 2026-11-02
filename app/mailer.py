"""Plain SMTP sender. If SMTP is not configured, messages are logged instead."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app import config

log = logging.getLogger("mailer")


def send(to: str, subject: str, body: str) -> bool:
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS):
        log.warning("SMTP not configured; would send to %s: %s", to, subject)
        return False
    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30) as s:
                s.login(config.SMTP_USER, config.SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(config.SMTP_USER, config.SMTP_PASS)
                s.send_message(msg)
        return True
    except Exception as e:  # never crash the worker on mail
        log.error("mail to %s failed: %s", to, e)
        return False
