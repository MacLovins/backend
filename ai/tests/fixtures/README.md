# Fixtures for lr-ai analyze / eval

Parser `Document` JSONL, one document per line (`lr-ai analyze --fixture …`, `lr-ai eval --fixtures-dir ai/tests/fixtures`).

| File | Source | Collected | Notes |
|---|---|---|---|
| `dhl.jsonl` | Google News RSS via `ai/scripts/gnews_fixture.py` | 2026-09-25 | 227 headlines, last 365 days, IA topics. **Headlines only** (`headline_only`), links are Google redirects. Stopgap: dhl.com is bot-protected, GDELT returned 429, `lr-parser collect` hangs on unreachable sites |

Queries used for `dhl.jsonl`:

```
"DHL" (AI OR "agentic" OR automation OR robotics) when:365d
"DHL Group" (strategy OR efficiency OR "cost savings" OR digitalization OR restructuring) when:365d
"DHL" (CIO OR COO OR "chief digital" OR appoints OR partnership) when:365d
```

Replace with full-text fixtures from `lr-parser collect --out` once the parser can collect these companies.
Golden labels must be made against the fixture's content (see `src/leadradar_ai/evals/golden/`).
