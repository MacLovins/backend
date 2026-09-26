# Бэкенд — общие правила серверной части

> **Роль:** общие правила для Python-пакетов репо (`parser`, `ai`, `auth`, `core`): процессы, границы,
> владение БД, соглашения, сборка и CI. `Dockerfile` единого образа лежит в корне репо.
> **Владелец:** P1 — Backend lead
> **Пакеты:** [core](core/SPEC.md) (`leadradar-core`, приложение) · [ai](ai/SPEC.md)
> (`leadradar-ai`, AI-движок) · [auth](auth/SPEC.md) (`leadradar-auth`, вход и роли)
> **Связано:** [ARCHITECTURE.md](ARCHITECTURE.md) §4.2–4.4, §4.6, §4.9, §5.3
> **Закрывает:** U1, U3, U10 · K5 (надёжность и сборка) · S16 (стек)

---

## 1. Этап 1 — MVP

### 1.1 Цель

Три пакета бэкенда и `parser` развиваются параллельно тремя разработчиками и при этом собираются в один работающий
монолит: один образ, три процесса, одна БД. Границы между пакетами проверяются автоматически, чтобы на этапе 2
вынести их в сервисы без переписывания.

### 1.2 Архитектура бэкенда

```
             ┌──────────── один Docker-образ (Dockerfile), разные команды ──────────────────────┐
             │  api        uvicorn leadradar_core.main:app                                      │
             │  worker     taskiq worker leadradar_core.worker.broker:broker                    │
             │  scheduler  taskiq scheduler leadradar_core.worker.broker:scheduler  (P1 cron)   │
             └─────────────────────────────────────────────────────────────────────────────────┘
leadradar_core (core) ──импортирует публичный API──▶ leadradar_ai (ai)
                                 ──────────────────────────────▶ leadradar_auth (auth)
                                 ──────────────────────────────▶ leadradar_parser (parser/)
leadradar_ai ──порты (Protocol)──▶ реализации в leadradar_core/adapters (SQL, Redis, parser)
```

| Процесс | Что делает | Масштаб в MVP |
|---|---|---|
| `api` | REST, SSE, синхронный пересчёт скоринга, discovery (≤ 60 с) | 1 процесс, uvicorn |
| `worker` | Граф анализа, генерация ключевых слов; держит в памяти модель эмбеддингов, клиент Gemini, чекпоинтер | 1 процесс, `--max-async-tasks 3` (лимитер LLM — внутри процесса) |
| `scheduler` | Cron: обновление отслеживаемых компаний, продолжение прогонов на паузе, диспетчер outbox | 1 процесс (P1) |

### 1.3 Функции

| ID | Функция | Пр. | Проверка |
|---|---|---|---|
| BE-01 | uv workspace: корневой `pyproject.toml` (members, dev-группа, ruff, pytest, import-linter), один `uv.lock` | P0 | `uv sync --all-packages` |
| BE-02 | Контракты импорта (import-linter) §1.5 | P0 | `uv run lint-imports` |
| BE-03 | `Dockerfile`: python 3.12-slim + uv, слой зависимостей, прогрев модели эмбеддингов | P0 | `docker build -f Dockerfile .` |
| BE-04 | Общие соглашения §1.6 (settings, логи, ошибки, время, id, тесты) | P0 | Ревью |
| BE-05 | Тестовая инфраструктура: Postgres-фикстура для core (Alembic upgrade один раз на сессию, транзакция с откатом на тест), `@pytest.mark.live` вне CI | P0 | `uv run pytest` |
| BE-06 | Корневой `CLAUDE.md` (≤ 20 строк): команды, ссылки на ARCHITECTURE и SPEC, правила границ, «тесты без сети» | P0 | Файл есть |

### 1.4 Входы, выходы и владение данными

| Ресурс | Владелец | Кто ещё пользуется | Правило |
|---|---|---|---|
| Схема `core` (Alembic) | leadradar-core | — | Миграции только в `core/migrations`, имя `YYYY-MM-DD_slug.py` |
| Схема `auth` | leadradar-auth (`auth_metadata`) | core читает через API пакета | Миграции в том же Alembic env (список metadata); без FK из `core` |
| Схема `langgraph` | чекпоинтер LangGraph | ai через порт | Создаётся `AsyncPostgresSaver.setup()` при старте worker |
| Redis | core | ai не знает о Redis | Очередь Taskiq + канал `run:{id}` |
| Кэш HTTP parser | parser (`PARSER_CACHE_DIR`) | — | Volume в compose |
| Модель эмбеддингов | ai | — | Кэш fastembed в образе |

### 1.5 Контракты импорта (корневой `pyproject.toml`)

```toml
[tool.uv.workspace]
members = ["parser", "ai", "auth", "core"]

[tool.importlinter]
root_packages = ["leadradar_parser", "leadradar_ai", "leadradar_auth", "leadradar_core"]

[[tool.importlinter.contracts]]
name = "Libraries never import the app"
type = "forbidden"
source_modules = ["leadradar_parser", "leadradar_ai", "leadradar_auth"]
forbidden_modules = ["leadradar_core"]

[[tool.importlinter.contracts]]
name = "Libraries are independent of each other"
type = "independence"
modules = ["leadradar_parser", "leadradar_ai", "leadradar_auth"]

[[tool.importlinter.contracts]]
name = "Core uses only public APIs of libraries"
type = "forbidden"
source_modules = ["leadradar_core"]
forbidden_modules = ["leadradar_ai.pipeline", "leadradar_ai.llm", "leadradar_ai.retrieval",
                     "leadradar_parser.adapters", "leadradar_parser.http", "leadradar_auth.security"]
```

### 1.6 Общие соглашения

- **Python 3.12**, uv, `src/`-layout в каждом пакете, `ruff check` и `ruff format` (line-length 110).
- **Async для I/O везде.** CPU-работа (эмбеддинги) только в worker (P6).
- **Контракты — Pydantic v2.** Входные схемы API: `extra="forbid"`. Поля контрактов между пакетами только добавляются.
- **Настройки — pydantic-settings, отдельный класс на пакет:** `PARSER_*`, `LLM_*` / `AI_*`, `AUTH_*`,
  у core — `APP_*`, `DATABASE_URL`, `REDIS_URL` (P20). Один `.env.example` в корне.
- **Логи:** structlog, JSON, контекст `request_id` / `run_id` / `company_id`; никаких `print`.
- **Ошибки:** пакеты бросают доменные исключения (`QuotaExhausted`, `SourceBlocked`, `InvalidCredentials`),
  core переводит их в HTTP.
- **Время** — только UTC (`datetime.now(UTC)`), в API — ISO 8601. **Id** — uuid4.
- **БД:** имена в `snake_case`, единственное число, `_at` для времени, JSONB для гибких полей, сложные выборки — в SQL (P6).
- **Тесты без сети:** respx для HTTP, FakeLLM для Gemini; живые проверки — `@pytest.mark.live`, запускаются вручную.
- **Git:** ветка на папку (`feat/parser-gdelt`, `feat/ai-verify`), мерж в `main` не реже раза в 3 часа, CI зелёный.

### 1.7 Команды

```bash
uv sync --all-packages                                   # установить всё
docker compose up -d postgres redis                      # инфраструктура для разработки
uv run --package leadradar-core alembic -c core/alembic.ini upgrade head
uv run --package leadradar-core uvicorn leadradar_core.main:app --reload
uv run --package leadradar-core taskiq worker leadradar_core.worker.broker:broker --workers 1 --max-async-tasks 3
uv run pytest                                            # все тесты (без сети)
uv run lint-imports && uv run ruff check . && uv run ruff format --check .
```

### 1.8 Критерии готовности (DoD бэкенда)

- [ ] `uv sync --all-packages` и `uv run pytest` проходят на чистой машине; `lint-imports` и `ruff` зелёные в CI.
- [ ] Образ собирается ≤ 5 мин и запускает все три процесса; модель эмбеддингов уже в образе.
- [ ] DoD каждого из трёх SPEC подпапок выполнен.
- [ ] Корневой `CLAUDE.md` есть и короткий.

### 1.9 Какие критерии закрывает группа

| ID | Как |
|---|---|
| U1, S16 | Весь бэкенд на Python: FastAPI, LangGraph, SQLAlchemy, Taskiq |
| U10 | Один образ и одна БД, пакеты-модули с проверяемыми границами |
| U3 | Порты, отсутствие FK между схемами и контракты импорта позволяют вынести пакеты в сервисы |
| K5 | CI, тесты без сети, единые соглашения по ошибкам, логам и времени |

---

## 2. Этап 2 — из монолита в сервисы

| Пакет MVP | Сервис этапа 2 | Шаг перехода |
|---|---|---|
| `leadradar-core` | Gateway/BFF + доменные сервисы | Outbox-диспетчер публикует в шину; модули выносятся по нагрузке |
| `leadradar-ai` | Intelligence (+ Scoring & ML, Research) | Отдельный worker-деплоймент, порты → HTTP или очередь |
| `leadradar-parser` | Ingestion | Обёртка FastAPI + consumer очереди, S3, планировщик |
| `leadradar-auth` | Identity / внешний IdP | OIDC; core проверяет JWT по JWKS |

Инфраструктура этапа 2: образ на сервис, Kubernetes (или Nomad), автоскейл воркеров по длине очереди, NATS JetStream
или Kafka, OpenTelemetry, Grafana, Sentry, секрет-хранилище, IaC. Цели — ARCHITECTURE §5.5. Критерии готовности:
каждый сервис деплоится независимо; контракты — версионируемые схемы событий и OpenAPI; при отказе одного сервиса
остальные деградируют без полного отказа (например, без ingestion лиды остаются доступны).

---

## 3. Рост и развитие: что заложено в MVP и как расширять

| Заложено в MVP | Зачем | Как растёт |
|---|---|---|
| uv workspace из независимых пакетов | Параллельная работа трёх человек | Каждый пакет получает свой образ и деплой |
| import-linter в CI | Границы не размываются под дедлайном | Те же контракты становятся границами сервисов |
| Порты ai + адаптеры core | Нет прямых зависимостей от инфраструктуры | Адаптер меняется на сетевой клиент |
| Отдельные схемы БД без FK между ними | Независимость данных | Схема переезжает с сервисом |
| Один образ, команды api / worker / scheduler | Простой деплой | Раздельные деплойменты, автоскейл |
| Настройки по префиксам пакетов | Изоляция конфигурации | Конфиг на сервис, секреты в vault |

---

## 4. Риски и анти-паттерны

- «Временно импортирую внутренности ai из core» — CI это запрещает. Нужно расширение — добавьте в публичный API пакета.
- Общая «utils»-библиотека для всех пакетов создаёт связность. Небольшое дублирование лучше.
- Миграции из нескольких мест расходятся — Alembic один, в core.
- Разные версии Python в пакетах — в workspace один `requires-python` (≥ 3.12).
