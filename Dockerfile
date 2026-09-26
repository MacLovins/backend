FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system leadradar \
    && useradd --system --gid leadradar --create-home leadradar

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /usr/local/bin/

WORKDIR /app

# 1) только манифесты -> слой с зависимостями кешируется, пока не меняется uv.lock
COPY pyproject.toml uv.lock ./
COPY parser/pyproject.toml parser/pyproject.toml
COPY ai/pyproject.toml ai/pyproject.toml
COPY auth/pyproject.toml auth/pyproject.toml
COPY core/pyproject.toml core/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace

# 2) исходники всех пакетов workspace (без tests — они только в стадии dev)
COPY parser/src parser/src
COPY ai/src ai/src
COPY auth/src auth/src
COPY core/src core/src
COPY core/migrations core/migrations
COPY core/alembic.ini core/alembic.ini
RUN uv sync --frozen --no-dev --all-packages

# --- parser: CLI-инструмент (docker compose run --rm parser ...) ---
FROM base AS parser

ENV PARSER_CACHE_DIR=/var/cache/leadradar/parser
RUN mkdir -p /var/cache/leadradar/parser && chown -R leadradar:leadradar /var/cache/leadradar

USER leadradar
ENTRYPOINT ["lr-parser"]
CMD ["--help"]

# --- app: api / worker / migrate / seed (команда задаётся в docker-compose.yml) ---
FROM base AS app

COPY seeds seeds
RUN mkdir -p /app/.cache && chown leadradar:leadradar /app/.cache

USER leadradar
EXPOSE 8000

# --- dev: app + dev-зависимости + tests (docker compose run --rm test / lint) ---
FROM app AS dev

USER root
COPY parser/tests parser/tests
COPY ai/tests ai/tests
COPY auth/tests auth/tests
COPY core/tests core/tests
RUN uv sync --frozen --all-packages \
    && mkdir -p /app/.pytest_cache /app/.ruff_cache \
    && chown leadradar:leadradar /app/.pytest_cache /app/.ruff_cache
USER leadradar
