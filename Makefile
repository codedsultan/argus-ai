SHELL := /bin/bash
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
COMPOSE_FILE ?= platform/compose.core.yaml
COMPOSE ?= docker compose -f $(COMPOSE_FILE)

.PHONY: help install run-shop run-agent chat core-up core-up-d core-down core-logs core-test core-v1-test public-build public-run test lint clean

help:
	@echo "install    install local dependencies in .venv"
	@echo "run-shop   run the fake shop API on :8080"
	@echo "run-agent  run the agent on :8010"
	@echo "chat       chat with a running agent (keeps history client-side)"
	@echo "core-up    start the core stack in the foreground"
	@echo "core-up-d  start the core stack in the background"
	@echo "core-down  stop the core stack and remove volumes"
	@echo "core-logs  follow core stack logs"
	@echo "core-test  run the full test suite"
	@echo "core-v1-test run the public core suite"
	@echo "public-build build the single-container public backend"
	@echo "public-run run it on http://localhost:8088"
	@echo "lint       run ruff"

install:
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements-dev.txt

run-shop:
	PYTHONPATH=. $(PYTHON) -m uvicorn domain.shop_api.app:app --host 0.0.0.0 --port 8080 --reload

run-agent:
	PYTHONPATH=. $(PYTHON) -m services.agent.main

chat:
	PYTHONPATH=. $(PYTHON) scripts/chat.py

core-up:
	$(COMPOSE) up --build

core-up-d:
	$(COMPOSE) up -d --build

core-down:
	$(COMPOSE) down -v

core-logs:
	$(COMPOSE) logs -f

core-test:
	$(PYTHON) -m pytest evals -q

# The public core keeps replay, history, determinism, the smoke test,
# telemetry, and the public proxy/mode guards. It excludes MCP, chaos, the
# trace-eval suite, and the CI matrix.
core-v1-test:
	$(PYTHON) -m pytest \
		evals/test_replay.py \
		evals/test_history.py \
		evals/test_determinism.py \
		evals/test_smoke.py \
		evals/test_telemetry.py \
		evals/test_public_proxy.py \
		evals/test_public_mode.py \
		-q

public-build:
	docker build -f deploy/public/Dockerfile -t argus-core .

public-run:
	docker run --rm -p 8088:8080 argus-core

test: core-test

lint:
	$(PYTHON) -m ruff check domain services evals scripts deploy

clean:
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
