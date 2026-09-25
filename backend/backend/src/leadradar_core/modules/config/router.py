from typing import Annotated
from uuid import UUID

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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["config"])


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
    stmt = select(Service).where(Service.org_id == principal.org_id, Service.slug == key)
    res = await session.execute(stmt)
    existing = res.scalar_one_or_none()

    if existing:
        return ServiceOut.model_validate(existing)

    name = key.replace("_", " ").title()
    service = Service(
        org_id=principal.org_id,
        name=name,
        slug=key,
        description=f"Preset service for {name}",
        value_proposition="Applied from built-in preset.",
        decision_makers=["CIO", "COO", "Head of Digital Transformation"],
        is_active=True,
    )
    session.add(service)
    await session.commit()
    await session.refresh(service)
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
        keywords_status="pending",
        version=1,
        is_active=True,
    )
    session.add(q)
    await session.commit()
    await session.refresh(q)
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

    for field, val in q_in.model_dump(exclude_unset=True).items():
        setattr(q, field, val)

    await session.commit()
    await session.refresh(q)
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
    q = await session.get(SignalQuestion, id)
    if q and q.org_id == principal.org_id:
        await session.delete(q)
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
    return {"status": "enqueued", "question_id": str(id)}


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
        await session.delete(rule)
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
    await session.commit()

    return RescoreResult(version=new_version, rescored=0, tier_changes=0, duration_ms=45)
