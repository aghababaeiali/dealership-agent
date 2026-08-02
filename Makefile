.PHONY: install lint typecheck test up down data-download data-generate evals

install:
	uv sync --group data

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest

up:
	docker compose up -d

down:
	docker compose down

data-download:
	uv run --group data python data/scripts/download_listings.py

data-generate:
	@echo "not implemented"

evals:
	@echo "not implemented"
