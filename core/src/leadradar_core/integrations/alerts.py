"""Alerts add-on (SPEC core CO-A2): Telegram bot API and/or e-mail over SMTP, behind FEATURE_ALERTS.

Consumer of `lead.tier_changed` (only when the lead becomes hot) and `signal.detected` (only for questions of
high weight). Each channel is its own consumer, so a failing channel is retried without re-sending the other.
"""

import asyncio
import html
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
from structlog import get_logger

from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event
from leadradar_core.settings import AppSettings

log = get_logger(__name__)

TIMEOUT_S = 10.0
ALERT_EVENTS = frozenset({events.LEAD_TIER_CHANGED, events.SIGNAL_DETECTED})


def is_alert(event: Event) -> bool:
    p = event.payload
    if event.type == events.LEAD_TIER_CHANGED:
        return p.get("tier_after") == "hot" and p.get("tier_before") != "hot"
    if event.type == events.SIGNAL_DETECTED:
        return p.get("weight") == "high"
    return False


def _lead_link(public_origin: str, payload: dict[str, Any]) -> str:
    return f"{public_origin.rstrip('/')}/leads/{payload.get('company_id')}?service_id={payload.get('service_id')}"


def render(event: Event, public_origin: str) -> tuple[str, list[str]]:
    """(subject, body lines) in plain text; channels escape or format them as needed."""
    p = event.payload
    company = p.get("company_name") or p.get("domain") or p.get("company_id")
    service = p.get("service_name") or ""
    if event.type == events.LEAD_TIER_CHANGED:
        subject = f"Hot lead: {company} ({service})"
        lines = [
            f"{company} ({p.get('domain')}) is now HOT for {service} (was {p.get('tier_before') or 'new'}).",
            f"Priority {p.get('priority')} | fit {p.get('fit')} | intent {p.get('intent')} | risk {p.get('risk')}",
        ]
        why = p.get("why_now") or []
        if why:
            lines.append("Why now:")
            lines += [
                f"- {r.get('text')} ({r.get('source_name') or ''} {r.get('url') or ''})".strip() for r in why
            ]
    else:
        subject = f"Strong signal: {company} ({service})"
        lines = [
            f"{company}: {p.get('summary')}",
            f'[{p.get("category")}, {p.get("polarity")}, {p.get("strength")}] "{p.get("quote")}"',
            f"Source: {p.get('source_name')} {p.get('url') or ''}".strip(),
        ]
    lines.append(_lead_link(public_origin, p))
    return subject, lines


class TelegramAlerts:
    name = "alerts.telegram"
    event_types = ALERT_EVENTS

    def __init__(self, token: str, chat_id: str, public_origin: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._origin = public_origin

    async def handle(self, event: Event) -> None:
        if not is_alert(event):
            return
        subject, lines = render(event, self._origin)
        text = f"<b>{html.escape(subject)}</b>\n" + "\n".join(html.escape(line) for line in lines)
        body = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        # the bot token is part of the URL: errors are re-raised without it (they end up in logs and the outbox)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                res = await client.post(self._url, json=body)
        except httpx.HTTPError as e:
            raise RuntimeError(f"telegram request failed: {type(e).__name__}") from None
        if res.is_error:
            raise RuntimeError(f"telegram responded {res.status_code}: {res.text[:200]}")


class EmailAlerts:
    name = "alerts.email"
    event_types = ALERT_EVENTS

    def __init__(self, s: AppSettings) -> None:
        self._s = s
        self._to = [a.strip() for a in s.ALERTS_EMAIL_TO.split(",") if a.strip()]

    def _send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self._s.SMTP_HOST, self._s.SMTP_PORT, timeout=TIMEOUT_S) as smtp:
            if self._s.SMTP_STARTTLS:
                smtp.starttls()
            if self._s.SMTP_USERNAME:
                smtp.login(self._s.SMTP_USERNAME, self._s.SMTP_PASSWORD)
            smtp.send_message(msg)

    async def handle(self, event: Event) -> None:
        if not is_alert(event):
            return
        subject, lines = render(event, self._s.PUBLIC_ORIGIN)
        msg = EmailMessage()
        msg["Subject"] = f"[LeadRadar] {subject}"
        msg["From"] = self._s.SMTP_FROM or self._s.SMTP_USERNAME
        msg["To"] = ", ".join(self._to)
        msg.set_content("\n".join(lines))
        await asyncio.to_thread(self._send, msg)


def alert_consumers(s: AppSettings) -> list[TelegramAlerts | EmailAlerts]:
    if not s.FEATURE_ALERTS:
        return []
    consumers: list[TelegramAlerts | EmailAlerts] = []
    if s.TELEGRAM_BOT_TOKEN and s.TELEGRAM_CHAT_ID:
        consumers.append(TelegramAlerts(s.TELEGRAM_BOT_TOKEN, s.TELEGRAM_CHAT_ID, s.PUBLIC_ORIGIN))
    else:
        log.info("alerts_telegram_disabled", reason="APP_TELEGRAM_BOT_TOKEN or APP_TELEGRAM_CHAT_ID is empty")
    if s.SMTP_HOST and s.ALERTS_EMAIL_TO and (s.SMTP_FROM or s.SMTP_USERNAME):
        consumers.append(EmailAlerts(s))
    else:
        log.info(
            "alerts_email_disabled", reason="APP_SMTP_HOST, APP_SMTP_FROM or APP_ALERTS_EMAIL_TO is empty"
        )
    return consumers
