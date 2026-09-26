"""Alert rules: matching events against rules, rendering notifications, delivering them, the preview, the
jobs-postings threshold and the e-mail digests (both evaluated by the scheduler, not by events).

Delivery is channel-agnostic here: `notify()` stores the in-app row and hands e-mails to a `Mailer`
callable that the caller provides (the SMTP path lives in integrations.alerts), so this module has no
network code and in-app notifications work without SMTP. A rule with a digest frequency queues its e-mails
(`delivered.email = "queued"`); `send_email_digests()` sends them later, one e-mail per user.
"""

import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from leadradar_auth import UserAccount
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event
from leadradar_core.modules.activity.models import DomainEvent
from leadradar_core.modules.alerts.models import AlertRule, Notification
from leadradar_core.modules.alerts.schemas import (
    DEFAULT_JOBS_WINDOW_H,
    AlertRuleIn,
    NotificationPreview,
    RulePreviewOut,
)
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.modules.leads.trends import (
    STRENGTH_RANK,
    TREND_LABELS,
    WHY_IT_MATTERS,
    lead_link,
    trend_kind,
)
from leadradar_core.settings import settings
from sqlalchemy import ColumnElement, Text, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog import get_logger

log = get_logger(__name__)

Mailer = Callable[[str, str, list[str]], Awaitable[None]]  # (to, subject, lines)

PREVIEW_WINDOW = timedelta(days=30)
PREVIEW_SAMPLE = 10
PREVIEW_SCAN_LIMIT = 5000
WATCH_CHANNELS = ["inapp", "email"]
DIGEST_MAX_ITEMS = 50  # notifications listed in one digest e-mail; the rest are counted
DIGEST_SUBJECT_NAMES = 2  # company names in the digest subject


@dataclass(frozen=True)
class Draft:
    """A rendered notification before it is stored."""

    company_id: UUID
    service_id: UUID | None
    kind: str
    trend_kind: str | None
    title: str
    body: str
    url: str
    event_id: UUID | None = None
    occurred_at: datetime | None = None
    category: str | None = None  # signal: the question's category and the signal's strength
    strength: str | None = None


@dataclass(frozen=True)
class Firmographics:
    """What a firmographic scope is matched against: the company's country, industries and size."""

    country_code: str | None = None
    industry_ids: tuple[str, ...] = ()
    employees: int | None = None


# --- rendering ---------------------------------------------------------------------------------


def lead_url(public_origin: str, company_id: UUID | str, service_id: UUID | str | None) -> str:
    return lead_link(public_origin, company_id, service_id)


def _label(kind: str) -> str:
    return TREND_LABELS.get(kind) or kind.replace("_", " ").capitalize()


def _gist(text: str | None, limit: int = 90) -> str:
    """First sentence of the summary, shortened for a title."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def render_signal(event: Event, public_origin: str) -> Draft:
    p = event.payload
    kind = trend_kind(p.get("category"), p.get("summary"), p.get("quote"))
    company = p.get("company_name") or p.get("domain") or "a company"
    label = _label(kind) if kind else _label(p.get("category") or "new")
    gist = _gist(p.get("summary"))
    title = f"{label} at {company}: {gist}" if gist else f"{label} signal at {company}"
    lines = [p.get("summary") or ""]
    if p.get("quote"):
        source = f" — {p['source_name']}" if p.get("source_name") else ""
        lines.append(f'"{p["quote"]}"{source}')
    why = WHY_IT_MATTERS.get(kind or "")
    if why:
        lines.append(f"Why it matters: {why}")
    return Draft(
        company_id=UUID(p["company_id"]),
        service_id=UUID(p["service_id"]) if p.get("service_id") else None,
        kind="signal",
        trend_kind=kind,
        title=title,
        body="\n".join(line for line in lines if line),
        url=lead_url(public_origin, p["company_id"], p.get("service_id")),
        event_id=event.id,
        occurred_at=event.created_at,
        category=p.get("category"),
        strength=p.get("strength") if p.get("strength") in STRENGTH_RANK else None,
    )


def render_tier(event: Event, public_origin: str) -> Draft:
    p = event.payload
    company = p.get("company_name") or p.get("domain") or "a company"
    service = p.get("service_name") or "the service"
    after = str(p.get("tier_after") or "").upper()
    title = f"{company} is now {after} for {service}"
    lines = [
        f"Tier {p.get('tier_before') or 'new'} → {p.get('tier_after')}; priority {p.get('priority')} "
        f"(fit {p.get('fit')}, intent {p.get('intent')}, risk {p.get('risk')})."
    ]
    why = [r.get("text") for r in (p.get("why_now") or []) if isinstance(r, dict) and r.get("text")]
    if why:
        lines.append("Why now: " + " ".join(why))
    return Draft(
        company_id=UUID(p["company_id"]),
        service_id=UUID(p["service_id"]) if p.get("service_id") else None,
        kind="tier",
        trend_kind=None,
        title=title,
        body="\n".join(lines),
        url=lead_url(public_origin, p["company_id"], p.get("service_id")),
        event_id=event.id,
        occurred_at=event.created_at,
    )


def render_jobs(
    company: Company, service_id: UUID | None, count: int, window_h: int, jobs_min: int, public_origin: str
) -> Draft:
    return Draft(
        company_id=company.id,
        service_id=service_id,
        kind="jobs_threshold",
        trend_kind="hiring",
        title=f"Hiring spike at {company.name}: {count} job postings",
        body=(
            f"{count} job postings appeared in the last {window_h} h (your threshold: {jobs_min}).\n"
            f"Why it matters: {WHY_IT_MATTERS['hiring']}"
        ),
        url=lead_url(public_origin, company.id, service_id),
        occurred_at=datetime.now(UTC),
    )


# --- matching ----------------------------------------------------------------------------------


def _rank(strength: str | None) -> int:
    return STRENGTH_RANK.get(strength or "", 0)


def has_firmographic_scope(scope: dict[str, Any] | None) -> bool:
    """The scope limits companies by country, industry or size (null or empty = any)."""
    scope = scope or {}
    return bool(scope.get("countries") or scope.get("industries")) or (
        scope.get("employees_min") is not None or scope.get("employees_max") is not None
    )


def firmographics_match(scope: dict[str, Any], firmo: Firmographics | None) -> bool:
    """Unknown data never matches a limit: a company without country (or size) is outside a scope that limits
    the country (or size); without the company (`firmo` None) only a scope without such limits matches."""
    if not has_firmographic_scope(scope):
        return True
    if firmo is None:
        return False
    countries, industries = scope.get("countries"), scope.get("industries")
    lo, hi = scope.get("employees_min"), scope.get("employees_max")
    if countries and (firmo.country_code or "").upper() not in countries:
        return False
    if industries and not set(industries) & set(firmo.industry_ids):
        return False
    if lo is not None or hi is not None:
        if firmo.employees is None:
            return False
        if (lo is not None and firmo.employees < lo) or (hi is not None and firmo.employees > hi):
            return False
    return True


def firmographic_conditions(scope: dict[str, Any]) -> list[ColumnElement[bool]]:
    """The same firmographic scope as SQL conditions on Company (NULL country or employees match no limit)."""
    conditions: list[ColumnElement[bool]] = []
    if scope.get("countries"):
        conditions.append(Company.country_code.in_(list(scope["countries"])))
    if scope.get("industries"):
        conditions.append(Company.industry_ids.overlap(list(scope["industries"])))
    if scope.get("employees_min") is not None:
        conditions.append(Company.employees >= int(scope["employees_min"]))
    if scope.get("employees_max") is not None:
        conditions.append(Company.employees <= int(scope["employees_max"]))
    return conditions


def in_scope(
    rule: AlertRule, company_id: str | None, service_id: str | None, firmo: Firmographics | None = None
) -> bool:
    scope = rule.scope or {}
    companies = scope.get("company_ids")
    services = scope.get("service_ids")
    if companies is not None and company_id not in {str(c) for c in companies}:
        return False
    if services is not None and service_id not in {str(s) for s in services}:
        return False
    return firmographics_match(scope, firmo)


def matches_trigger(rule: AlertRule, event: Event) -> bool:
    trigger = rule.trigger or {}
    kind = trigger.get("kind")
    p = event.payload
    if kind == "signal" and event.type == events.SIGNAL_DETECTED:
        categories = trigger.get("categories")
        if categories is not None:
            if trend_kind(p.get("category"), p.get("summary"), p.get("quote")) not in categories:
                return False
        if trigger.get("polarity") and p.get("polarity") != trigger["polarity"]:
            return False
        min_strength = trigger.get("min_strength")
        if min_strength and _rank(p.get("strength")) < _rank(min_strength):
            return False
        levels = trigger.get("levels")  # {signal category: minimum strength}; other categories do not fire
        if levels:
            level = levels.get(p.get("category") or "")
            if level is None or _rank(p.get("strength")) < _rank(level):
                return False
        return True
    if kind == "tier" and event.type == events.LEAD_TIER_CHANGED:
        tier_to = trigger.get("tier_to")
        return tier_to is None or p.get("tier_after") in tier_to
    return False


def match_event(
    rule: AlertRule, event: Event, public_origin: str, firmo: Firmographics | None = None
) -> Draft | None:
    """The rendered notification when the rule fires for the event, else None.

    `firmo`: the event's company, needed by a rule with a firmographic scope (without it such a rule is
    silent).
    """
    if not rule.is_active or event.org_id != rule.org_id:
        return None
    p = event.payload
    if not p.get("company_id") or not in_scope(rule, p.get("company_id"), p.get("service_id"), firmo):
        return None
    if not matches_trigger(rule, event):
        return None
    if event.type == events.SIGNAL_DETECTED:
        return render_signal(event, public_origin)
    return render_tier(event, public_origin)


async def active_rules(
    session: AsyncSession, org_id: UUID, kinds: Sequence[str] | None = None
) -> list[AlertRule]:
    stmt = select(AlertRule).where(AlertRule.org_id == org_id, AlertRule.is_active.is_(True))
    if kinds is not None:
        stmt = stmt.where(AlertRule.trigger["kind"].astext.in_(list(kinds)))
    return list((await session.execute(stmt.order_by(AlertRule.created_at))).scalars().all())


async def company_firmographics(
    session: AsyncSession, org_id: UUID, company_ids: Iterable[Any]
) -> dict[str, Firmographics]:
    """{company id (str): firmographics} of the org's companies among `company_ids`, in one query."""
    ids: set[UUID] = set()
    for value in company_ids:
        try:
            ids.add(UUID(str(value)))
        except ValueError:
            continue
    if not ids:
        return {}
    rows = await session.execute(
        select(Company.id, Company.country_code, Company.industry_ids, Company.employees).where(
            Company.org_id == org_id, Company.id.in_(ids)
        )
    )
    return {
        str(company_id): Firmographics(country, tuple(industries or ()), employees)
        for company_id, country, industries, employees in rows.all()
    }


# --- delivery ----------------------------------------------------------------------------------


async def user_email(session: AsyncSession, user_id: UUID) -> str | None:
    return (
        await session.execute(
            select(UserAccount.email).where(UserAccount.id == user_id, UserAccount.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def notify(
    session: AsyncSession, rule: AlertRule, draft: Draft, mailer: Mailer | None
) -> Notification | None:
    """Stores the in-app notification and e-mails it when the rule asks for it and a mailer is available.

    Idempotent per (rule, event): a re-delivered event does not create a second notification.
    """
    if draft.event_id is not None:
        duplicate = await session.scalar(
            select(exists().where(Notification.rule_id == rule.id, Notification.event_id == draft.event_id))
        )
        if duplicate:
            return None
    row = Notification(
        id=uuid4(),
        org_id=rule.org_id,
        user_id=rule.user_id,
        rule_id=rule.id,
        event_id=draft.event_id,
        company_id=draft.company_id,
        service_id=draft.service_id,
        kind=draft.kind,
        trend_kind=draft.trend_kind,
        category=draft.category,
        strength=draft.strength,
        title=draft.title,
        body=draft.body,
        url=draft.url,
        delivered={},
    )
    if "email" in (rule.channels or []):
        if (rule.email_frequency or "instant") == "instant":
            row.delivered = {"email": await _send_email(session, rule, draft, mailer)}
        else:
            row.delivered = {"email": "queued"}  # sent with the rule's next digest (send_email_digests)
    session.add(row)
    await session.flush()
    return row


async def _send_email(session: AsyncSession, rule: AlertRule, draft: Draft, mailer: Mailer | None) -> str:
    if mailer is None:
        return "skipped"
    to = await user_email(session, rule.user_id)
    if not to:
        log.info("alert_email_skipped", rule_id=str(rule.id), reason="no active user e-mail")
        return "skipped"
    try:
        await mailer(to, f"[LeadRadar] {draft.title}", [*draft.body.splitlines(), draft.url])
    except Exception as e:
        log.warning("alert_email_failed", rule_id=str(rule.id), error=f"{type(e).__name__}: {e}")
        return "failed"
    return "sent"


async def deliver_event(
    session: AsyncSession, event: Event, public_origin: str, mailer: Mailer | None
) -> list[Notification]:
    """All notifications of one event: every active rule of the org that matches, one row per rule."""
    if event.type not in (events.SIGNAL_DETECTED, events.LEAD_TIER_CHANGED):
        return []
    rules = await active_rules(session, event.org_id, ("signal", "tier"))
    firmo = None
    if any(has_firmographic_scope(r.scope) for r in rules):  # the company is loaded only when a rule needs it
        company_id = event.payload.get("company_id")
        firmo = (await company_firmographics(session, event.org_id, [company_id])).get(str(company_id))
    created = []
    for rule in rules:
        draft = match_event(rule, event, public_origin, firmo)
        if draft is None:
            continue
        row = await notify(session, rule, draft, mailer)
        if row is not None:
            created.append(row)
    return created


# --- preview -----------------------------------------------------------------------------------


def rule_from_input(org_id: UUID, user_id: UUID, body: AlertRuleIn) -> AlertRule:
    return AlertRule(
        id=uuid4(),
        org_id=org_id,
        user_id=user_id,
        name=body.name,
        is_active=body.is_active,
        channels=list(body.channels),
        scope=body.scope.model_dump(mode="json"),
        trigger=body.trigger.model_dump(mode="json"),
        email_frequency=body.email_frequency,
    )


def _preview(draft: Draft) -> NotificationPreview:
    return NotificationPreview(
        company_id=draft.company_id,
        service_id=draft.service_id,
        kind=draft.kind,
        trend_kind=draft.trend_kind,
        category=draft.category,
        strength=draft.strength,
        title=draft.title,
        body=draft.body,
        url=draft.url,
        occurred_at=draft.occurred_at or datetime.now(UTC),
    )


async def preview_rule(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    body: AlertRuleIn,
    public_origin: str,
    now: datetime | None = None,
) -> RulePreviewOut:
    """What the rule would have produced over the last 30 days (events replayed; jobs counted as of now)."""
    now = now or datetime.now(UTC)
    rule = rule_from_input(org_id, user_id, body)
    rule.is_active = True
    kind = body.trigger.kind
    if kind == "jobs_threshold":
        drafts = await jobs_threshold_drafts(session, rule, public_origin, now)
        return RulePreviewOut(count=len(drafts), notifications=[_preview(d) for d in drafts[:PREVIEW_SAMPLE]])
    event_type = events.SIGNAL_DETECTED if kind == "signal" else events.LEAD_TIER_CHANGED
    stmt = (
        select(DomainEvent)
        .where(
            DomainEvent.org_id == org_id,
            DomainEvent.type == event_type,
            DomainEvent.created_at >= now - PREVIEW_WINDOW,
        )
        .order_by(DomainEvent.created_at.desc())
        .limit(PREVIEW_SCAN_LIMIT)
    )
    rows = (await session.execute(stmt)).scalars().all()
    firmos: dict[str, Firmographics] = {}
    if has_firmographic_scope(rule.scope):  # the companies of the scanned events, in one query
        firmos = await company_firmographics(session, org_id, {r.payload.get("company_id") for r in rows})
    count = 0
    sample: list[NotificationPreview] = []
    for row in rows:
        event = Event(
            id=row.id, org_id=row.org_id, type=row.type, payload=dict(row.payload), created_at=row.created_at
        )
        draft = match_event(rule, event, public_origin, firmos.get(str(event.payload.get("company_id"))))
        if draft is None:
            continue
        count += 1
        if len(sample) < PREVIEW_SAMPLE:
            sample.append(_preview(draft))
    return RulePreviewOut(count=count, notifications=sample)


# --- jobs threshold ----------------------------------------------------------------------------


async def jobs_threshold_drafts(
    session: AsyncSession, rule: AlertRule, public_origin: str, now: datetime
) -> list[Draft]:
    """Companies in the rule's scope (firmographics included) whose job postings in the window reach the
    threshold."""
    trigger = rule.trigger or {}
    jobs_min = int(trigger.get("jobs_min") or 1)
    window_h = int(trigger.get("jobs_window_h") or DEFAULT_JOBS_WINDOW_H)
    scope = rule.scope or {}
    service_ids = scope.get("service_ids")
    service_id = UUID(str(service_ids[0])) if service_ids else None
    stmt = (
        select(Company, func.count(Document.id).label("jobs"))
        .join(Document, Document.company_id == Company.id)
        .where(
            Company.org_id == rule.org_id,
            Document.source_type == "jobs",
            Document.published_at >= now - timedelta(hours=window_h),
            *firmographic_conditions(scope),
        )
        .group_by(Company.id)
        .having(func.count(Document.id) >= jobs_min)
        .order_by(func.count(Document.id).desc(), Company.name)
    )
    if scope.get("company_ids") is not None:
        stmt = stmt.where(Company.id.in_([UUID(str(c)) for c in scope["company_ids"]]))
    return [
        render_jobs(company, service_id, int(count), window_h, jobs_min, public_origin)
        for company, count in (await session.execute(stmt)).all()
    ]


async def evaluate_jobs_thresholds(
    session_factory: async_sessionmaker[AsyncSession],
    public_origin: str,
    mailer: Mailer | None,
    *,
    now: datetime | None = None,
    only_org: UUID | None = None,
) -> list[Notification]:
    """One pass over the jobs_threshold rules; a (rule, company) is notified at most once per window."""
    now = now or datetime.now(UTC)
    created: list[Notification] = []
    async with session_factory() as session, session.begin():
        stmt = select(AlertRule).where(
            AlertRule.is_active.is_(True), AlertRule.trigger["kind"].astext == "jobs_threshold"
        )
        if only_org is not None:
            stmt = stmt.where(AlertRule.org_id == only_org)
        for rule in (await session.execute(stmt.order_by(AlertRule.created_at))).scalars().all():
            window_h = int((rule.trigger or {}).get("jobs_window_h") or DEFAULT_JOBS_WINDOW_H)
            since = now - timedelta(hours=window_h)
            for draft in await jobs_threshold_drafts(session, rule, public_origin, now):
                already = await session.scalar(
                    select(
                        exists().where(
                            Notification.rule_id == rule.id,
                            Notification.company_id == draft.company_id,
                            Notification.created_at >= since,
                        )
                    )
                )
                if already:
                    continue
                row = await notify(session, rule, draft, mailer)
                if row is not None:
                    created.append(row)
    return created


# --- e-mail digests ----------------------------------------------------------------------------

_QUEUED = Notification.delivered["email"].astext == "queued"


def render_digest(items: Sequence[tuple[Notification, str]], public_origin: str) -> tuple[str, list[str]]:
    """(subject, lines) of one user's digest; `items` are (notification, company name), newest first."""
    count = len(items)
    names = list(dict.fromkeys(name for _, name in items))
    companies = " and ".join(names)
    if len(names) > DIGEST_SUBJECT_NAMES:
        companies = f"{', '.join(names[:DIGEST_SUBJECT_NAMES])} and {len(names) - DIGEST_SUBJECT_NAMES} more"
    subject = f"[LeadRadar] {count} new signal{'' if count == 1 else 's'}: {companies}"
    lines: list[str] = []
    for notification, _ in items[:DIGEST_MAX_ITEMS]:
        lines += [notification.title, notification.url, ""]
    if count > DIGEST_MAX_ITEMS:
        lines += [f"and {count - DIGEST_MAX_ITEMS} more", ""]
    lines.append(f"All notifications: {public_origin.rstrip('/')}")
    return subject, lines


async def _send_digest(
    session: AsyncSession,
    user_id: UUID,
    items: Sequence[tuple[Notification, str]],
    public_origin: str,
    mailer: Mailer | None,
) -> str:
    if mailer is None:
        return "skipped"
    to = await user_email(session, user_id)
    if not to:
        log.info("alert_digest_skipped", user_id=str(user_id), reason="no active user e-mail")
        return "skipped"
    subject, lines = render_digest(items, public_origin)
    try:
        await mailer(to, subject, lines)
    except Exception as e:
        log.warning("alert_digest_failed", user_id=str(user_id), error=f"{type(e).__name__}: {e}")
        return "failed"
    return "sent"


async def send_email_digests(
    session_factory: async_sessionmaker[AsyncSession],
    public_origin: str,
    mailer: Mailer | None,
    *,
    now: datetime | None = None,
    only_org: UUID | None = None,
) -> int:
    """One digest run: the queued e-mails of the rules that are due — "twice_daily" at every run, "daily"
    at the run in hour APP_ALERTS_DAILY_DIGEST_HOUR (UTC) — as one e-mail per user; returns the number of
    e-mails sent.

    Every included notification gets `delivered.email` "sent", "failed" or "skipped" (no mailer, no active
    user e-mail). The queue of a rule that is gone, inactive or no longer e-mails is dropped ("skipped"); a
    rule switched back to "instant" has its queue sent with the next run.
    """
    now = now or datetime.now(UTC)
    daily_due = now.astimezone(UTC).hour == settings.ALERTS_DAILY_DIGEST_HOUR
    users = select(Notification.user_id).where(_QUEUED).distinct()
    if only_org is not None:
        users = users.where(Notification.org_id == only_org)
    async with session_factory() as session:
        user_ids = list((await session.execute(users)).scalars().all())
    sent = 0
    for user_id in user_ids:  # a transaction per user: the rows are marked together with their e-mail
        async with session_factory() as session, session.begin():
            outcome = await _user_digest(session, user_id, daily_due, public_origin, mailer, only_org)
        if outcome == "sent":
            sent += 1
    return sent


async def _user_digest(
    session: AsyncSession,
    user_id: UUID,
    daily_due: bool,
    public_origin: str,
    mailer: Mailer | None,
    only_org: UUID | None,
) -> str | None:
    """The user's digest of the due queued e-mails; the delivery outcome, None when nothing is due."""
    stmt = (
        select(Notification, AlertRule, Company.name)
        .join(Company, Company.id == Notification.company_id)
        .outerjoin(AlertRule, AlertRule.id == Notification.rule_id)
        .where(Notification.user_id == user_id, _QUEUED)
        .order_by(Notification.created_at.desc(), Notification.id)
        .with_for_update(of=Notification, skip_locked=True)  # a concurrent run skips these rows
    )
    if only_org is not None:
        stmt = stmt.where(Notification.org_id == only_org)
    items: list[tuple[Notification, str]] = []
    for notification, rule, company_name in (await session.execute(stmt)).all():
        if rule is None or not rule.is_active or "email" not in (rule.channels or []):
            notification.delivered = {**notification.delivered, "email": "skipped"}
        elif rule.email_frequency != "daily" or daily_due:
            items.append((notification, company_name))
    if not items:
        return None
    outcome = await _send_digest(session, user_id, items, public_origin, mailer)
    for notification, _ in items:
        notification.delivered = {**notification.delivered, "email": outcome}
    return outcome


# --- watch (one-click rule per company) --------------------------------------------------------


def _single_company_scope(company_id: UUID):
    """Rules whose scope is exactly this one company."""
    company_ids = AlertRule.scope["company_ids"]
    return (
        (func.jsonb_typeof(company_ids) == "array")
        & (func.jsonb_array_length(company_ids) == 1)
        & (company_ids.has_key(str(company_id)))
    )


async def watch_company(session: AsyncSession, org_id: UUID, user_id: UUID, company: Company) -> AlertRule:
    """Creates (or re-activates) the user's "<company> — any signal" rule."""
    stmt = (
        select(AlertRule)
        .where(
            AlertRule.org_id == org_id,
            AlertRule.user_id == user_id,
            AlertRule.trigger["kind"].astext == "signal",
            _single_company_scope(company.id),
        )
        .order_by(AlertRule.is_active.desc(), AlertRule.created_at)
    )
    rule = (await session.execute(stmt)).scalars().first()
    if rule is None:
        rule = AlertRule(
            id=uuid4(),
            org_id=org_id,
            user_id=user_id,
            name=f"{company.name} — any signal",
            channels=list(WATCH_CHANNELS),
            scope={"company_ids": [str(company.id)], "service_ids": None},
            trigger={"kind": "signal"},
            email_frequency="instant",
        )
        session.add(rule)
    rule.is_active = True
    await session.flush()
    return rule


async def unwatch_company(session: AsyncSession, org_id: UUID, user_id: UUID, company_id: UUID) -> int:
    """Deactivates the user's rules scoped to exactly this company; returns how many."""
    rows = (
        (
            await session.execute(
                select(AlertRule).where(
                    AlertRule.org_id == org_id,
                    AlertRule.user_id == user_id,
                    AlertRule.is_active.is_(True),
                    _single_company_scope(company_id),
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rows:
        rule.is_active = False
    await session.flush()
    return len(rows)


def watched_expr(user_id: UUID, company_id_column: Any):
    """SQL: the user has an active rule whose scope names the company (usable inside a bigger query)."""
    return exists().where(
        AlertRule.user_id == user_id,
        AlertRule.is_active.is_(True),
        AlertRule.scope["company_ids"].has_key(cast(company_id_column, Text)),
    )


async def watched_company_ids(session: AsyncSession, org_id: UUID, user_id: UUID) -> set[UUID]:
    """Every company id named in the user's active rules (companies of the org only)."""
    rows = (
        await session.execute(
            select(AlertRule.scope["company_ids"]).where(
                AlertRule.org_id == org_id,
                AlertRule.user_id == user_id,
                AlertRule.is_active.is_(True),
                AlertRule.scope["company_ids"].astext.is_not(None),
            )
        )
    ).scalars()
    ids: set[UUID] = set()
    for value in rows:
        for c in value or []:
            try:
                ids.add(UUID(str(c)))
            except ValueError:
                continue
    return ids
