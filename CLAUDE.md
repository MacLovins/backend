# LeadRadar Backend

## Команды
- `uv sync --all-packages` — установка зависимостей workspace
- `docker compose up -d postgres redis` — запуск инфраструктуры
- `uv run --package leadradar-core alembic -c backend/backend/alembic.ini upgrade head` — миграции
- `uv run --package leadradar-core uvicorn leadradar_core.main:app --reload` — запуск API
- `uv run pytest` — запуск тестов без сети (сетевые тесты под `@pytest.mark.live`)
- `uv run lint-imports && uv run ruff check . && uv run ruff format --check .` — линтинг

## Архитектура и границы
- Архитектура: ARCHITECTURE.md §4, ТЗ модулей в SPEC.md.
- Запрещены циклические импорты: parser, ai, auth независимы.
- Core импортирует только публичные API (`__init__.py`) библиотек.
