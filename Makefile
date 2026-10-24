PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: install test lint typecheck run scan check

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

run:
	$(PY) -m uvicorn app.main:app --reload --port 8000

scan:
	.venv/bin/detect-secrets scan --all-files --exclude-files '^\.venv/' --exclude-files '^\.git/'

check: lint typecheck test
