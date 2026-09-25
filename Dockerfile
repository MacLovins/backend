FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PARSER_CACHE_DIR=/var/cache/leadradar/parser

RUN groupadd --system leadradar \
    && useradd --system --gid leadradar --create-home leadradar \
    && mkdir -p /var/cache/leadradar/parser \
    && chown -R leadradar:leadradar /var/cache/leadradar

RUN pip install --no-cache-dir uv==0.10.2

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY parser/pyproject.toml parser/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace

COPY parser parser
RUN uv sync --frozen --no-dev --package leadradar-parser

USER leadradar

ENTRYPOINT ["lr-parser"]
CMD ["--help"]
