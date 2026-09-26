# Fixtures for lr-ai analyze / eval

Parser `Document` JSONL, one document per line (`lr-ai analyze --fixture …`, `lr-ai eval --fixtures-dir ai/tests/fixtures`).

| File | Source | Collected | Notes |
|---|---|---|---|
| `dhl.jsonl` | Google News RSS via `ai/scripts/gnews_fixture.py` | 2026-09-25 | 227 headlines, last 365 days, IA topics. **Headlines only** (`headline_only`), links are Google redirects. Stopgap: dhl.com is bot-protected, GDELT returned 429, `lr-parser collect` hangs on unreachable sites |
| `lufthansa.jsonl` | same | 2026-09-26 | 176 headlines: AI, efficiency and job cuts, executives and partnerships |
| `sap.jsonl` | same | 2026-09-26 | 247 headlines. **Vendor trap**: most AI news are SAP products and customers' projects, not SAP's own operations |
| `orange.jsonl` | same | 2026-09-26 | 221 headlines. **Homonym trap**: Orange County, Orange Pi, Orange Logic, orange the fruit next to Orange the operator |

Queries used for `dhl.jsonl`:

```
"DHL" (AI OR "agentic" OR automation OR robotics) when:365d
"DHL Group" (strategy OR efficiency OR "cost savings" OR digitalization OR restructuring) when:365d
"DHL" (CIO OR COO OR "chief digital" OR appoints OR partnership) when:365d
```

Queries for the others: `"<Company>" (AI OR agentic OR automation …)`, `"<Company>" (efficiency OR "cost savings" OR
restructuring …)`, `"<Company>" (CIO OR COO OR appoints OR partnership)` (Orange: cyber incidents, NIS2, Orange
Cyberdefense instead of the last one), all `when:365d`.

Replace with full-text fixtures from `lr-parser collect --out` once the parser can collect these companies.
Golden labels must be made against the fixture's content (see `src/leadradar_ai/evals/golden/`).
