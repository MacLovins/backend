# parser — ТЗ (`leadradar-parser`)

> **Роль:** сбор и нормализация публичных данных о компаниях из разнородных источников, резолв компании
> (домен, careers, ATS, фирмографика), автопоиск компаний по ICP, таксономия индустрий.
> **Владелец:** P2 — Data engineer · **Потребитель:** `core` (адаптер порта `Collector`, discovery, meta)
> **Связано:** [ARCHITECTURE.md](../ARCHITECTURE.md) §3.1 (Public Data, Accounts), §4.4 (границы), §4.7 (контракты), §4.14 (риски)
> **Закрывает:** K1 (чистые данные на входе), K5, K6 · S1 (discovery), S6–S11, S13, S16, S18 · A1 · U5, U6

---

## 0. Как пользоваться этим файлом

- `parser` — **чистая библиотека + CLI**: без БД, без FastAPI, без LLM. Вход — Pydantic-контракты, выход — `Document`.
  Хранением занимается core, анализом — ai.
- Каждый адаптер разрабатывается и тестируется отдельно (`lr-parser collect --sources <id>`), тесты идут без сети
  (respx-фикстуры).
- Для агента: одна функция из §1.3 + §1.4 (контракты) + §1.7 (правила вежливости). Проверка — команда из таблицы.

---

## 1. Этап 1 — MVP

### 1.1 Цель

За ≤ 90 с на компанию собрать из бесплатных источников новости, страницы сайта и newsroom, вакансии и фирмографику,
привести всё к единому `Document` с датами и дедупом, не упасть при сбое отдельного источника и не нарушить
robots.txt и ToS. Плюс найти новые компании по ICP (Wikidata).

### 1.2 Границы и требования челленджа (Orange Systems Data Layer)

| Компонент | Требования ТЗ челленджа | Реализация в LeadRadar |
|---|---|---|
| **Web scraping / crawling** | Playwright, Scrapy или SerpAPI | HTTPX с Browser User-Agent, защитой от Cloudflare-банов (таймауты 10s), SerpAPI для обхода капчи |
| **News & Signals** | NewsAPI, GDELT, RSSHub / Google News RSS | `google_news` (RSS, 0 лимитов), `gdelt` (DOC 2.0 API), `newsapi` (NewsAPI.org), `serpapi` (Google News search) |
| **Job data** | Corporate career pages, public job boards | `jobs_ats` (Greenhouse, Lever, Workday, Personio, Ashby, SmartRecruiters, Workable) + `careers_html` fallback |
| **Company data** | Crunchbase, Wikidata, registries | Wikidata SPARQL (выручка, сотрудники, LPR, LEI) + импорт CSV Crunchbase |
| **Storage** | PostgreSQL или MongoDB | PostgreSQL 16 + pgvector (`core`, `auth`, `langgraph`) |
| **AI / ML Layer** | LangChain / LangGraph, LLMs (Gemini/OpenAI/Claude), Scoring, Message Gen | LangGraph, Gemini Gateway с fallback, векторизация multilingual-e5, scoring с decay, Outreach Generator |

### 1.3 Функции

| ID | Функция | Пр. | Проверка |
|---|---|---|---|
| PR-01 | Контракты (`contracts.py`) и публичный API (`__init__.py`) по §1.4 | P0 | `pytest -k contracts` |
| PR-02 | HTTP-слой: `httpx.AsyncClient`, Browser User-Agent, таймауты 10s, ретраи с backoff, лимит на хост (aiolimiter), HTTP-кэш (hishel) | P0 | `pytest -k http` |
| PR-03 | Адаптер `google_news`: RSS-поиск (без банов и 429), извлечение заголовков и сниппетов за 30-365 дней | P0 | `pytest -k google_news` |
| PR-04 | Адаптер `gdelt`: DOC 2.0 API, JSON, 2 запроса, circuit breaker при 429, fallback на заголовок | P0 | `pytest -k gdelt` |
| PR-05 | Адаптер `newsapi`: поиск по `NEWSAPI_KEY` (если задан) мировых СМИ | P0 | `pytest -k newsapi` |
| PR-06 | Адаптер `serpapi`: поиск по `SERPAPI_KEY` новостей и обход защиты сайтов | P0 | `pytest -k serpapi` |
| PR-07 | Адаптер `website`: robots → sitemap(ы) → выбор URL → trafilatura (текст, заголовок, дата) | P0 | `pytest -k website` |
| PR-08 | `resolve_company`: homepage, `own_domains`, `careers_url`, детекция ATS, алиасы, фирмографика Wikidata | P0 | `lr-parser resolve --domain dhl.com` |
| PR-09 | Адаптер `jobs_ats`: Greenhouse, Lever, Workday, Personio, Ashby, SmartRecruiters, Workable | P0 | `pytest -k ats` |
| PR-10 | Fallback `careers_html`: вакансии со страницы карьеры (эвристики) | P0 | `pytest -k careers_html` |
| PR-11 | Нормализация: очистка текста, язык, `canonical_url`, `content_hash`, даты в UTC | P0 | `pytest -k normalize` |
| PR-12 | Wikidata: фирмографика по QID / домену / имени (выручка, сотрудники, CEO, LEI) | P0 | `pytest -k wikidata` |
| PR-13 | `discover(DiscoveryQuery)`: SPARQL по ICP (страны, индустрии, размер) → кандидаты | P0 | `lr-parser discover` |
| PR-14 | Таксономия `data/industries.yaml` + `data/countries.yaml` | P0 | `pytest -k taxonomy` |
| PR-15 | CLI `lr-parser`: `resolve`, `collect`, `discover`, `adapters` | P0 | `lr-parser --help` |
| PR-19 | Адаптер `adzuna` (по ключу): 25 запросов/мин, 250 в сутки; фильтр по компании и `what_or` | P1 | `pytest -k adzuna` |
| PR-20 | Fallback на Playwright для JS-страниц, где trafilatura вернула пусто | P2 | — |
| PR-21 | Заглушка `crunchbase` (выключена без ключа; интерфейс под этап 2) | P2 | — |

### 1.4 Входы и выходы

#### 1.4.1 Публичный API (`leadradar_parser/__init__.py`)

```python
async def resolve_company(ref: CompanyRef, *, http: HttpClient | None = None) -> ResolvedCompany
async def collect(company: ResolvedCompany, plan: CollectPlan, *, http: HttpClient | None = None) -> CollectResult
async def discover(query: DiscoveryQuery, *, http: HttpClient | None = None) -> list[CompanyCandidate]
def industry_taxonomy() -> list[Industry]
def country_catalog() -> list[Country]
def list_adapters() -> list[AdapterInfo]          # id, source_type, enabled, requires_key, rate_limit
def create_http_client(settings: ParserSettings | None = None) -> HttpClient
# + реэкспорт всех контрактов §1.4.2 и ParserSettings — core импортирует только из `leadradar_parser`
```

#### 1.4.2 Контракты (`contracts.py`; поля только добавляются)

```python
SourceType = Literal["news", "website", "jobs", "report", "registry", "incident"]
AtsKind = Literal["greenhouse", "lever", "workday", "personio", "ashby", "smartrecruiters", "workable", "recruitee"]

class AtsRef(BaseModel):
    kind: AtsKind; token: str; host: str | None = None; site: str | None = None   # host и site — для Workday

class CompanyRef(BaseModel):
    name: str; domain: str; country_code: str | None = None; aliases: list[str] = []
    careers_url: str | None = None; newsroom_url: str | None = None; ats: AtsRef | None = None
    wikidata_qid: str | None = None

class Firmographics(BaseModel):
    legal_name: str | None = None; country_code: str | None = None; hq_city: str | None = None
    industry_ids: list[str] = []; employees: int | None = None; revenue_eur: int | None = None
    founded: int | None = None; lei: str | None = None; wikidata_qid: str | None = None
    crunchbase_id: str | None = None; ceo: str | None = None; source: str

class ResolvedCompany(CompanyRef):
    homepage_url: str; own_domains: list[str]; firmographics: Firmographics | None = None
    resolved_at: datetime; notes: list[str] = []          # «careers не найден», «ATS: workday» и т. п.

class CollectPlan(BaseModel):
    source_types: set[SourceType]; since: datetime
    news_topics: list[str] = []; job_keywords: list[str] = []
    languages: list[str] = ["en", "de", "fr"]
    max_items_per_source: int = 50; max_website_pages: int = 25; time_budget_s: int = 90

class Document(BaseModel):
    source_type: SourceType; source_name: str             # id адаптера: "gdelt", "website", "workday", …
    url: str; canonical_url: str; title: str | None; text: str
    published_at: datetime | None; fetched_at: datetime; language: str | None
    content_hash: str                                     # sha256 нормализованного текста
    meta: dict = {}                                       # publisher, page_kind, job_location, department, headline_only, attribution

class SourceError(BaseModel):
    adapter: str; kind: Literal["rate_limited", "blocked", "robots", "not_found", "timeout", "parse_error", "disabled"]
    message: str; retry_after_s: int | None = None

class CollectResult(BaseModel):
    documents: list[Document]; errors: list[SourceError]
    stats: dict[str, int]                                 # {"gdelt": 23, "website": 12, "workday": 41}
    duration_ms: int

class DiscoveryQuery(BaseModel):
    countries: list[str]; industries: list[str]
    employees_min: int | None = None; employees_max: int | None = None
    limit: int = 100; exclude_domains: list[str] = []

class CompanyCandidate(BaseModel):
    name: str; domain: str; country_code: str | None; industry_ids: list[str]
    employees: int | None; revenue_eur: int | None; wikidata_qid: str
    lei: str | None; crunchbase_id: str | None; source: Literal["wikidata"] = "wikidata"
```

#### 1.4.3 Протокол адаптера (`adapters/base.py`)

```python
class SourceAdapter(Protocol):
    id: str                          # "gdelt"
    source_type: SourceType          # "news"
    requires_env: str | None         # "NEWSAPI_KEY" или None
    rate_limit: RateLimit            # RateLimit(requests=1, per_seconds=5, scope="global" | "host")
    async def fetch(self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient) -> AsyncIterator[Document]: ...
```

Реестр: `ADAPTERS: dict[str, SourceAdapter]`. Включение — `PARSER_ADAPTERS` + наличие ключей. Новый источник =
новый файл в `adapters/` + одна строка в реестре.

### 1.5 Зависимости

| Тип | Что |
|---|---|
| Python | `httpx`, `hishel`, `tenacity`, `aiolimiter`, `protego`, `trafilatura`, `lxml`, `fast-langdetect`, `pydantic` v2, `pyyaml`, `typer`; P1: `feedparser`, `pymupdf`, `gnews-decoder`, `rapidfuzz` |
| Внешние | GDELT DOC 2.0, сайты компаний, публичные JSON-эндпоинты ATS, Wikidata (SPARQL + API), P1: Google News RSS, GLEIF, HIBP, NewsAPI и Adzuna (ключи) |
| От других папок | Ничего |
| Для других папок | core — публичный API §1.4.1; ai — JSONL-фикстуры (`lr-parser collect --out`) к H6 |

```dotenv
PARSER_ADAPTERS=gdelt,website,jobs_ats,careers_html,wikidata      # P1: google_news,gleif,hibp,reports,rss,newsapi,adzuna
PARSER_USER_AGENT="LeadRadarBot/0.1 (+https://<demo-host>/bot; <team-email>)"
PARSER_CACHE_DIR=.cache/parser
PARSER_CACHE_TTL_S=86400
PARSER_HOST_RPS=1
PARSER_MAX_CONCURRENCY=8
NEWSAPI_KEY=
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
PARSER_REPORTS_MAX_PDFS=3          # reports: PDF на компанию, ≤ 30 МБ каждый (PARSER_REPORTS_MAX_PDF_MB)
RSSHUB_BASE_URL=                   # только свой инстанс RSSHub; RSSHUB_ROUTE=/bing/search/{query}
```

Без ключей по умолчанию включены также `reports`, `hibp` (каталог утечек — 1 запрос в сутки на процесс) и `gleif`;
`adzuna`, `newsapi`, `serpapi`, `rsshub`, `crunchbase` — только при заданных ключах / URL.

### 1.6 Структура папки

```
parser/
├── SPEC.md
├── pyproject.toml                      # name = "leadradar-parser"; scripts: lr-parser = "leadradar_parser.cli:app"
├── src/leadradar_parser/
│   ├── __init__.py                     # публичный API §1.4.1
│   ├── contracts.py · settings.py · errors.py
│   ├── http.py                         # фабрика клиента: UA, кэш, лимиты, robots, блок-лист, ретраи
│   ├── normalize.py                    # текст, язык, canonical_url, content_hash, даты
│   ├── resolve.py                      # homepage, careers/newsroom, детекция ATS, own_domains
│   ├── discovery.py                    # ICP → SPARQL → кандидаты
│   ├── taxonomy.py
│   ├── data/  industries.yaml · countries.yaml · url_patterns.yaml · ats_patterns.yaml
│   ├── adapters/
│   │   ├── base.py · registry.py
│   │   ├── news_gdelt.py · news_google_rss.py (P1) · news_newsapi.py (P1)
│   │   ├── web_site.py · web_rss.py (P1) · web_reports.py (P1)
│   │   ├── jobs_ats.py · jobs_careers_html.py · jobs_adzuna.py (P1)
│   │   ├── registry_wikidata.py · registry_gleif.py (P1)
│   │   ├── incident_hibp.py (P1)
│   │   └── crunchbase.py (P2, заглушка)
│   └── cli.py
└── tests/
    ├── fixtures/http/ …                # записанные ответы: GDELT JSON, sitemap, HTML, ATS JSON, SPARQL JSON
    └── test_*.py                       # respx; живые тесты помечены @pytest.mark.live (не в CI)
```

### 1.7 Алгоритмы и правила

#### 1.7.1 Вежливость и право (P16, P17)

- User-Agent с контактом; robots.txt проверяется перед каждым хостом (кэш 24 ч); `Disallow` → `SourceError(robots)`.
- Лимиты: 1 запрос/с на хост по умолчанию; **GDELT — не чаще 1 запроса в 5 с на весь процесс** (лимит на IP);
  после повторных 429 — backoff 60 → 120 с и пауза источника на 15 мин. Wikidata — один запрос за раз,
  тайм-аут 60 с, на 429 — ждать `Retry-After`.
- **Блок-лист** (проверяется в HTTP-слое, тест обязателен): `linkedin.com` и поддомены, `facebook.com`, `instagram.com`,
  `x.com`, `twitter.com`, `indeed.*`, `glassdoor.*`.
- Страницы за логином, пейволлом или капчей не обходим: берём только заголовок (`meta.headline_only = true`).
- HIBP: только публичный каталог утечек, без поиска по e-mail; в `meta.attribution` — «Have I Been Pwned (CC BY 4.0)».
- Персональные данные не собираем специально. Имена руководителей попадают только внутри публичных новостей.

#### 1.7.2 Новости — GDELT (PR-03)

- Запрос A (общий, 30 дней): `"<name>" (sourcelang:english OR sourcelang:german OR …)`,
  `mode=ArtList&format=json&maxrecords=75&sort=DateDesc&timespan=30d`.
- Запрос B (по темам, до 3 месяцев — предел GDELT): `"<name>" (<до 8 терминов из news_topics через OR>)`.
- Короткие или многозначные имена («DHL», «Orange») дополняем юр. названием или вторым словом из алиасов.
  Фильтр сущности в ai всё равно отсеет чужое.
- По каждому URL (≤ 20 свежих, дедуп по canonical) загружаем текст через trafilatura; не удалось — документ
  только с заголовком. Поля: `published_at` = `seendate`, `meta.publisher` = домен, `language`.

#### 1.7.3 Сайт и newsroom (PR-04)

1. `robots.txt` → `Sitemap:`; иначе `/sitemap.xml`, `/sitemap_index.xml`. Разбираем ≤ 5 карт и ≤ 5 000 URL.
2. Оценка URL: шаблоны из `url_patterns.yaml` (мультиязычно: `news|press|presse|media|newsroom|investor|ir|strategy|
   strategie|about|ueber-uns|unternehmen|sustainability|annual-report|geschaeftsbericht|careers|karriere|jobs`)
   + свежесть `lastmod` в окне `since`.
3. Добавляем ссылки навигации с главной. Со страниц-списков newsroom идём на статьи (тот же хост, глубина 1,
   ≤ 15 статей в окне).
4. Берём топ `max_website_pages` (25), загружаем (1 запрос/с), trafilatura: `favor_precision=True`, с метаданными
   (title, date). Страницы < 300 символов отбрасываем. `meta.page_kind` ∈ {news, strategy, ir, about, careers, other}.

#### 1.7.4 Резолв компании и ATS (PR-05, PR-06)

- homepage = `https://<domain>` (с редиректами); `own_domains` = домен + домены после редиректов + хост ATS.
- `careers_url`: ссылки с главной по шаблонам careers / karriere / jobs; иначе типовые пути; иначе пусто (в `notes`).
- Детекция ATS — регулярные выражения по HTML, ссылкам и iframe страницы карьеры (`ats_patterns.yaml`).
  **Эндпоинты — публичные и неофициальные, проверить на 2–3 компаниях в час 0:**

| ATS | Признак на странице | Эндпоинт |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}` | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `jobs.lever.co/{company}` (EU: `jobs.eu.lever.co`) | `GET https://api.lever.co/v0/postings/{company}?mode=json` (EU: `api.eu.lever.co`) |
| Workday | `{tenant}.wd{N}.myworkdayjobs.com/{site}` | `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` с телом `{"appliedFacets":{},"limit":20,"offset":0,"searchText":"<keyword>"}`; описание — `GET …/wday/cxs/{tenant}/{site}{externalPath}` |
| Personio | `{sub}.jobs.personio.de` / `.com` | `GET https://{sub}.jobs.personio.de/xml` |
| Ashby (P1) | `jobs.ashbyhq.com/{name}` | `GET https://api.ashbyhq.com/posting-api/job-board/{name}` |
| SmartRecruiters (P1) | `jobs.smartrecruiters.com/{id}` | `GET https://api.smartrecruiters.com/v1/companies/{id}/postings` |
| Workable (P1) | `apply.workable.com/{sub}` | `GET https://apply.workable.com/api/v1/widget/accounts/{sub}` |
| Recruitee (P1) | `{sub}.recruitee.com` | `GET https://{sub}.recruitee.com/api/offers/` |

- Одна вакансия = один `Document(source_type="jobs")`: заголовок + локация + отдел + описание (plain text ≤ 8k символов),
  `published_at` = дата публикации или обновления, `meta` = {location, department, employment_type, ats}.
  Для Workday — до 5 поисков по `job_keywords`, дедуп по `externalPath`, описания у ≤ 30 вакансий.
- Нет ATS → `careers_html`: ссылки, похожие на вакансии (`/job/`, `/stellen`, `/position`, `jobId=`),
  документ только с заголовком (`meta.headline_only = true`).

#### 1.7.5 Нормализация (PR-08)

`canonical_url`: нижний регистр схемы и хоста, без `#…`, без `utm_*`, `gclid`, `fbclid`, без завершающего `/`.
Текст: схлопнуть пробелы, убрать повторяющиеся строки и cookie-баннеры. Лимиты: 50k символов (страницы),
8k (вакансии), 40k (отчёты — только релевантные страницы). Язык — `fast-langdetect`. `content_hash` —
sha256(casefold + схлопнутые пробелы). Даты — UTC; неизвестная дата → `None` (ai помечает `undated`).
Внутри одного `CollectResult` дубли по `canonical_url` и `content_hash` удаляются.

#### 1.7.6 Discovery по ICP (PR-10) — Wikidata SPARQL

```sparql
SELECT ?item ?itemLabel ?website ?cc (MAX(?emp) AS ?employees) (SAMPLE(?lei0) AS ?lei) (SAMPLE(?cb0) AS ?cbId)
       (GROUP_CONCAT(DISTINCT STR(?industry); separator="|") AS ?industries)
WHERE {
  VALUES ?industry { {{industry_qids}} }          # из industries.yaml по выбранным id
  VALUES ?country  { {{country_qids}} }           # из countries.yaml; Германия = wd:Q183, Австрия = wd:Q40
  ?item wdt:P452 ?industry ; wdt:P17 ?country ; wdt:P856 ?website .
  ?country wdt:P297 ?cc .
  OPTIONAL { ?item wdt:P1128 ?emp . }             # число сотрудников
  OPTIONAL { ?item wdt:P1278 ?lei0 . }            # LEI
  OPTIONAL { ?item wdt:P2088 ?cb0 . }             # Crunchbase organization ID
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?item ?itemLabel ?website ?cc
HAVING (COALESCE(MAX(?emp), {{employees_min}}) >= {{employees_min}})   # неизвестный размер не отсекаем
ORDER BY DESC(?employees) LIMIT {{limit}}
```

Домен — хост из `P856` без `www.`. Дедуп по домену, исключаем `exclude_domains`. Индустрии Wikidata мапятся обратно
на id таксономии. Неизвестное число сотрудников — кандидат остаётся, в UI отмечается как пробел в данных.
QID индустрий заполняются в `industries.yaml` на H12–H14 (поиск на wikidata.org, по 1–3 QID на индустрию).

#### 1.7.7 Таксономия (PR-11)

`industries.yaml` (≈ 25 записей):
`{id: logistics, label: "Logistics & Transport", wikidata: [Q…], nace: ["H49", "H52"], nis2: annex_i, dora: false}`.
Обязательные id (на них ссылаются пресеты ai): `logistics`, `airlines`, `rail`, `postal_courier`, `automotive`,
`manufacturing`, `chemicals`, `pharma`, `medical_devices`, `healthcare`, `banking`, `insurance`,
`financial_markets`, `telecom`, `energy_utilities`, `oil_gas`, `water`, `retail`, `consumer_goods`, `food_beverage`,
`public_sector`, `digital_infrastructure`, `it_services`, `software`, `media`, `construction`, `real_estate`.
`countries.yaml`: ISO2, QID, языки (для запросов и ключевых слов), `is_eu`.

### 1.8 План работ P2 (часы от старта)

| Окно | Задачи |
|---|---|
| H0–H2 | PR-01, PR-02, PR-12 (каркас), проверка эндпоинтов ATS и GDELT на 2–3 компаниях |
| H2–H8 | PR-04, PR-03, PR-06 (Greenhouse, Lever, Workday, Personio), PR-09, PR-08. **H6: JSONL DHL, Lufthansa, SAP, Orange → P3** |
| H8–H12 | PR-05 (резолв и ATS), встраивание в адаптер `Collector` core (с P1), фиксы |
| H12–H24 | PR-10, PR-11, PR-07, остальные ATS; P1: PR-13, PR-15, PR-16, PR-14 |
| H24–H32 | Пакетный прогон демо (с P1), починка адаптеров по логам; P1: PR-17, PR-18, PR-19 при наличии ключей |
| H32–H40 | Тесты, статистика источников для страницы Quality, README пакета |

### 1.9 Критерии готовности (DoD)

- [ ] `uv run --package leadradar-parser pytest` зелёный **без сети**: для каждого адаптера есть фикстура;
      тесты robots-disallow, блок-листа (запрос к `linkedin.com` отклонён ещё до сети) и 429 → backoff.
- [ ] `lr-parser collect --name "DHL Group" --domain dhl.com --sources news,website,jobs --since 90d --out dhl.jsonl`
      за ≤ 90 с даёт ≥ 10 новостей, ≥ 10 страниц сайта и вакансии из ATS (если он найден), все строки — валидные `Document`.
      То же для Lufthansa Group.
- [ ] `lr-parser resolve --domain lufthansagroup.com` возвращает `careers_url`, ATS или заметку и фирмографику из Wikidata.
- [ ] `lr-parser discover --countries DE,AT --industries logistics,airlines --min-employees 5000` → ≥ 10 кандидатов с доменами.
- [ ] Сбой любого адаптера (фикстура 429, timeout, 500) не роняет `collect`: остальные документы есть, ошибка лежит в `errors`.
- [ ] Повторный `collect` в пределах TTL кэша не делает сетевых запросов.
- [ ] `lint-imports` зелёный: пакет не импортирует workspace-пакеты.

### 1.10 Какие критерии закрывает модуль (MVP)

| ID | Как |
|---|---|
| S6 | Wikidata (профиль, CEO, идентификаторы, включая Crunchbase ID) — бесплатная замена; GLEIF (P1); CSV-выгрузку Crunchbase импортирует core; адаптер API — заглушка |
| S7 | `website` (newsroom, стратегия, IR, о компании), `reports` PDF (P1), RSS newsroom (P1) |
| S8 | `gdelt` (P0), `google_news` (P1), `newsapi` (P1, dev-ключ) |
| S9 | `jobs_ats` (8 ATS) + `careers_html` + `adzuna` (P1) |
| S10 | Блок-лист LinkedIn с тестом |
| S11 | Единый `Document` и нормализация для всех источников |
| S13 | Инциденты: HIBP (P1) и новости; смена руководства и compliance — новости и сайт; tech stack — тексты вакансий |
| S1, U6 | `discover` по ICP через Wikidata |
| S18 | `since` + дедуп + HTTP-кэш → инкрементальный повторный сбор |
| K1 | Чистый основной текст (trafilatura), даты, дедуп, `headline_only`-разметка → меньше мусора на входе ai |
| K5 | Изоляция сбоев источников, лимиты, ретраи, circuit breaker, кэш, тайм-бюджет |
| K6 | Реестр адаптеров, таксономия и страны как данные → новые рынки без кода |

---

## 2. Этап 2 — Ingestion service

### 2.1 Цель

Непрерывный, масштабируемый и юридически чистый сбор данных для ≥ 10 000 аккаунтов на тенант: свежесть новостей ≤ 1 ч,
вакансий ≤ 24 ч, сайтов ≤ 7 дней, наблюдаемое здоровье источников, архив сырья для доказательств.

### 2.2 Функции

| Область | Функции |
|---|---|
| Сервис | Та же библиотека за FastAPI (internal) + воркеры, читающие `collect.requested`; публикует `document.collected` |
| Планировщик | Политики свежести по источнику и тиру аккаунта (Hot — чаще); инкрементальный обход: ETag / Last-Modified, `lastmod` в sitemap, RSS; детекция изменений (diff) — в ai уходит только новое |
| Краулинг | Пул Playwright / Crawl4AI для JS-сайтов с лимитами; очередь URL; прокси — только там, где ToS это разрешают |
| Источники | Crunchbase API (лицензия), SerpAPI / Brave / Tavily-поиск, RSSHub, SEC EDGAR (8-K Item 1.05 — киберинциденты, 10-K), EU TED (тендеры), реестры (Companies House, Unternehmensregister), Bundesagentur für Arbeit (вакансии DE), ransomware.live (коммерческая лицензия), детекция tech stack (Wappalyzer-подобные отпечатки), транскрипты earnings calls |
| Сущности | Сервис резолва компаний: граф дочерних компаний (GLEIF relationships), бренды и алиасы, несколько доменов на компанию |
| Хранилище | Сырые HTML и PDF в S3 / MinIO (снапшот-доказательство и воспроизводимость), lineage документа |
| Эксплуатация | Дашборд здоровья источников (успешность, латентность, свежесть), автоотключение деградировавших адаптеров, алерты |
| Рынки | Пакеты источников по странам (DE, RO, Nordics…): локальные СМИ, job boards с разрешающими ToS, языки |
| Право | Реестр источников с правовым статусом и ToS, сроки хранения, процедура удаления по запросу |

### 2.3 Входы и выходы (изменения)

`Document` получает поля `raw_ref` (ссылка на S3), `change_type` (new / updated), `crawl_id` — только добавление.
Новый контракт `SourceHealth`. События: `collect.requested` → `document.collected`, `source.degraded`.

### 2.4 Зависимости

Очередь или шина (NATS JetStream / Kafka), S3 / MinIO, Redis (распределённые лимиты), браузерный пул, лицензии
платных источников.

### 2.5 Критерии готовности этапа 2

Свежесть по SLO (новости ≤ 1 ч, вакансии ≤ 24 ч); ≥ 100 000 документов в сутки; доля успешных запросов ≥ 95% по
активным источникам; любой источник отключается флагом без деплоя; для каждого сигнала доступен архивный снапшот.

### 2.6 Критерии, которые усиливает этап 2

S6 (Crunchbase API), S7 (JS-сайты, архив), S8 и S18 (near-real-time), S9 (больше рынков), S13 (EDGAR, TED,
ransomware, tech stack), K5 и K6 (масштаб, пакеты рынков).

---

## 3. Рост и развитие: что заложено в MVP и как расширять

| Заложено в MVP | Зачем | Как растёт на этапе 2 |
|---|---|---|
| `SourceAdapter` + реестр + `PARSER_ADAPTERS` | Новый источник = 1 файл | Плагины через entry points, флаги в админке, здоровье по каждому адаптеру |
| Единый `Document` с `meta` | Разнородные источники в одном формате | Новые `source_type` (tender, filing) и поля — только добавлением |
| Библиотека без БД + CLI | Разработка и тесты без инфраструктуры | Оборачивается в сервис без изменений адаптеров |
| `CollectPlan.since`, `content_hash`, HTTP-кэш | Инкрементальный сбор | Планировщик свежести, ETag / Last-Modified, diff |
| Централизованный HTTP-слой (лимиты, robots, блок-лист) | Вежливость и право в одном месте | Распределённый лимитер (Redis), пул браузеров, прокси-политики |
| Таксономия и страны в YAML | Рынки и индустрии — данные | Пакеты рынков, NACE и NAICS, локализация |
| `discover` через `DiscoveryQuery` | Автопоиск по ICP | Дополнительные провайдеры (Crunchbase, реестры) за тем же интерфейсом |
| Заглушка `crunchbase` | Место в архитектуре | Реализация по лицензии без изменения потребителей |

---

## 4. Риски и анти-паттерны

- **Не писать в БД из parser** — иначе пакет не вынести в сервис.
- **Не скрейпить LinkedIn, Indeed, Glassdoor** и не обходить логин, пейволл или капчу.
- **Не бомбить GDELT** параллельными запросами: 1 запрос / 5 с на IP, иначе блок примерно на 15 минут.
- Эндпоинты ATS неофициальные и могут поменяться. Каждый адаптер изолирован, сбой → `SourceError`, fallback на
  `careers_html`.
- Декодирование Google News зависит от внутреннего RPC Google и хрупкое. Поэтому это P1, и основной источник — GDELT.
- Омонимы компаний: запросы должны включать юр. название или алиас. Финальный фильтр сущности — в ai.
- NewsAPI dev-план **только для разработки**, в «продакшене» (в том числе на публичном демо-стенде) нужен платный план;
  на стенде по умолчанию выключен.
