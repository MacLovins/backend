"""User-defined alert rules as an outbox consumer (always on: in-app notifications need no credentials).

`signal.detected` and `lead.tier_changed` are matched against the active rules of the org; each match becomes
a `notification` row and, when the rule asks for e-mail and SMTP is configured, an e-mail to the rule's owner.
E-mail failures are recorded on the notification (`delivered.email = "failed"`), never raised: the in-app
copy must not be retried because SMTP was down.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog import get_logger

from leadradar_core.db.session import async_session_factory
from leadradar_core.integrations.alerts import send_email, smtp_configured
from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event
from leadradar_core.modules.alerts.service import Mailer, deliver_event
from leadradar_core.settings import AppSettings

log = get_logger(__name__)


def mailer_for(s: AppSettings) -> Mailer | None:
    """The e-mail sender for alert rules, None when SMTP is not configured (e-mail is then "skipped")."""
    if not smtp_configured(s):
        return None

    async def mailer(to: str, subject: str, lines: list[str]) -> None:
        await send_email(s, to, subject, lines)

    return mailer


class AlertRulesConsumer:
    name = "alerts.rules"
    event_types = frozenset({events.SIGNAL_DETECTED, events.LEAD_TIER_CHANGED})

    def __init__(
        self,
        s: AppSettings,
        session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
        mailer: Mailer | None = None,
    ) -> None:
        self._origin = s.PUBLIC_ORIGIN
        self._session_factory = session_factory
        self._mailer = mailer if mailer is not None else mailer_for(s)

    async def handle(self, event: Event) -> None:
        async with self._session_factory() as session, session.begin():
            created = await deliver_event(session, event, self._origin, self._mailer)
        if created:
            log.info("alert_rules_notified", event_id=str(event.id), notifications=len(created))


def alert_rules_consumers(s: AppSettings) -> list[AlertRulesConsumer]:
    return [AlertRulesConsumer(s)]
