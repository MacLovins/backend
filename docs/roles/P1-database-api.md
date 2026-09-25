# P1 — База данных, API и инфраструктура

> Памятка для бэкендера, который отвечает за БД, API, очередь, авторизацию и деплой.
> Подробное ТЗ: [core/SPEC.md](../../core/SPEC.md) · [auth/SPEC.md](../../auth/SPEC.md) ·
> [BACKEND.md](../../BACKEND.md) · общая картина: [ARCHITECTURE.md](../../ARCHITECTURE.md) §4.

---

## 1. Твоя зона

Ты собираешь продукт в одно целое. Парсер (P2) и AI (P3) — это библиотеки, которые ничего не хранят и не знают про HTTP.
Всё, что связано с хранением, API, очередью, пользователями и сервером, — твоё.

| Отвечаешь за | Папка |
|---|---|
| Корень uv workspace, `docker-compose.yml`, `Caddyfile`, `.env.example`, CI | корень репо `backend` |
| Схема БД, модели SQLAlchemy, миграции Alembic | `core/src/leadradar_core/db`, `modules/*/models.py`, `migrations/` |
| REST API и SSE для фронта, снимок `openapi.json` | `core/src/leadradar_core/modules/*/router.py` |
| Очередь Taskiq и worker, который запускает граф P3 | `core/src/leadradar_core/worker/` |
| Адаптеры портов AI: сохранение документов, сигналов, скоров, прогресс | `core/src/leadradar_core/adapters/` |
| Вход, JWT, роли `admin` / `sales` | `auth/` |
| Сиды: пресеты, демо-компании, пользователи | `lr seed` |
| Сервер за Cloudflare, резервный дамп БД | compose + cloudflared |

**Не твоё:** как собирать данные (P2), промпты и формула скоринга (P3), экраны (F1, F2).
Твоя работа — вызвать их публичные функции и сохранить результат.

---

## 2. Первые 2 часа (M0)

1. Корневой `pyproject.toml`: workspace-members `parser`, `ai`, `auth`, `core`; ruff, pytest,
   import-linter (контракты — [BACKEND.md](../../BACKEND.md) §1.5).
2. `docker-compose.yml` с `postgres` (`pgvector/pgvector:pg16`) и `redis:7-alpine` + healthchecks.
3. Скелет `leadradar-core`: `create_app()`, `settings.py`, `/health`, `/health/ready`, structlog.
4. CI: ruff + pytest + lint-imports.
5. Корневой `CLAUDE.md` (≤ 20 строк): команды, ссылки на ARCHITECTURE и SPEC, правило «тесты без сети».
6. Договориться с P3 о портах (`Collector`, `AnalysisStore`, `ProgressSink`) — [ai/SPEC.md](../../ai/SPEC.md) §1.4.3.

---

## 3. База данных

Postgres 16 + pgvector. Три схемы: `core` (ты), `auth` (пакет auth, миграции тоже у тебя), `langgraph` (чекпоинты графа,
создаются сами через `AsyncPostgresSaver.setup()`).

**Порядок создания таблиц** (одна миграция на группу, имена файлов `YYYY-MM-DD_slug.py`):

| Шаг | Таблицы | Кому нужно первым |
|---|---|---|
| 1 | `org`, `auth.user_account` | вход для фронта |
| 2 | `service`, `signal_question`, `icp_profile`, `disqualification_rule`, `scoring_profile` | настройки (F2), пресеты (P3) |
| 3 | `company` | аккаунты, discovery |
| 4 | `document`, `document_chunk`, `extraction_state` | сбор (P2) и индексация (P3) |
| 5 | `analysis_run`, `run_event` | прогоны и SSE |
| 6 | `signal`, `rejected_evidence`, `lead_score` | результаты AI |
| 7 | `feedback`, `llm_call`, `llm_cache`, `domain_event` | качество, квоты, outbox |

Поля и индексы — [core/SPEC.md](../../core/SPEC.md) §1.4.3. Главные правила:

- У всех таблиц `core` есть `id uuid`, `org_id`, `created_at`, `updated_at`.
- `company`: уникальность `(org_id, domain)`. Домен всегда нормализован: без схемы, `www.` и пути, в нижнем регистре.
- `document`: уникальность `(company_id, content_hash)`. Вставка через `ON CONFLICT DO NOTHING` — это дедуп между прогонами.
- `scoring_profile` не редактируется: каждое сохранение создаёт новую версию с `is_current=true`.
- `lead_score`: старые строки получают `is_current=false`, новая вставляется. Для списка лидов нужен частичный индекс
  `(org_id, service_id, priority desc) WHERE is_current`.
- `signal`: при повторном извлечении старые → `superseded`, новые → `active`. Отметка пользователя «Wrong» →
  `rejected_by_user`, сигнал выпадает из скоринга.
- Внешних ключей на `auth.*` нет — только `user_id uuid`. Так auth можно вынести отдельно на этапе 2.

---

## 4. API (в порядке, в котором его ждёт фронт)

| Когда | Что отдать | Кто ждёт |
|---|---|---|
| H4 | Первый `openapi.json`: auth, meta, services, questions, companies (пусть с заглушками) | F1, F2 — генерируют клиент и моки |
| H8 | Рабочие auth, config CRUD, companies CRUD, `POST /runs`, `GET /runs/{id}` | F1, F2 |
| H11 | SSE `/runs/{id}/events`, `GET /leads`, `GET /leads/{id}` на реальных данных DHL | F1 |
| H12–H24 | discovery, импорт CSV, scoring-profile + пересчёт, quality, documents | F2, F1 |
| **H24** | **Заморозка OpenAPI.** Дальше менять только по согласию F1 и F2 | все |

После любого изменения API: `lr export-openapi` → коммит `openapi.json` → сообщить фронту
(`npm run sync:api && npm run gen:api` у них).

Правила: логика в `service.py`, в роутере её нет. Транзакция — одна на use case. Сеть и LLM в обработчиках запросов
не вызываются, только через worker. Исключение — discovery с тайм-аутом 60 с.

---

## 5. Worker и связка с AI и парсером

1. `worker/broker.py`: `ListQueueBroker(REDIS_URL)`; на старте worker создаются `FastEmbedder`, `GeminiClient`,
   `AsyncPostgresSaver` (всё это — из публичного API `leadradar_ai`).
2. `adapters/collector.py` — `ParserCollector`: `CompanyProfile` → `parser.resolve_company` → обновить компанию →
   `parser.collect` → вставить документы с дедупом → вернуть `AnalysisDocument[]`.
3. `adapters/store.py` — `SqlAnalysisStore`: фрагменты, сигналы, отклонённое, fingerprint, скоры.
4. `adapters/progress.py` — `RedisProgressSink`: строка в `run_event` + `PUBLISH run:{run_id}`.
5. `worker/tasks.py`: `analyze_company(run_id, company_id, service_ids)` → `graph.ainvoke(..., thread_id=f"{run_id}:{company_id}")`.
   `QuotaExhausted` → статус компании `paused`, её можно продолжить.
6. Пересчёт (`PUT /scoring-profile`, ICP, правила): загрузить сигналы всех компаний одним запросом →
   `ai.score_company` → записать. Цель — ≤ 2 с на 1 000 компаний, без вызовов LLM.

Весь маппинг между контрактами parser, ai и ORM — только в `adapters/mapping.py`.

---

## 6. Авторизация (≈ 3 ч в окне H2–H8)

PyJWT + `pwdlib[argon2]`; cookie `lr_session` (`HttpOnly; Secure; SameSite=Lax`); dummy-hash против timing-атак;
роли `admin` / `sales`; `require_roles("admin")` на запись настроек. Детали — [auth/SPEC.md](../../auth/SPEC.md).

---

## 7. Деплой (H11 — первый, H32–H40 — финальный)

- На сервере оба репо клонированы рядом (`../frontend` нужен для сборки `web`).
- Сервисы: `postgres`, `redis`, `api`, `worker`, `scheduler`, `web` (Caddy), `cloudflared` (**named tunnel**:
  quick tunnel задерживает SSE).
- Caddy: `reverse_proxy /api/* api:8000 { flush_interval -1 }`, без сжатия SSE.
- На H11 проверить, что прогресс анализа приходит в браузер через Cloudflare в реальном времени.
  Если нет — фронт перейдёт на polling, это нормально.
- Перед демо: `pg_dump` прогнанной базы, проверка локального запуска.

---

## 8. План по часам

| Окно | Что делаешь | Результат |
|---|---|---|
| H0–H2 | Workspace, compose, скелет core, CI, CLAUDE.md | `docker compose up postgres redis`, `/health` = ok |
| H2–H8 | Миграции шаги 1–7, auth, config CRUD, companies CRUD, meta | OpenAPI на H4 и H8 |
| H8–H12 | Taskiq worker, адаптеры, runs API, SSE, деплой | **H11: DHL end-to-end на сервере** |
| H12–H24 | Discovery, импорт CSV, профиль скоринга + пересчёт, leads, quality, сиды | **H24: заморозка OpenAPI** |
| H24–H32 | Пакетный прогон 40–60 демо-компаний вместе с P2, следить за квотами Gemini | Данные для демо |
| H32–H40 | Тесты, cron-обновление (P1), CSV-экспорт (P1), финальный деплой, дамп | Code freeze на H40 |
| H36–H44 | Надстройки, если ядро готово: алерты, HubSpot | За feature-флагами |

---

## 9. Готово, когда

- [ ] `docker compose up -d` — всё healthy; миграции накатываются на пустую БД.
- [ ] `lr seed …` создаёт 2 услуги, ≥ 40 компаний, admin и sales.
- [ ] Прогон DHL: стадии идут через SSE, за ≤ 3 мин DHL появляется в `/leads` с причинами и цитатами.
- [ ] Смена веса → пересчёт ≤ 2 с, в `llm_call` новых строк нет.
- [ ] sales получает 403 на запись настроек, аноним — 401.
- [ ] Убили worker посреди прогона → `retry-failed` доводит прогон до конца.
- [ ] pytest, lint-imports, снимок OpenAPI — зелёные.

---

## 10. Частые ошибки

- Коммит внутри репозитория или сервиса по частям — атомарность use case ломается.
- N+1 в `GET /leads`: нужен один запрос с join на текущий `lead_score`; причины уже лежат в `why_now`.
- Импорт внутренностей `leadradar_ai.pipeline` или `leadradar_parser.adapters` — CI это запрещает, бери только публичный API.
- Молча поменял схему ответа — у фронта сломалась генерация. Сначала предупреди.
- Секреты в репозитории. `.env` только локально и на сервере.

## 11. Промпт для Claude Code

```text
Контекст: @ARCHITECTURE.md §4.6–4.7, @core/SPEC.md §1.4.3.
Задача: CO-02 — модели SQLAlchemy и миграция Alembic для таблиц шага 4 (document, document_chunk, extraction_state).
Вне рамок: API, адаптеры. Проверка: `alembic upgrade head` на пустой БД и `uv run pytest -k models`. Сначала план.
```
