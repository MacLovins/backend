# Fixtures for lr-ai analyze / eval

Parser `Document` JSONL, one document per line (`lr-ai analyze --fixture …`, `lr-ai eval --fixtures-dir ai/tests/fixtures`).

| File | Source | Collected | Notes |
|---|---|---|---|
| `dhl.jsonl` | Google News RSS via `ai/scripts/gnews_fixture.py` | 2026-09-25 | 227 headlines, last 365 days, IA topics. **Headlines only** (`headline_only`), links are Google redirects. Stopgap: dhl.com is bot-protected, GDELT returned 429, `lr-parser collect` hangs on unreachable sites |
| `lufthansa.jsonl` | `lr-parser collect --name "Lufthansa Group" --domain lufthansagroup.com --sources news,website,jobs --since 180d` + a `--sources news --since 400d --topics "4000 administrative jobs,artificial intelligence,digitalization,efficiency program,cyber attack"` run, merged (dedup by hash/URL) | 2026-09-26 | 187 docs: news (Google News, GDELT, NewsAPI, SerpAPI) + 23 pages of lufthansagroup.com. Contains the Annex facts (4,000 admin jobs by 2030 with AI) |
| `sap.jsonl` | `lr-parser collect --name SAP --domain sap.com …` (+ topics run) | 2026-09-26 | 150 news docs. **Vendor trap**: SAP sells AI/automation; also "sap" the verb and SAP Center (arena) |
| `orange.jsonl` | `lr-parser collect --name Orange --domain orange.com …` (+ topics run) | 2026-09-26 | 154 news docs. **Homonym trap**: most hits are Orange County, Orange Order, orange the colour/fruit, Orange Marketing… |
| `deutschebahn.jsonl` | `lr-parser collect --name "Deutsche Bahn" --domain deutschebahn.com …` (+ German topics run) | 2026-09-26 | 198 docs incl. 24 pages of deutschebahn.com. Real DDoS attack (Feb 2026) next to an outage that was *not* an attack |

Queries used for `dhl.jsonl`:

```
"DHL" (AI OR "agentic" OR automation OR robotics) when:365d
"DHL Group" (strategy OR efficiency OR "cost savings" OR digitalization OR restructuring) when:365d
"DHL" (CIO OR COO OR "chief digital" OR appoints OR partnership) when:365d
```

Golden labels are made against the fixtures' content (see `src/leadradar_ai/evals/golden/`): every labelled quote
is verbatim in the named document (checked by `tests/test_evals.py`). Dates matter — run evals with
`--now 2026-09-26`, e.g. `lr-ai eval --oracle --fake-embeddings --now 2026-09-26`. Keys come from `.env`; nothing
secret is stored in the fixtures.
