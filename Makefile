.PHONY: help install test lint fmt run docker-test docker-lint docker-dev docker-up

help:
	@grep -E '^[a-z-]+:' $(MAKEFILE_LIST) | cut -d: -f1 | sed 's/^/  make /'

install:
	python3 -m venv .venv && .venv/bin/pip install -e '.[test,lint]'

test:
	.venv/bin/python -m pytest --cov --cov-report=term-missing

lint:
	.venv/bin/ruff check . && .venv/bin/black --check . && .venv/bin/mypy

fmt:
	.venv/bin/ruff check --fix . && .venv/bin/black .

run:
	.venv/bin/python -m unifi_occupancy

docker-test:
	docker compose -f docker-compose.test.yml run --rm test

docker-lint:
	docker compose -f docker-compose.lint.yml run --rm lint

docker-dev:
	docker compose -f docker-compose.dev.yml up --build

docker-up:
	docker compose up -d --build
