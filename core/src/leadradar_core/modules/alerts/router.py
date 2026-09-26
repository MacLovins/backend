"""Alert rules, notifications and the one-click company watch. Everything is scoped to the current user."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.alerts.models import AlertRule, Notification
from leadradar_core.modules.alerts.schemas import (
    AlertRuleIn,
    AlertRuleOut,
    AlertRuleUpdate,
    NotificationOut,
    ReadAllOut,
    RulePreviewOut,
    UnreadCountOut,
)
from leadradar_core.modules.alerts.service import (
    preview_rule,
    rule_from_input,
    unwatch_company,
    watch_company,
)
from leadradar_core.settings import settings
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["alerts"])


async def _own_rule(session: AsyncSession, principal: Principal, rule_id: UUID) -> AlertRule:
    rule = await session.get(AlertRule, rule_id)
    if rule is None or rule.org_id != principal.org_id or rule.user_id != principal.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert rule not found")
    return rule


# --- rules -------------------------------------------------------------------------------------


@router.get("/alerts/rules", response_model=list[AlertRuleOut])
async def list_alert_rules(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[AlertRuleOut]:
    rows = (
        await session.execute(
            select(AlertRule)
            .where(AlertRule.org_id == principal.org_id, AlertRule.user_id == principal.user_id)
            .order_by(AlertRule.created_at.desc())
        )
    ).scalars()
    return [AlertRuleOut.model_validate(r) for r in rows]


@router.post("/alerts/rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
async def create_alert_rule(
    body: AlertRuleIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AlertRuleOut:
    rule = rule_from_input(principal.org_id, principal.user_id, body)
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.post("/alerts/rules/preview", response_model=RulePreviewOut)
async def preview_alert_rule(
    body: AlertRuleIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RulePreviewOut:
    """How often the rule would have fired over the last 30 days, with the first notifications rendered."""
    return await preview_rule(session, principal.org_id, principal.user_id, body, settings.PUBLIC_ORIGIN)


@router.patch("/alerts/rules/{rule_id}", response_model=AlertRuleOut)
async def update_alert_rule(
    rule_id: UUID,
    body: AlertRuleUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AlertRuleOut:
    rule = await _own_rule(session, principal, rule_id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    for field, value in changes.items():
        setattr(rule, field, value)
    await session.commit()
    await session.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.delete("/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert_rule(
    rule_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    rule = await _own_rule(session, principal, rule_id)
    await session.delete(rule)
    await session.commit()


# --- notifications -----------------------------------------------------------------------------


@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotificationOut]:
    stmt = select(Notification).where(
        Notification.org_id == principal.org_id, Notification.user_id == principal.user_id
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = (
        await session.execute(stmt.order_by(Notification.created_at.desc(), Notification.id).limit(limit))
    ).scalars()
    return [NotificationOut.model_validate(n) for n in rows]


@router.get("/notifications/unread-count", response_model=UnreadCountOut)
async def unread_count(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UnreadCountOut:
    count = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.org_id == principal.org_id,
            Notification.user_id == principal.user_id,
            Notification.read_at.is_(None),
        )
    )
    return UnreadCountOut(count=count or 0)


@router.post("/notifications/read-all", response_model=ReadAllOut)
async def read_all(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReadAllOut:
    result = await session.execute(
        update(Notification)
        .where(
            Notification.org_id == principal.org_id,
            Notification.user_id == principal.user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
    return ReadAllOut(updated=result.rowcount or 0)


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> NotificationOut:
    row = await session.get(Notification, notification_id)
    if row is None or row.org_id != principal.org_id or row.user_id != principal.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
    return NotificationOut.model_validate(row)


# --- company watch -----------------------------------------------------------------------------


async def _company(session: AsyncSession, principal: Principal, company_id: UUID) -> Company:
    company = await session.get(Company, company_id)
    if company is None or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company


@router.post("/companies/{id}/watch", response_model=AlertRuleOut)
async def watch_company_endpoint(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AlertRuleOut:
    """Creates or re-activates the "<company> — any signal" rule (in-app and e-mail)."""
    company = await _company(session, principal, id)
    rule = await watch_company(session, principal.org_id, principal.user_id, company)
    await session.commit()
    await session.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.delete("/companies/{id}/watch", status_code=status.HTTP_204_NO_CONTENT)
async def unwatch_company_endpoint(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await _company(session, principal, id)
    await unwatch_company(session, principal.org_id, principal.user_id, id)
    await session.commit()
