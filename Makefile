.PHONY: help dev down logs migrate test test-unit lint fmt hooks

UV ?= uv
# Host ports, overridable when something else already listens on them.
export POSTGRES_HOST_PORT ?= 5432
export LEDGER_HOST_PORT ?= 8000
export SIMULATOR_HOST_PORT ?= 8100

help:
	@echo "make dev       start Postgres, the Ledger API and the Simulator; wait until healthy"
	@echo "make down      stop the stack and delete its data"
	@echo "make logs      follow the stack's logs"
	@echo "make test      run every test (integration tests need Docker)"
	@echo "make test-unit run the unit tests only"
	@echo "make lint      ruff, ruff format --check, mypy --strict"
	@echo "make fmt       apply ruff fixes and formatting"
	@echo "make hooks     install the pre-commit hooks (gitleaks, ruff)"

dev:
	docker compose up --build --detach --wait ledger simulator
	@curl -fsS http://localhost:$(LEDGER_HOST_PORT)/health && echo
	@curl -fsS http://localhost:$(SIMULATOR_HOST_PORT)/health && echo

down:
	docker compose down -v

logs:
	docker compose logs -f

migrate:
	$(UV) run alembic upgrade head

test:
	$(UV) run pytest

test-unit:
	$(UV) run pytest -m "not integration"

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

fmt:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

hooks:
	$(UV) run pre-commit install
