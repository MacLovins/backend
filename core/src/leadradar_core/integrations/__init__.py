"""Add-ons (ARCHITECTURE §4.13): each one is a consumer of the domain_event outbox behind its own feature flag."""

from leadradar_core.integrations.alerts import alert_consumers
from leadradar_core.integrations.hubspot import hubspot_consumers
from leadradar_core.modules.activity.dispatcher import Consumer
from leadradar_core.settings import AppSettings, settings


def enabled_consumers(s: AppSettings = settings) -> list[Consumer]:
    """The consumer registry: empty unless an add-on is switched on and has its credentials."""
    return [*alert_consumers(s), *hubspot_consumers(s)]
