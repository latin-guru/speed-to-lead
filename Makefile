PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: install test lint typecheck migrate run worker demo demo-seed scan check

install:
	python3.11 -m venv .venv
	$(PIP) install -r requirements-dev.txt

test:
	$(PY) -m pytest --cov=app --cov-report=term-missing

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy

migrate:
	.venv/bin/alembic upgrade head

run: migrate
	$(PY) -m uvicorn app.main:create_app --factory --reload --port 8000

worker: migrate
	$(PY) -m app.worker

# Live demo: fresh fake data in ./demo.db, worker + lead simulator in the web process.
demo:
	$(PY) -m app.demo

demo-seed:
	$(PY) -m app.demo --seed-only

scan:
	.venv/bin/detect-secrets scan --all-files --exclude-files '^\.venv/' --exclude-files '^\.git/'

check: lint typecheck test
