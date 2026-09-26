from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class HubSpotStatus(BaseModel):
    enabled: bool


class HubSpotPushIn(BaseModel):
    # None: the service where the lead scores highest
    service_id: UUID | None = None


class HubSpotPushOut(BaseModel):
    hubspot_company_id: str
    created: bool
    note_id: str
    record_url: str | None
    synced_at: datetime
