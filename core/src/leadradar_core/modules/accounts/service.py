"""Accounts use cases: CSV import (CO-07)."""

from typing import Any
from uuid import UUID

from leadradar_core.errors import PayloadTooLargeException, UnprocessableException
from leadradar_core.modules.accounts.importer import (
    DuplicatePolicy,
    ImportFormatError,
    MappingName,
    TooManyRowsError,
    parse_csv,
)
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import CompanyImportReport, ImportDuplicateOut
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def check_import_size(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise PayloadTooLargeException(
            f"CSV file is larger than {max_bytes // (1024 * 1024)} MB",
            {"max_bytes": max_bytes},
        )


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


async def import_companies(
    session: AsyncSession,
    org_id: UUID,
    content: bytes,
    *,
    mapping: MappingName = "default",
    column_map: dict[str, str] | None = None,
    on_duplicate: DuplicatePolicy = "merge",
    max_rows: int = 5000,
) -> CompanyImportReport:
    """Create new companies and fill empty fields of existing ones (dedup by normalized domain).

    `updated` counts only companies where at least one field actually changed; rows that change nothing are
    `skipped`. Duplicate domains inside the file are merged into the first row (or skipped) and reported.
    """
    try:
        parsed = parse_csv(
            content, mapping=mapping, column_map=column_map, on_duplicate=on_duplicate, max_rows=max_rows
        )
    except TooManyRowsError as e:
        raise UnprocessableException(
            "too_many_rows", f"CSV has more than {e.limit} rows; split the file", {"max_rows": e.limit}
        ) from e
    except ImportFormatError as e:
        raise UnprocessableException(e.code, str(e), e.details) from e

    report = CompanyImportReport(
        total_rows=parsed.total_rows,
        skipped=parsed.skipped,
        errors=list(parsed.errors),
        warnings=list(parsed.warnings),
        duplicates=[
            ImportDuplicateOut(row=d.row, first_row=d.first_row, domain=d.domain, action=d.action)
            for d in parsed.duplicates
        ],
    )
    if not parsed.rows:
        return report

    domains = [r.domain for r in parsed.rows]
    existing = {
        c.domain: c
        for c in (
            await session.execute(
                select(Company).where(Company.org_id == org_id, Company.domain.in_(domains))
            )
        ).scalars()
    }

    for row in parsed.rows:
        company = existing.get(row.domain)
        if company is None:
            session.add(
                Company(
                    org_id=org_id,
                    name=row.name,
                    domain=row.domain,
                    homepage_url=row.fields.get("homepage_url") or f"https://{row.domain}",
                    origin="csv",
                    is_tracked=True,
                    **{k: v for k, v in row.fields.items() if k != "homepage_url"},
                )
            )
            report.created += 1
            continue
        changed = False
        for key, value in row.fields.items():
            if _is_empty(getattr(company, key)):  # never overwrite data the user already has
                setattr(company, key, value)
                changed = True
        if changed:
            report.updated += 1
        else:
            report.skipped += 1

    await session.commit()
    return report
