.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help setup up down ps logs build api worker migrate seed test lint ts-types

help:
	@echo "LeadRadar backend — всё в docker compose, на хост ничего ставить не нужно:"
	@echo "  make setup    собрать образ, поднять всё, накатить миграции, засеять демо-данные"
	@echo "  make up       поднять postgres, redis, api, worker (в фоне, с пересборкой образа)"
	@echo "  make down     остановить всё (данные в volumes остаются)"
	@echo "  make ps       статус контейнеров"
	@echo "  make logs     логи api + worker"
	@echo "  make build    только пересобрать образ"
	@echo "  make api      api в переднем плане с логами (после правок кода: Ctrl+C и снова make api)"
	@echo "  make worker   worker в переднем плане с логами"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make seed     демо-данные (lr seed)"
	@echo "  make test     pytest (без сети; нужен postgres/redis — поднимутся сами)"
	@echo "  make lint     import-linter + ruff"
	@echo "  make ts-types TS-типы из openapi.json -> clients/ts/index.d.ts"

setup: up migrate seed
	@echo
	@echo "Готово. API: http://127.0.0.1:8000/docs  Логи: make logs"

up:
	$(COMPOSE) up -d --build postgres redis api worker

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f api worker

build:
	$(COMPOSE) build api

api:
	$(COMPOSE) up --build api

worker:
	$(COMPOSE) up --build worker

migrate:
	$(COMPOSE) run --rm --build migrate

seed:
	$(COMPOSE) run --rm --build seed

test:
	$(COMPOSE) run --rm --build test

lint:
	$(COMPOSE) run --rm --build lint

ts-types:
	docker run --rm -u $$(id -u):$$(id -g) -e npm_config_cache=/tmp/.npm -v $(CURDIR):/w -w /w/clients/ts node:22-alpine \
		npx --yes openapi-typescript@7.13.0 ../../openapi.json -o index.d.ts
