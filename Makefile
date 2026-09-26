.DEFAULT_GOAL := help
COMPOSE := docker compose
UV_CORE := uv run --package leadradar-core

.PHONY: help setup sync up down logs migrate seed api worker test lint

help:
	@echo "LeadRadar backend — из корня репо:"
	@echo "  make setup    uv sync + postgres/redis + миграции + seed"
	@echo "  make up       только postgres и redis"
	@echo "  make down     остановить postgres/redis"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make seed     демо-данные (lr seed)"
	@echo "  make api      FastAPI с --reload  (терминал 1)"
	@echo "  make worker   Taskiq worker       (терминал 2)"
	@echo "  make test     pytest без сети"
	@echo "  make lint     import-linter + ruff"

setup: sync up migrate seed
	@echo
	@echo "Инфра готова. Дальше в двух терминалах:"
	@echo "  make api"
	@echo "  make worker"

sync:
	uv sync --all-packages

up:
	$(COMPOSE) up -d postgres redis

down:
	$(COMPOSE) stop postgres redis

logs:
	$(COMPOSE) logs -f postgres redis

migrate: up
	$(UV_CORE) alembic -c core/alembic.ini upgrade head

seed:
	uv run lr seed

api:
	$(UV_CORE) uvicorn leadradar_core.main:app --reload --host 127.0.0.1 --port 8000

worker:
	$(UV_CORE) taskiq worker leadradar_core.worker.broker:broker --workers 1 --max-async-tasks 3

test:
	uv run pytest

lint:
	uv run lint-imports && uv run ruff check . && uv run ruff format --check .
