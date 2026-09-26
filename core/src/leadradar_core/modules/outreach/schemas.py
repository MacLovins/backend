from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OutreachGenerateIn(BaseModel):
    service_id: UUID | None = None  # empty: the company's top-priority service, else any active service
    channel: str = "email"  # email / linkedin_inmail / call_script (anything else: email)
    language: str = "en"
    tone: str = "professional"  # professional / conversational / direct (anything else: professional)
    sender_name: str | None = None
    sender_title: str | None = None
    sender_company: str = "LeadRadar"


class OutreachDraftOut(BaseModel):
    channel: str
    subject: str | None = None
    body: str
    referenced_signals: list[UUID] = []
    referenced_quotes: list[str] = []
    hook: str | None = None
    call_to_action: str
    language: str = "en"


class OutreachJobOut(BaseModel):
    """Poll `GET /leads/{company_id}/outreach/{id}` until status is succeeded (draft set) or failed (error set)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    service_id: UUID | None = None
    status: str  # queued / running / succeeded / failed
    draft: OutreachDraftOut | None = None
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
