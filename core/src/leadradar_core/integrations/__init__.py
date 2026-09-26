"""Add-ons (ARCHITECTURE §4.13): consumers of the domain_event outbox.

User-defined alert rules are always on (in-app notifications need no credentials); the hardcoded hot-lead /
strong-signal channels and HubSpot stay behind their feature flags.
"""

from leadradar_core.integrations.alert_rules import alert_rules_consumers
from leadradar_core.integrations.alerts import alert_consumers
from leadradar_core.integrations.hubspot import hubspot_consumers
from leadradar_core.modules.activity.dispatcher import Consumer
from leadradar_core.settings import AppSettings, settings


def enabled_consumers(s: AppSettings = settings) -> list[Consumer]:
    """The consumer registry: alert rules always, the other add-ons when switched on with credentials."""
    return [*alert_rules_consumers(s), *alert_consumers(s), *hubspot_consumers(s)]
