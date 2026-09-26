#!/usr/bin/env python3
"""CLI utility to test all parser adapters and data collection on any company.

Usage:
    PYTHONPATH="core/src:auth/src:ai/src:parser/src" uv run python scripts/test_parser.py [domain] [company_name]
Example:
    PYTHONPATH="core/src:auth/src:ai/src:parser/src" uv run python scripts/test_parser.py kuehne-nagel.com "Kuehne + Nagel"
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

load_dotenv(".env")

from leadradar_parser import CollectPlan, CompanyRef, collect, resolve_company  # noqa: E402


async def main() -> None:
    domain = sys.argv[1] if len(sys.argv) > 1 else "kuehne-nagel.com"
    name = sys.argv[2] if len(sys.argv) > 2 else None

    print("\n=======================================================")
    print(f" LEADRADAR PARSER TEST: {domain}")
    print("=======================================================\n")

    # Step 1: Resolve Company
    print("[1/2] Resolving company via Wikidata & DNS...")
    ref = CompanyRef(domain=domain, name=name)
    resolved = await resolve_company(ref)

    company_title = resolved.name or resolved.domain
    print(f"  Company: {company_title}")
    print(f"  Homepage: {resolved.homepage_url}")
    if resolved.careers_url:
        print(f"  Careers URL: {resolved.careers_url}")
    if resolved.ats:
        print(f"  ATS System: {resolved.ats.kind} ({resolved.ats.url})")
    if resolved.firmographics:
        fg = resolved.firmographics
        print(f"  Firmographics: Country={fg.country_code}, City={fg.hq_city}, Employees={fg.employees}")

    # Step 2: Collect Data across all adapters
    print("\n[2/2] Collecting data via active adapters...")
    print("      (SerpAPI, NewsAPI, GDELT, Google News, RSSHub, Careers)...")

    plan = CollectPlan(
        source_types={"news", "registry", "jobs"},
        since=datetime.now(UTC) - timedelta(days=28),
        time_budget_s=30,
        max_items_per_source=20,
    )

    start_time = datetime.now(UTC)
    result = await collect(resolved, plan)
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    print("\n=======================================================")
    print(f" COLLECTION SUMMARY ({elapsed:.1f}s):")
    print("=======================================================")
    print(f" Total unique documents collected: {len(result.documents)}\n")
    print(" Results by adapter:")
    for adapter, count in sorted(result.stats.items()):
        status_icon = "[OK]" if count > 0 else "[--]"
        print(f"   {status_icon} {adapter:18}: {count:3} documents")

    if result.errors:
        print("\n Notes / Disables:")
        for err in result.errors:
            print(f"   - [{err.adapter}] ({err.kind}): {err.message}")

    if result.documents:
        print("\n=======================================================")
        print(" TOP 5 COLLECTED SAMPLES:")
        print("=======================================================")
        for i, doc in enumerate(result.documents[:5], 1):
            pub_date = doc.published_at.strftime("%Y-%m-%d") if doc.published_at else "n/a"
            print(f"\n{i}. [{doc.source_name.upper()}] {doc.title}")
            print(f"   Date: {pub_date} | Language: {doc.language or 'auto'}")
            print(f"   URL: {doc.url}")
            clean_snippet = " ".join(doc.text.split())[:180]
            print(f"   Snippet: {clean_snippet}...")

    print("\n=======================================================")
    print(" PARSER TEST COMPLETED SUCCESSFULLY!")
    print("=======================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
