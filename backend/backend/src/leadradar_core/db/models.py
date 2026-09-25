"""Central registry of all core models for SQLAlchemy metadata discovery."""

from leadradar_core.db.base import Base, core_metadata
from leadradar_core.modules.accounts.models import Company, Org
from leadradar_core.modules.activity.models import DomainEvent
from leadradar_core.modules.config.models import (
    DisqualificationRule,
    ICPProfile,
    ScoringProfile,
    Service,
    SignalQuestion,
)
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.intelligence.models import (
    Document,
    DocumentChunk,
    ExtractionState,
    RejectedEvidence,
    Signal,
)
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.meta.models import LLMCache, LLMCall
from leadradar_core.modules.runs.models import AnalysisRun, RunEvent

__all__ = [
    "AnalysisRun",
    "Base",
    "Company",
    "DisqualificationRule",
    "Document",
    "DocumentChunk",
    "DomainEvent",
    "ExtractionState",
    "Feedback",
    "ICPProfile",
    "LLMCall",
    "LLMCache",
    "LeadScore",
    "Org",
    "RejectedEvidence",
    "RunEvent",
    "ScoringProfile",
    "Service",
    "Signal",
    "SignalQuestion",
    "core_metadata",
]
