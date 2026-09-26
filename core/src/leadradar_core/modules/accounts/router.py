import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from leadradar_auth.dependencies import get_current_principal, require_roles
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.errors import UnprocessableException
from leadradar_core.modules.accounts.importer import DuplicatePolicy, MappingName
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import (
    CompanyCreate,
    CompanyImportReport,
    CompanyOut,
    CompanyUpdate,
    DocumentOut,
)
from leadradar_core.modules.accounts.service import check_import_size, import_companies
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.pagination import PaginatedResponse
from leadradar_core.settings import settings
from leadradar_core.utils.domain import normalize_domain
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["accounts"])


@router.get("/companies", response_model=PaginatedResponse[CompanyOut])
async def list_companies(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None),
    is_tracked: bool | None = Query(default=None),
) -> PaginatedResponse[CompanyOut]:
    stmt = select(Company).where(Company.org_id == principal.org_id)

    if q:
        search_filter = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(Company.name.ilike(search_filter), Company.domain.ilike(search_filter)))

    if is_tracked is not None:
        stmt = stmt.where(Company.is_tracked == is_tracked)

    # Count total
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    stmt = stmt.order_by(Company.created_at.desc()).offset(offset).limit(page_size)
    res = await session.execute(stmt)
    companies = res.scalars().all()

    return PaginatedResponse(
        items=[CompanyOut.model_validate(c) for c in companies],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/companies", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
async def create_company(
    comp_in: CompanyCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CompanyOut:
    normalized = normalize_domain(comp_in.domain)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain")

    # Check if exists
    stmt = select(Company).where(Company.org_id == principal.org_id, Company.domain == normalized)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Company with domain '{normalized}' already exists",
        )

    company = Company(
        org_id=principal.org_id,
        name=comp_in.name,
        domain=normalized,
        country_code=comp_in.country_code,
        industry_ids=comp_in.industry_ids,
        employees=comp_in.employees,
        revenue_eur=comp_in.revenue_eur,
        hq_city=comp_in.hq_city,
        homepage_url=comp_in.homepage_url or f"https://{normalized}",
        careers_url=comp_in.careers_url,
        newsroom_url=comp_in.newsroom_url,
        linkedin_url=comp_in.linkedin_url,
        notes=comp_in.notes,
        tags=comp_in.tags,
        origin="manual",
        is_tracked=True,
    )
    session.add(company)
    await session.commit()
    await session.refresh(company)
    return CompanyOut.model_validate(company)


@router.get("/companies/{id}", response_model=CompanyOut)
async def get_company(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CompanyOut:
    company = await session.get(Company, id)
    if not company or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return CompanyOut.model_validate(company)


@router.patch("/companies/{id}", response_model=CompanyOut)
async def update_company(
    id: UUID,
    comp_in: CompanyUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CompanyOut:
    company = await session.get(Company, id)
    if not company or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    for field, val in comp_in.model_dump(exclude_unset=True).items():
        setattr(company, field, val)

    await session.commit()
    await session.refresh(company)
    return CompanyOut.model_validate(company)


@router.delete(
    "/companies/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles("admin"))],
)
async def delete_company(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    company = await session.get(Company, id)
    if company and company.org_id == principal.org_id:
        await session.delete(company)
        await session.commit()


@router.post("/companies/import", response_model=CompanyImportReport)
async def import_companies_csv(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    file: Annotated[UploadFile, File()],
    column_map: Annotated[
        str | None,
        Form(description='mapping=custom: JSON object target field → CSV column, e.g. {"name": "Company"}'),
    ] = None,
    mapping: Annotated[MappingName, Query()] = "default",
    on_duplicate: Annotated[DuplicatePolicy, Query()] = "merge",
) -> CompanyImportReport:
    """Import companies from CSV (≤ 5 MB, ≤ 5 000 rows). Mappings: default template, crunchbase, custom."""
    content = await file.read(settings.IMPORT_MAX_BYTES + 1)
    check_import_size(len(content), settings.IMPORT_MAX_BYTES)
    parsed_map: dict[str, str] | None = None
    if column_map:
        try:
            raw_map = json.loads(column_map)
        except json.JSONDecodeError as e:
            raise UnprocessableException("invalid_column_map", "column_map must be a JSON object") from e
        if not isinstance(raw_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_map.items()
        ):
            raise UnprocessableException(
                "invalid_column_map", "column_map must map field names to column names"
            )
        parsed_map = raw_map
    return await import_companies(
        session,
        principal.org_id,
        content,
        mapping=mapping,
        column_map=parsed_map,
        on_duplicate=on_duplicate,
        max_rows=settings.IMPORT_MAX_ROWS,
    )


@router.get("/companies/{id}/documents", response_model=PaginatedResponse[DocumentOut])
async def get_company_documents(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    source_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PaginatedResponse[DocumentOut]:
    company = await session.get(Company, id)
    if not company or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    stmt = select(Document).where(Document.company_id == id)
    if source_type:
        stmt = stmt.where(Document.source_type == source_type)

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    stmt = (
        stmt.order_by(Document.fetched_at.desc(), Document.id).offset((page - 1) * page_size).limit(page_size)
    )
    res = await session.execute(stmt)
    return PaginatedResponse(
        items=[DocumentOut.model_validate(doc) for doc in res.scalars().all()],
        total=total,
        page=page,
        page_size=page_size,
    )
