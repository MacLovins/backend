# core — ТЗ (`leadradar-core`)

> **Роль:** приложение. REST API и SSE для фронтенда, БД и миграции, очередь и worker, исполнение графа ai
> с реальными адаптерами, импорт и автопоиск аккаунтов, лиды, фидбек, метрики, сиды, деплой.
> **Владелец:** P1 — Backend lead · **Зависит от:** `parser`, `ai`, `auth` (только их публичный API)
> **Связано:** [ARCHITECTURE.md](../ARCHITECTURE.md) §4.2–4.11 · [BACKEND.md](../BACKEND.md) (общие правила бэкенда)
> **Закрывает:** K2, K4 (через API), K5, K6 · S1–S5, S10, S15 (экспорт), S17, S18 · A1, A6 · U2, U6, U8–U11

---

## 0. Как пользоваться этим файлом

- API-контракт (§1.4.1) — источник правды для фронтенда. Его снимок `openapi.json` лежит в корне репо backend и
  обновляется командой `lr export-openapi` (P1); CI падает, если снимок разошёлся с кодом. Фронт забирает копию
  командой `npm run sync:api` из соседнего клона `../backend`.
- Бизнес-логика — в `service.py` модулей, в роутерах её нет. Транзакцию коммитит сервисный слой на границе
  use case. Долгие операции (сбор, LLM) — **только в worker**, в запросе API их нет.
- Для агента: одна функция из §1.3 + нужный кусок §1.4 + команда проверки.

---

## 1. Этап 1 — MVP

### 1.1 Цель

Связать всё в работающий продукт: пользователь настраивает услуги, добавляет или находит компании, запускает анализ,
видит живой прогресс и получает ранжированные лиды с доказательствами. Пересчёт скоринга при смене настроек — мгновенный.
Всё крутится на своём сервере за Cloudflare.

### 1.2 Границы

| Входит | Не входит |
|---|---|
| FastAPI-приложение, модули домена, БД и Alembic, Taskiq worker и scheduler, адаптеры портов ai, SSE, сиды, OpenAPI, Docker, compose, CI | Логика сбора (parser), извлечения и скоринга (ai), хеширование паролей и JWT (auth) |
| Outbox `domain_event` и диспетчер без потребителей | Алерты, HubSpot, outreach — надстройки после ядра (§1.3, CO-A*) |
| `org_id` во всех таблицах (одна организация) | Мультитенантность, RLS — этап 2 |

### 1.3 Функции

| ID | Функция | Пр. | Проверка |
|---|---|---|---|
| CO-01 | Каркас: `AppSettings`, фабрика приложения, lifespan (пулы БД и Redis), structlog JSON (`request_id`, `run_id`), единый формат ошибок, `/health`, `/health/ready` | P0 | `pytest -k health` |
| CO-02 | БД: SQLAlchemy 2 async + asyncpg, Alembic (схемы `core` и `auth`, файлы `YYYY-MM-DD_slug.py`), модели §1.4.3, индексы | P0 | `alembic upgrade head` на пустой БД; `pytest -k models` |
| CO-03 | Подключение auth: `create_auth_router`, router-level `get_current_principal`, `require_roles("admin")` на запись конфигурации; P1 — проверка `Origin` для небезопасных методов | P0 | `pytest -k roles` (401, 403) |
| CO-04 | Config API: услуги, вопросы (версии, `keywords_status`), ICP, правила, профиль скоринга (неизменяемые версии), применение пресетов | P0 | `pytest -k config` |
| CO-05 | Синхронный пересчёт услуги после изменения весов, ICP, правил или профиля: `ai.score_company` по сохранённым сигналам, ≤ 2 с на 1 000 компаний, без LLM | P0 | `pytest -k rescore_perf` |
| CO-06 | Accounts API: CRUD компаний, нормализация домена, дедуп по домену, ручные поля (linkedin_url, careers_url, notes, tags) | P0 | `pytest -k companies` |
| CO-07 | Импорт CSV: шаблон по умолчанию (P0); маппинги `crunchbase` и ручной маппинг колонок (P1); отчёт created / updated / skipped / errors | P0/P1 | `pytest -k csv_import` |
| CO-08 | Discovery API: `search` (`parser.discover` + `ai.fit_score`, флаг `already_tracked`) и `accept` | P0 | `pytest -k discovery` (фейковый parser) |
| CO-09 | Runs API: создать (analyze / refresh), список, карточка со статусами компаний, `cancel`, `retry-failed` | P0 | `pytest -k runs` |
| CO-10 | SSE `GET\|POST /runs/{id}/events`: реплей из `run_event` + Redis pub/sub, `Last-Event-ID`, keep-alive 15 с (FastAPI) | P0 | `pytest -k sse` (httpx stream) |
| CO-11 | Worker на Taskiq + Redis: задачи `analyze_company`, `expand_question`; на старте worker — модель эмбеддингов, клиент Gemini, чекпоинтер | P0 | `pytest -k worker` (фейковые deps ai) |
| CO-12 | Адаптеры портов ai: `ParserCollector`, `SqlAnalysisStore`, `RedisProgressSink`, `SqlLLMCache`, `SqlUsageSink`; сборка графа с `AsyncPostgresSaver` | P0 | `pytest -k adapters` + E2E §1.9 |
| CO-13 | Leads API: список (фильтры, сортировка, пагинация, NEW за 7 дней) и карточка (breakdown, why_now, сигналы по вопросам, ЛПР, история, сводка источников) | P0 | `pytest -k leads` |
| CO-14 | `GET /companies/{id}/documents`: что просканировано (прозрачность) | P0 | `pytest -k documents` |
| CO-15 | Фидбек по сигналам (P0) и лидам (P1) + `GET /quality`: precision всего, по категориям и источникам, статистика верификатора | P0 | `pytest -k quality` |
| CO-16 | Meta API: индустрии и страны (из parser), пресеты (из ai), подписи категорий и перечисления | P0 | `pytest -k meta` |
| CO-17 | Outbox: `emit_event()` в той же транзакции + периодический диспетчер (реестр потребителей пуст) + `GET /activity` | P1 | `pytest -k outbox` |
| CO-18 | CLI `lr seed`: пресеты IA и Cyber, демо-аккаунты из CSV, пользователи admin и sales | P0 | `lr seed …` на пустой БД |
| CO-19 | CLI `lr export-openapi` (→ `openapi.json` в корне репо) + тест снимка в CI | P0 | `pytest -k openapi_snapshot` |
| CO-20 | `GET /leads/export.csv` (формат, пригодный для импорта в CRM) | P1 | `pytest -k export` |
| CO-21 | `GET /meta/usage`: вызовы и токены LLM за сутки по моделям против лимитов, документы по источникам | P1 | `pytest -k usage` |
| CO-22 | Scheduler: cron `refresh_tracked` (по умолчанию раз в 6 ч, инкрементально) + `resume_paused` раз в 15 мин | P1 | Ручной прогон задачи |
| CO-23 | `Dockerfile` (один образ), `docker-compose.yml` (`web` собирается из соседнего `../frontend`), `Caddyfile`, `cloudflared`, `.env.example` | P0 | `docker compose up` — всё healthy |
| CO-24 | CI (GitHub Actions): ruff, pytest (Postgres service), import-linter, снимок OpenAPI | P0 | Зелёный пайплайн |
| CO-A1 | Надстройка: `POST /leads/{company_id}/outreach` → `ai.generate_outreach` | После ядра | ARCHITECTURE §4.13 |
| CO-A2 | Надстройка: потребитель алертов (`signal.detected` сильный, `lead.tier_changed` → Telegram / e-mail) | После ядра | — |
| CO-A3 | Надстройка: HubSpot — `POST /leads/{company_id}/push/hubspot` (ручная отправка), `GET /integrations/hubspot` (для кнопки) и потребитель `lead.tier_changed`; общий клиент `modules/integrations/hubspot.py`, private app token, `FEATURE_HUBSPOT` | После ядра | `pytest core/tests/test_hubspot.py core/tests/test_integrations.py` |

### 1.4 Входы и выходы

#### 1.4.1 REST API (`/api/v1`, JSON в snake_case, UUID, время ISO 8601 UTC)

| Метод и путь | Роль | Назначение | Пр. |
|---|---|---|---|
| `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` | — / любой | Из пакета auth (см. его SPEC) | P0 |
| `GET/POST /auth/users` · `PATCH /auth/users/{id}` | admin | Управление пользователями | P1 |
| `GET /meta/industries` · `/meta/countries` · `/meta/presets` · `/meta/labels` | любой | Справочники для UI | P0 |
| `GET /meta/usage` | любой | Квоты LLM и объёмы сбора | P1 |
| `GET/POST /services` · `GET/PATCH /services/{id}` | чтение — любой, запись — admin | Услуги | P0 |
| `POST /presets/{key}/apply` | admin | Создать услугу из пресета (идемпотентно по slug) | P0 |
| `GET/POST /services/{id}/questions` · `PATCH/DELETE /questions/{id}` | admin для записи | Вопросы-сигналы (версии; вес меняет только скоринг) | P0 |
| `POST /questions/{id}/expand` | admin | Перегенерировать ключевые слова (задача worker) | P0 |
| `POST /services/{id}/questions/suggest` | admin | Черновики вопросов от AI (`ai.suggest_questions`) | P1 |
| `GET/PUT /services/{id}/icp` | admin для записи | ICP → синхронный пересчёт | P0 |
| `GET/POST /services/{id}/rules` · `PATCH/DELETE /rules/{id}` | admin для записи | Правила дисквалификации → пересчёт | P0 |
| `GET/PUT /services/{id}/scoring-profile` | admin для записи | Новая версия профиля → пересчёт → `{version, rescored, tier_changes, duration_ms}` | P0 |
| `GET/POST /companies` · `GET/PATCH/DELETE /companies/{id}` | любой | Аккаунты (удаление — admin) | P0 |
| `POST /companies/import` (multipart, `?mapping=default\|crunchbase\|custom`) | любой | Импорт CSV → отчёт | P0/P1 |
| `GET /companies/{id}/documents?source_type=` | любой | Просканированные источники | P0 |
| `POST /discovery/search` · `POST /discovery/accept` | любой | Автопоиск по ICP | P0 |
| `POST /runs` · `GET /runs` · `GET /runs/{id}` | любой | Прогоны анализа: `{kind: analyze\|refresh, mode: incremental\|full, company_ids (≥ 1), service_ids}` → `queued`; `full` — пересбор и повторное извлечение без учёта fingerprint | P0 |
| `GET\|POST /runs/{id}/events` | любой | SSE-поток прогона | P0 |
| `POST /runs/{id}/cancel` · `POST /runs/{id}/retry-failed` | любой | Управление прогоном (409 для завершённого прогона; worker останавливает граф между стадиями) | P0 |
| `POST /leads/{company_id}/outreach` → 202 · `GET /leads/{company_id}/outreach/{job_id}` | любой | Черновик outreach (CO-A1): задача worker `generate_outreach`, результат — опросом | После ядра |
| `GET /leads?service_id=&tier=&country=&industry=&min_priority=&has_new=&q=&sort=&page=&page_size=` | любой | Рейтинг | P0 |
| `GET /leads/{company_id}?service_id=` | любой | Карточка лида | P0 |
| `GET /leads/export.csv?service_id=&…` | любой | CSV-экспорт | P1 |
| `POST/DELETE /signals/{id}/feedback` · `POST /leads/{company_id}/feedback` | любой | Оценки (upsert: один голос на пользователя и цель; DELETE отзывает голос; ответ содержит пересчитанный score) | P0 / P1 |
| `GET /quality?service_id=` | любой | Метрики качества | P0 |
| `GET /activity?limit=50` | любой | Лента событий | P1 |
| `GET /health` · `GET /health/ready` (вне `/api/v1`) | — | Liveness и readiness (БД, Redis) | P0 |

Общие соглашения: пагинация `page`, `page_size` (≤ 100) → `{items, total, page, page_size}`; ошибки домена →
`{"error": {"code", "message", "details"}}`; 422 — стандартная валидация FastAPI; `response_model` и `status_code`
заданы у каждого эндпоинта (P6).

Ключевые схемы ответов (сокращённо):

```jsonc
// GET /leads → items[]
{"company": {"id": "…", "name": "DHL Group", "domain": "dhl.com", "country_code": "DE", "industry_ids": ["logistics"], "employees": 590000},
 "service_id": "…",
 "score": {"priority": 69.2, "tier": "hot", "fit": 92.0, "intent": 81.3, "risk": 38.1, "disqualified": false},
 "top_reasons": [{"text": "Uses agentic AI to process customer RFQs…", "source_name": "dhl.com", "date": "2026-06-18", "polarity": "positive"}],
 "flags": ["vendor"], "signals_count": 7, "new_signals_7d": 2, "last_signal_at": "2026-09-10", "analyzed_at": "2026-09-25T09:12:00Z"}

// GET /leads/{company_id}?service_id=
{"company": {"…": "все поля + notes, linkedin_url, careers_url, newsroom_url, ats"},
 "service": {"id": "…", "name": "Intelligent Automation"},
 "score": {"…": "поля выше + fit_details, rule_hits, data_gaps, breakdown[], why_now[], scoring_profile_version, computed_at"},
 "signals_by_question": [{"question": {"id": "…", "key": "ia_ai_projects", "text": "…", "category": "ai_automation", "polarity": "positive", "weight": "high"},
                          "strength": 0.83, "points": 2.49,
                          "signals": [{"id": "…", "quote": "…", "summary": "…", "strength": "strong", "confidence": 0.9, "url": "…",
                                       "source_name": "dhl.com", "source_type": "website", "event_date": "2026-06-18",
                                       "flags": [], "my_feedback": null}]}],
 "decision_makers": ["COO", "CIO", "Head of Digital Transformation", "Head of Automation", "Head of Process Excellence"],
 "history": [{"computed_at": "…", "priority": 61.0, "tier": "warm"}],
 "sources_summary": {"news": 23, "website": 12, "jobs": 41}}

// GET /quality
{"labeled": 124, "precision": 0.86,
 "by_category": [{"category": "hiring", "labeled": 30, "precision": 0.93}], "by_source": [{"source_type": "news", "labeled": 51, "precision": 0.80}],
 "verifier": {"evidence_total": 540, "rejected": {"quote_not_found": 12, "wrong_subject": 31, "stale": 8, "below_confidence": 20}},
 "hallucination_rate": 0.022}
```

**SSE** (`EventSourceResponse` из `fastapi.sse`, FastAPI ≥ 0.135). События: `run.progress` `{done, total, failed, paused}` ·
`company.stage` `{company_id, service_id?, stage, status, message}` · `company.done` `{company_id, scores: [{service_id, priority, tier}]}` ·
`run.finished` `{status}`. Имена и данные одинаковы в живом потоке и в реплее (имя выводится из `stage`/`status` строки `run_event`). У каждого события `id` = `run_event.id`: при переподключении с `Last-Event-ID` сначала отдаётся
реплей из БД. POST-вариант того же пути нужен для клиентов за прокси, которые буферизуют GET-SSE.

#### 1.4.2 Вход и выход worker (Taskiq)

```python
@broker.task(task_name="analyze_company", retry_on_error=False)
async def analyze_company(run_id: str, company_id: str, service_ids: list[str], mode: str = "incremental") -> None: ...
@broker.task(task_name="expand_question")
async def expand_question(question_id: str) -> None: ...
@broker.task(task_name="refresh_tracked", schedule=[{"cron": settings.refresh_cron}])     # P1
async def refresh_tracked() -> None: ...
@broker.task(task_name="dispatch_events", schedule=[{"cron": "* * * * *"}])               # P1
async def dispatch_events() -> None: ...
```

Идемпотентность: повтор `analyze_company` с тем же `run_id:company_id` продолжает граф с чекпоинта.
Статусы компаний в прогоне берутся из последнего `run_event` по компании.

#### 1.4.3 Модель данных (схема `core`; у всех таблиц `id uuid`, `org_id`, `created_at`, `updated_at`)

| Таблица | Поля | Индексы и ограничения |
|---|---|---|
| `org` | name | — |
| `service` | name, slug, description, value_proposition, decision_makers text[], is_active | uq(org_id, slug) |
| `signal_question` | service_id, key, text, category, polarity, weight, source_types text[], recency_days, keywords jsonb, job_titles text[], negative_terms text[], keywords_status (pending / ready / failed), version, is_active | uq(service_id, key) |
| `icp_profile` | service_id, countries text[], industries_any text[], employees_min, employees_max, revenue_min_eur, nice_to_have jsonb, version | uq(service_id) |
| `disqualification_rule` | service_id, name, kind, condition jsonb, action, cap_value, is_active | idx(service_id) |
| `scoring_profile` | service_id, version, params jsonb, is_current | uq(service_id, version); частичный индекс по `is_current` |
| `company` | name, domain, aliases text[], own_domains text[], country_code, industry_ids text[], employees, revenue_eur, hq_city, wikidata_qid, lei, crunchbase_id, homepage_url, careers_url, newsroom_url, ats jsonb, linkedin_url, notes, tags text[], origin (csv / manual / discovery), is_tracked, resolved_at, last_analyzed_at | uq(org_id, domain) |
| `document` | company_id, source_type, source_name, url, canonical_url, title, text, published_at, fetched_at, language, content_hash, meta jsonb | uq(company_id, content_hash); idx(company_id, source_type, published_at desc) |
| `document_chunk` | document_id, company_id, ord, text, char_start, char_end, embedding vector(384) | idx(company_id); HNSW — на этапе 2 |
| `analysis_run` | kind, status, params jsonb, progress jsonb, stats jsonb, error, created_by, started_at, finished_at | idx(org_id, created_at desc) |
| `run_event` | run_id, company_id, service_id, stage, status, message, payload jsonb (id — bigserial для SSE) | idx(run_id, id) |
| `extraction_state` | company_id, service_id, fingerprint, prompt_version, extracted_at | uq(company_id, service_id) |
| `signal` | company_id, service_id, question_id, question_key, question_version, document_id, chunk_id, run_id, category, polarity, quote, quote_start, quote_end, summary, strength, confidence, reliability, event_date, published_at, url, source_type, source_name, flags text[], status (active / superseded / rejected_by_user), model, prompt_version, detected_at | idx(company_id, service_id, status) |
| `rejected_evidence` | company_id, service_id, question_id, run_id, quote, reason | idx(service_id, reason) |
| `lead_score` | company_id, service_id, scoring_profile_id, fit, intent, risk, priority, tier, disqualified, rule_hits jsonb, fit_details jsonb, breakdown jsonb, why_now jsonb, data_gaps jsonb, computed_at, is_current | частичный idx(org_id, service_id, priority desc) по `is_current` |
| `feedback` | user_id, target_type (signal / lead), target_id, service_id, verdict, reason | uq(user_id, target_type, target_id) |
| `llm_call` | run_id, purpose, model, prompt_version, input_tokens, output_tokens, latency_ms, cache_hit, status, error | idx(model, created_at) |
| `llm_cache` | key (pk), response jsonb, model, prompt_version | — |
| `domain_event` | type, payload jsonb, processed_at | частичный idx по `processed_at is null` |

Замечания: `signal` при повторном извлечении той же услуги: старые активные сигналы → `superseded`, новые → `active`
(история не теряется). Отметка пользователя `incorrect` → `rejected_by_user`: такой сигнал не участвует в скоринге,
пересчёт компании запускается сразу. Схема `auth` принадлежит пакету auth. Внешних ключей на `auth.*` нет —
хранится только `user_id` / `created_by` (uuid).

### 1.5 Зависимости

| Тип | Что |
|---|---|
| Workspace | `leadradar-parser`, `leadradar-ai`, `leadradar-auth` — через `[tool.uv.sources] {workspace = true}` |
| Python | `fastapi` (≥ 0.135, нативный SSE), `uvicorn[standard]`, `sqlalchemy[asyncio]` 2, `asyncpg`, `alembic`, `pgvector`, `pydantic-settings`, `taskiq`, `taskiq-redis`, `taskiq-fastapi`, `redis`, `langgraph-checkpoint-postgres` + `psycopg[binary,pool]`, `structlog`, `python-multipart`, `rapidfuzz`, `typer`; dev: `pytest`, `pytest-asyncio`, `httpx`, `ruff`, `import-linter` |
| Инфраструктура | PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`), Redis 7, Caddy 2, cloudflared |
| Внешние | Через пакеты: Gemini (ai), источники (parser) |

```dotenv
APP_ENV=dev                                   # dev | demo
APP_PUBLIC_ORIGIN=https://leadradar.<домен>   # для проверки Origin и cookie
DATABASE_URL=postgresql+asyncpg://leadradar:leadradar@postgres:5432/leadradar
LANGGRAPH_DB_URL=postgresql://leadradar:leadradar@postgres:5432/leadradar   # psycopg, схема langgraph
REDIS_URL=redis://redis:6379/0
WORKER_MAX_ASYNC_TASKS=3
REFRESH_CRON="0 */6 * * *"
FEATURE_OUTREACH=false
FEATURE_ALERTS=false
FEATURE_HUBSPOT=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
HUBSPOT_PRIVATE_APP_TOKEN=
CF_TUNNEL_TOKEN=
# + переменные AUTH_* (auth), LLM_* и AI_* (ai), PARSER_* (parser)
```

### 1.6 Структура папки

```
core/
├── SPEC.md
├── pyproject.toml                       # name = "leadradar-core"; scripts: lr = "leadradar_core.cli:app"
├── alembic.ini · migrations/            # env.py: target_metadata = [core_metadata, auth_metadata]
├── seeds/ demo_accounts.csv
├── src/leadradar_core/
│   ├── main.py                          # create_app(): роутеры, middleware, exception handlers, lifespan
│   ├── settings.py · logging.py · errors.py · pagination.py
│   ├── db/ base.py · session.py · types.py
│   ├── modules/                         # у каждого модуля: models.py, schemas.py, repository.py, service.py, router.py
│   │   ├── config/                      # услуги, вопросы, ICP, правила, профили, пресеты, rescore
│   │   ├── accounts/                    # компании, импорт CSV, discovery
│   │   ├── intelligence/                # документы, фрагменты, сигналы, отклонённое, extraction_state
│   │   ├── leads/                       # lead_score, список и карточка, экспорт
│   │   ├── runs/                        # прогоны, события, SSE
│   │   ├── feedback/                    # фидбек, quality
│   │   ├── activity/                    # outbox, диспетчер, лента (P1)
│   │   └── meta/                        # справочники, usage
│   ├── adapters/                        # реализации портов ai + маппинг контрактов parser ↔ ai ↔ ORM
│   │   ├── collector.py · store.py · progress.py · llm_cache.py · usage.py · mapping.py
│   ├── worker/ broker.py · tasks.py · deps.py        # deps: сборка AnalysisDeps и графа на старте worker
│   ├── integrations/                    # надстройки: alerts.py, hubspot.py (после ядра, за флагами)
│   └── cli.py                           # lr seed | export-openapi | run-analysis | rescore
└── tests/
```

### 1.7 Ключевые решения

- **Граница транзакции.** Роутер → сервис (use case, `async with session.begin()`) → репозитории. `emit_event()` пишет
  в outbox в той же транзакции.
- **Пересчёт (CO-05)** одним проходом: бандл услуги (вопросы, ICP, правила, текущий профиль) + все компании + активные
  сигналы одним запросом (группировка в Python) → `ai.score_company` → `is_current=false` у старых строк, вставка новых
  (bulk). Меняется только вес вопроса → версия вопроса не растёт, только пересчёт. Меняется текст, полярность,
  источники или окно → версия +1, `expand_question`, вопрос помечен stale для повторного анализа.
- **Адаптер `ParserCollector`:** `CompanyProfile` → `parser.CompanyRef` → `resolve_company` → обновить компанию →
  `parser.collect(CollectPlan)` → вставить `document` с `ON CONFLICT (company_id, content_hash) DO NOTHING` →
  вернуть документы окна как `ai.AnalysisDocument`. Весь маппинг — в `adapters/mapping.py`.
- **Прогресс:** `RedisProgressSink.emit` → строка `run_event` (коммит) → `PUBLISH run:{run_id}` с id события.
  Обновление `analysis_run.progress` — атомарный `UPDATE … jsonb_set`.
- **Завершение прогона:** последняя компания (done + failed + paused = total) → статус `succeeded` / `partial` /
  `failed` → событие `run.finished` → `domain_event run.finished`.
- **Квоты:** `QuotaExhausted` от ai → компания `paused`; `resume_paused` (P1) или кнопка «Retry failed» продолжают с чекпоинта.
- **Импорт CSV:** домен из URL (без схемы, `www.` и пути, нижний регистр); страна — ISO2 или название → ISO2 через
  `parser.country_catalog()`; индустрия — id или fuzzy-сопоставление с таксономией (rapidfuzz ≥ 85, иначе пусто);
  ≤ 5 000 строк; дедуп по домену (обновляем пустые поля).
- **Discovery:** синхронно с тайм-аутом 60 с (лимит Wikidata). Кандидаты → `ai.fit_score` → сортировка по Fit и размеру.
- **Безопасность:** cookie-JWT (same-origin через Caddy), проверка `Origin` у POST / PUT / PATCH / DELETE (P1),
  лимит размера загрузки 5 МБ, в `APP_ENV=demo` Swagger только для admin или выключен (P6).
- **Деплой:** репо backend и frontend клонированы рядом; `docker compose up -d` — `postgres`, `redis`, `api`, `worker`,
  `scheduler`, `web` (сборка из `../frontend`; Caddy: статика и
  `reverse_proxy /api/* api:8000 { flush_interval -1 }`, без сжатия для SSE-путей), `cloudflared` (named tunnel).
  Модель эмбеддингов прогревается при сборке образа.

### 1.8 План работ P1 (часы от старта)

| Окно | Задачи |
|---|---|
| H0–H2 | Корневой `pyproject.toml` (workspace, ruff, pytest, import-linter), compose (pg + redis), CO-01, CO-24, корневой `CLAUDE.md` |
| H2–H8 | CO-02, пакет auth (≈ 3 ч, см. его SPEC), CO-03, CO-04, CO-06, CO-16; **OpenAPI → фронт на H4 и H8** (CO-19) |
| H8–H12 | CO-11, CO-12 (с P2 и P3), CO-09, CO-10; **H11: DHL end-to-end + деплой через Cloudflare + проверка SSE** |
| H12–H24 | CO-08, CO-07, CO-05, CO-13, CO-14, CO-15, CO-18; **H24 — заморозка OpenAPI** |
| H24–H32 | Пакетный прогон демо-аккаунтов (с P2, следить за квотами), багфиксы; P1: CO-17, CO-21 |
| H32–H40 | Тесты, CO-22, CO-20, README, финальный деплой, `pg_dump` |
| H36–H44 | Надстройки CO-A2, CO-A3 (и CO-A1 с P3), если DoD ядра выполнен |

### 1.9 Критерии готовности (DoD)

- [ ] `docker compose up -d` поднимает всё; `GET /health/ready` = ok; `alembic upgrade head` на пустой БД без ошибок.
- [ ] `lr seed --presets intelligent_automation,cybersecurity --accounts seeds/demo_accounts.csv` создаёт 2 услуги,
      ≥ 40 компаний, пользователей admin и sales.
- [ ] **E2E:** login admin → `POST /runs` для DHL (обе услуги) → SSE показывает стадии → прогон завершается ≤ 3 мин →
      `GET /leads?service_id=<IA>` содержит DHL с priority, tier и ≥ 3 причинами → `GET /leads/{dhl}` отдаёт breakdown и
      сигналы с проверенными цитатами и URL.
- [ ] `PUT /services/{id}/scoring-profile` → ответ ≤ 2 с, рейтинг изменился, число строк `llm_call` не изменилось.
- [ ] Sales не может менять конфигурацию (403), аноним получает 401; cookie `HttpOnly; Secure; SameSite=Lax` на сервере.
- [ ] Kill worker посреди прогона → после рестарта `POST /runs/{id}/retry-failed` доводит компании до конца с чекпоинта.
- [ ] SSE доходит до браузера через Cloudflare в реальном времени (иначе фронт переходит на polling — проверить оба пути).
- [ ] `pytest` зелёный, `lint-imports` зелёный, снимок `openapi.json` в корне репо совпадает с кодом.
- [ ] `GET /quality` возвращает precision по ≥ 100 размеченным сигналам (после разметки командой).

### 1.10 Какие критерии закрывает модуль (MVP)

| ID | Как |
|---|---|
| K2, S1–S5 | Config API: услуги, вопросы, веса, полярность, источники, ICP, правила, профиль с версиями + мгновенный пересчёт |
| K4 | API под задачи продажника: рейтинг с причинами, карточка с доказательствами, живой прогресс, фидбек |
| K5 | Очередь, идемпотентные прогоны, чекпоинты, SSE с реплеем, health-checks, CI, compose, изоляция LLM от запросов API |
| K6 | `org_id`, конфиг вместо кода, импорт CSV, экспорт (P1), метрики использования |
| K1 | Фидбек → precision; отметка «incorrect» исключает сигнал из скоринга |
| S10 | Поля `linkedin_url` и заметки, список ЛПР из услуги, никаких интеграций с LinkedIn |
| S6 | Импорт CSV-выгрузки Crunchbase (маппинг `crunchbase`, P1) |
| S15 | CSV-экспорт (P1), надстройка HubSpot (CO-A3) через outbox |
| S17, S18 | `why_now` и breakdown в API; инкрементальные прогоны, cron (P1), NEW за 7 дней |
| U6, U8–U11 | Импорт и discovery; outbox под надстройки; деплой за Cloudflare; монолит; роли |

---

## 2. Этап 2 — Gateway/BFF + доменные сервисы

### 2.1 Цель

Надёжная мультитенантная платформа: core превращается в gateway/BFF и набор доменных сервисов, общающихся через шину
событий. Появляются интеграции, уведомления, публичный API и эксплуатационная зрелость.

### 2.2 Функции

| Область | Функции |
|---|---|
| Архитектура | Модули `config`, `accounts`, `leads`, `runs` выделяются по мере нагрузки; outbox-диспетчер публикует в NATS JetStream (Kafka при росте); порты ai и parser — сетевые клиенты |
| Мультитенантность | `org_id` уже везде → Postgres RLS, изоляция конфигов и квот, админка тенанта |
| Интеграции | HubSpot двусторонне (компании, свойства скора, заметки с доказательствами; исходы сделок → метки ML), Salesforce и Pipedrive через тот же интерфейс, webhooks, публичный API с ключами |
| Уведомления | Правила алертов на пользователя («Tier A в DACH + смена руководства»), дайджесты, Slack / Teams / Telegram / e-mail |
| Продукт | Сохранённые представления, территории (рынки → пользователи), заметки и задачи, аудит-лог |
| Данные | Партиционирование `document` по месяцам, архив в S3, ClickHouse для аналитики, HNSW-индекс по `document_chunk` |
| Эксплуатация | OpenTelemetry, Prometheus / Grafana, Sentry, SLO (99.5%), DLQ, rate limiting API, blue-green деплой, IaC, резервные копии и PITR |

### 2.3 Входы и выходы (изменения)

`/api/v2` появляется только при несовместимых изменениях; до этого поля только добавляются. Новые ресурсы:
`/integrations/*`, `/alerts/rules`, `/views`, `/territories`, `/api-keys`, `/webhooks`, `/audit`.

### 2.4 Зависимости

Шина событий, Identity-сервис (OIDC), S3, ClickHouse (опц.), наблюдаемость, секрет-хранилище.

### 2.5 Критерии готовности этапа 2

p95 API ≤ 300 мс для списков; 99.5% доступности; изоляция тенантов доказана тестами RLS; HubSpot-синхронизация
идемпотентна (повтор не создаёт дублей); восстановление из бэкапа ≤ 1 ч.

### 2.6 Критерии, которые усиливает этап 2

K5 (надёжность, наблюдаемость), K6 (мультитенантность, интеграции, ROI-аналитика), S15 (HubSpot), S18 (алерты).

---

## 3. Рост и развитие: что заложено в MVP и как расширять

| Заложено в MVP | Зачем | Как растёт на этапе 2 |
|---|---|---|
| Доменные модули с `router / service / repository` | Изоляция и понятные границы | Модуль выносится в сервис с тем же API |
| `org_id` во всех таблицах | Единственная организация сейчас | RLS и мультитенантность без миграции данных |
| Outbox `domain_event` + диспетчер + реестр потребителей | Надстройки без изменения ядра | Публикация в шину; алерты, CRM и webhooks — потребители |
| Адаптеры портов в `adapters/` | Единственное место связи с ai и parser | Замена на HTTP или очередь, код модулей не меняется |
| Версии `scoring_profile` и `signal_question`, `prompt_version` в сигналах | Воспроизводимость | A/B профилей, аудит, откат |
| `run_event` с id + SSE-реплей | Надёжный real-time | Тот же поток для уведомлений и WebSocket |
| `llm_call` и `/meta/usage` | Контроль квот | Биллинг, лимиты тенантов, стоимость на лид |
| Фидбек (сигнал и лид) | Точность | Метки для ML и калибровки, аналитика качества |
| Один Docker-образ с командами api / worker / scheduler | Простой деплой | Раздельные деплойменты и автоскейл по очередям |
| Снимок OpenAPI в CI | Контракт с фронтом | Версионирование API, SDK для партнёров |

---

## 4. Риски и анти-паттерны

- **Не вызывать LLM и не ходить в сеть в обработчике HTTP-запроса** (кроме discovery с тайм-аутом) — только через worker.
- **Не держать бизнес-логику в роутерах** и не коммитить внутри репозиториев (ломается атомарность use case).
- **N+1 в `GET /leads`:** один запрос с join на текущий `lead_score` и агрегатами сигналов; причины уже лежат в `why_now`.
- **Не менять OpenAPI после H24** без согласования с F1 и F2; каждое изменение — новый снимок (`lr export-openapi`),
  а во frontend — `npm run sync:api && npm run gen:api`.
- Не хранить секреты в репозитории; `.env` только локально и на сервере.
- Cloudflare quick tunnel буферизует GET-SSE — используем named tunnel, POST-SSE и polling-fallback.
