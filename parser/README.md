# leadradar-parser

Чистая async-библиотека и CLI для сбора и нормализации публичных данных о компаниях. Пакет не зависит от БД,
FastAPI или LLM.

```bash
uv sync --package leadradar-parser
uv run lr-parser adapters
uv run lr-parser resolve --domain dhl.com --name "DHL Group"
uv run lr-parser collect --domain dhl.com --name "DHL Group" --sources news,website,jobs --since 30d
```

Через Docker:

```bash
docker compose build parser
docker compose --profile tools run --rm parser adapters
docker compose --profile tools run --rm parser resolve --domain dhl.com --name "DHL Group"
```

Тесты всегда выполняются без сети:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
