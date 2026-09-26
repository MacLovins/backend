from typing import Annotated
from uuid import UUID

import leadradar_ai as ai
from fastapi import APIRouter, Depends, HTTPException, status
from leadradar_auth.dependencies import get_current_principal, require_roles
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.config.models import (
    DisqualificationRule,
    ICPProfile,
    ScoringProfile,
    Service,
    SignalQuestion,
)
from leadradar_core.modules.config.presets import UnknownPreset, create_service_from_preset
from leadradar_core.modules.config.schemas import (
    DisqualificationRuleCreate,
    DisqualificationRuleOut,
    DisqualificationRuleUpdate,
    ICPProfileIn,
    ICPProfileOut,
    RescoreResult,
    ScoringProfileIn,
    ScoringProfileOut,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
    SignalQuestionCreate,
    SignalQuestionOut,
    SignalQuestionUpdate,
)
from leadradar_core.modules.intelligence.service import rescore_service
from leadradar_core.worker.enqueue import enqueue_expand
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["config"])

# a change of these fields changes what the question means: version + 1 and new keywords (SPEC core §1.7);
# the version is part of the extraction fingerprint, so a new temperature guide re-grades the evidence
MEANING_FIELDS = ("text", "polarity", "source_types", "recency_days", "category", "temperature")
SCORING_PARAMS = set(ai.ScoringProfile.model_fields) - {"id", "version"}


def validate_rule(kind: str, condition: dict, action: str, cap_value: object) -> None:
    """The AI engine's own validation, so a broken rule is rejected here instead of skipped at scoring."""
    try:
        ai.RuleConfig(
            id=UUID(int=0),
            name="check",
            kind=kind,
            condition=condition,
            action=action,
            cap_value=float(cap_value) if cap_value is not None else None,
        )
    except ValidationError as e:
        raise _unprocessable(e) from e


def _unprocessable(e: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=e.errors(include_url=False, include_context=False),
    )


def validate_question(q: SignalQuestion) -> None:
    """The stored question must map to ai.QuestionConfig, otherwise rescoring would fail later."""
    try:
        ai.QuestionConfig(
            id=UUID(int=0),
            key=q.key,
            version=max(q.version or 1, 1),
            text=q.text,
            category=q.category,
            polarity=q.polarity,
            weight=q.weight,
            source_types=set(q.source_types or []),
            recency_days=q.recency_days,
            temperature=q.temperature or {},
        )
    except ValidationError as e:
        raise _unprocessable(e) from e


def validate_icp(icp: ICPProfileIn) -> None:
    try:
        ai.ICPConfig(
            countries=icp.countries,
            industries_any=icp.industries_any,
            employees_min=icp.employees_min,
            employees_max=icp.employees_max,
            revenue_min_eur=int(icp.revenue_min_eur) if icp.revenue_min_eur is not None else None,
            nice_to_have=(icp.nice_to_have or {}).get("criteria", []),
        )
    except ValidationError as e:
        raise _unprocessable(e) from e


# --- Services ---
@router.get("/services", response_model=list[ServiceOut])
async def list_services(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ServiceOut]:
    stmt = select(Service).where(Service.org_id == principal.org_id)
    res = await session.execute(stmt)
    return [ServiceOut.model_validate(s) for s in res.scalars().all()]


@router.post(
    "/services",
    response_model=ServiceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("admin"))],
)
async def create_service(
    service_in: ServiceCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ServiceOut:
    service = Service(
        org_id=principal.org_id,
        name=service_in.name,
        slug=service_in.slug,
        description=service_in.description,
        value_proposition=service_in.value_proposition,
        decision_makers=service_in.decision_makers,
        is_active=service_in.is_active,
    )
    session.add(service)
    await session.commit()
    await session.refresh(service)
    return ServiceOut.model_validate(service)


@router.get("/services/{id}", response_model=ServiceOut)
async def get_service(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ServiceOut:
    service = await session.get(Service, id)
    if not service or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return ServiceOut.model_validate(service)


@router.patch(
    "/services/{id}",
    response_model=ServiceOut,
    dependencies=[Depends(require_roles("admin"))],
)
async def update_service(
    id: UUID,
    update_in: ServiceUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ServiceOut:
    service = await session.get(Service, id)
    if not service or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    for field, val in update_in.model_dump(exclude_unset=True).items():
        setattr(service, field, val)

    await session.commit()
    await session.refresh(service)
    return ServiceOut.model_validate(service)


# --- Presets Apply ---
@router.post(
    "/presets/{key}/apply",
    response_model=ServiceOut,
    dependencies=[Depends(require_roles("admin"))],
)
async def apply_preset(
    key: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ServiceOut:
    try:
        service, created = await create_service_from_preset(session, principal.org_id, key)
    except UnknownPreset as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown preset '{key}'") from e
    await session.commit()
    await session.refresh(service)
    if created:
        for q in (
            await session.execute(select(SignalQuestion).where(SignalQuestion.service_id == service.id))
        ).scalars():
            await enqueue_expand(q.id)
    return ServiceOut.model_validate(service)


# --- Questions ---
@router.get("/services/{id}/questions", response_model=list[SignalQuestionOut])
async def list_questions(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SignalQuestionOut]:
    stmt = select(SignalQuestion).where(
        SignalQuestion.service_id == id, SignalQuestion.org_id == principal.org_id
    )
    res = await session.execute(stmt)
    return [SignalQuestionOut.model_validate(q) for q in res.scalars().all()]


@router.post(
    "/services/{id}/questions",
    response_model=SignalQuestionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("admin"))],
)
async def create_question(
    id: UUID,
    q_in: SignalQuestionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SignalQuestionOut:
    service = await session.get(Service, id)
    if not service or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    existing = (
        await session.execute(
            select(SignalQuestion.id).where(SignalQuestion.service_id == id, SignalQuestion.key == q_in.key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Question key '{q_in.key}' already exists in this service (deleted questions keep their key)",
        )
    q = SignalQuestion(
        org_id=principal.org_id,
        service_id=id,
        key=q_in.key,
        text=q_in.text,
        category=q_in.category,
        polarity=q_in.polarity,
        weight=q_in.weight,
        source_types=q_in.source_types,
        recency_days=q_in.recency_days,
        job_titles=q_in.job_titles,
        negative_terms=q_in.negative_terms,
        temperature=q_in.temperature.model_dump() if q_in.temperature else None,
        keywords_status="pending",
        version=1,
        is_active=True,
    )
    validate_question(q)
    session.add(q)
    await session.commit()
    await session.refresh(q)
    await enqueue_expand(q.id)
    return SignalQuestionOut.model_validate(q)


@router.patch(
    "/questions/{id}",
    response_model=SignalQuestionOut,
    dependencies=[Depends(require_roles("admin"))],
)
async def update_question(
    id: UUID,
    q_in: SignalQuestionUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SignalQuestionOut:
    q = await session.get(SignalQuestion, id)
    if not q or q.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")

    changes = q_in.model_dump(exclude_unset=True)
    meaning_changed = any(
        field in changes and changes[field] != getattr(q, field) for field in MEANING_FIELDS
    )
    rescore = any(
        field in changes and changes[field] != getattr(q, field) for field in ("weight", "is_active")
    )
    for field, val in changes.items():
        setattr(q, field, val)
    validate_question(q)
    if meaning_changed:  # stale: new keywords now, new extraction on the next analysis (fingerprint)
        q.version += 1
        q.keywords_status = "pending"
    if rescore:  # weight or activity only change scoring: save and rescore in one transaction
        await rescore_service(session, principal.org_id, q.service_id)
    await session.commit()
    await session.refresh(q)
    if meaning_changed:
        await enqueue_expand(q.id)
    return SignalQuestionOut.model_validate(q)


@router.delete(
    "/questions/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles("admin"))],
)
async def delete_question(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Soft delete: the question stops counting, its signals and history stay (rescored in one transaction)."""
    q = await session.get(SignalQuestion, id)
    if q and q.org_id == principal.org_id and q.is_active:
        q.is_active = False
        await rescore_service(session, principal.org_id, q.service_id)
        await session.commit()


@router.post(
    "/questions/{id}/expand",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_roles("admin"))],
)
async def expand_question(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    q = await session.get(SignalQuestion, id)
    if not q or q.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    q.keywords_status = "pending"
    await session.commit()
    enqueued = await enqueue_expand(id)
    return {"status": "enqueued" if enqueued else "pending", "question_id": str(id)}


# --- ICP ---
@router.get("/services/{id}/icp", response_model=ICPProfileOut)
async def get_icp(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ICPProfileOut:
    stmt = select(ICPProfile).where(ICPProfile.service_id == id, ICPProfile.org_id == principal.org_id)
    res = await session.execute(stmt)
    icp = res.scalar_one_or_none()
    if not icp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ICP not configured")
    return ICPProfileOut.model_validate(icp)


@router.put(
    "/services/{id}/icp",
    response_model=ICPProfileOut,
    dependencies=[Depends(require_roles("admin"))],
)
async def put_icp(
    id: UUID,
    icp_in: ICPProfileIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ICPProfileOut:
    validate_icp(icp_in)
    stmt = select(ICPProfile).where(ICPProfile.service_id == id, ICPProfile.org_id == principal.org_id)
    res = await session.execute(stmt)
    icp = res.scalar_one_or_none()

    if icp:
        icp.countries = icp_in.countries
        icp.industries_any = icp_in.industries_any
        icp.employees_min = icp_in.employees_min
        icp.employees_max = icp_in.employees_max
        icp.revenue_min_eur = icp_in.revenue_min_eur
        icp.nice_to_have = icp_in.nice_to_have
        icp.version += 1
    else:
        icp = ICPProfile(
            org_id=principal.org_id,
            service_id=id,
            countries=icp_in.countries,
            industries_any=icp_in.industries_any,
            employees_min=icp_in.employees_min,
            employees_max=icp_in.employees_max,
            revenue_min_eur=icp_in.revenue_min_eur,
            nice_to_have=icp_in.nice_to_have,
            version=1,
        )
        session.add(icp)

    await rescore_service(session, principal.org_id, id)
    await session.commit()
    await session.refresh(icp)
    return ICPProfileOut.model_validate(icp)


# --- Disqualification Rules ---
@router.get("/services/{id}/rules", response_model=list[DisqualificationRuleOut])
async def list_rules(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[DisqualificationRuleOut]:
    stmt = select(DisqualificationRule).where(
        DisqualificationRule.service_id == id, DisqualificationRule.org_id == principal.org_id
    )
    res = await session.execute(stmt)
    return [DisqualificationRuleOut.model_validate(r) for r in res.scalars().all()]


@router.post(
    "/services/{id}/rules",
    response_model=DisqualificationRuleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("admin"))],
)
async def create_rule(
    id: UUID,
    rule_in: DisqualificationRuleCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DisqualificationRuleOut:
    validate_rule(rule_in.kind, rule_in.condition, rule_in.action, rule_in.cap_value)
    rule = DisqualificationRule(
        org_id=principal.org_id,
        service_id=id,
        name=rule_in.name,
        kind=rule_in.kind,
        condition=rule_in.condition,
        action=rule_in.action,
        cap_value=rule_in.cap_value,
        is_active=rule_in.is_active,
    )
    session.add(rule)
    await rescore_service(session, principal.org_id, id)
    await session.commit()
    await session.refresh(rule)
    return DisqualificationRuleOut.model_validate(rule)


@router.patch(
    "/rules/{id}",
    response_model=DisqualificationRuleOut,
    dependencies=[Depends(require_roles("admin"))],
)
async def update_rule(
    id: UUID,
    rule_in: DisqualificationRuleUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DisqualificationRuleOut:
    rule = await session.get(DisqualificationRule, id)
    if not rule or rule.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

    for field, val in rule_in.model_dump(exclude_unset=True).items():
        setattr(rule, field, val)
    validate_rule(rule.kind, rule.condition, rule.action, rule.cap_value)

    await rescore_service(session, principal.org_id, rule.service_id)
    await session.commit()
    await session.refresh(rule)
    return DisqualificationRuleOut.model_validate(rule)


@router.delete(
    "/rules/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles("admin"))],
)
async def delete_rule(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    rule = await session.get(DisqualificationRule, id)
    if rule and rule.org_id == principal.org_id:
        service_id = rule.service_id
        await session.delete(rule)
        await rescore_service(session, principal.org_id, service_id)
        await session.commit()


# --- Scoring Profile ---
@router.get("/services/{id}/scoring-profile", response_model=ScoringProfileOut)
async def get_scoring_profile(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ScoringProfileOut:
    stmt = (
        select(ScoringProfile)
        .where(
            ScoringProfile.service_id == id,
            ScoringProfile.org_id == principal.org_id,
            ScoringProfile.is_current == True,  # noqa: E712
        )
        .order_by(ScoringProfile.version.desc())
    )
    res = await session.execute(stmt)
    profile = res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scoring profile not found")
    return ScoringProfileOut.model_validate(profile)


@router.put(
    "/services/{id}/scoring-profile",
    response_model=RescoreResult,
    dependencies=[Depends(require_roles("admin"))],
)
async def put_scoring_profile(
    id: UUID,
    profile_in: ScoringProfileIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RescoreResult:
    unknown = set(profile_in.params) - SCORING_PARAMS
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown scoring parameters: {sorted(unknown)}",
        )
    try:
        ai.ScoringProfile(id=UUID(int=0), version=1, **profile_in.params)
    except ValidationError as e:
        raise _unprocessable(e) from e

    # Set all existing profiles for this service to is_current=False
    await session.execute(
        update(ScoringProfile)
        .where(ScoringProfile.service_id == id, ScoringProfile.org_id == principal.org_id)
        .values(is_current=False)
    )

    # Get max version
    stmt = (
        select(ScoringProfile.version)
        .where(ScoringProfile.service_id == id, ScoringProfile.org_id == principal.org_id)
        .order_by(ScoringProfile.version.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    current_ver = res.scalar_one_or_none() or 0
    new_version = current_ver + 1

    profile = ScoringProfile(
        org_id=principal.org_id,
        service_id=id,
        version=new_version,
        params=profile_in.params,
        is_current=True,
    )
    session.add(profile)
    result = await rescore_service(session, principal.org_id, id)
    await session.commit()
    return RescoreResult(version=new_version, **result)
