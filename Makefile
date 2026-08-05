.PHONY: help install db up down init-db test lint fmt serve ingest seed clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package plus dev extras and the Chromium build
	pip install -e ".[dev]"   # add ".[dev,gmail]" for stage-5 Gmail support
	python -m playwright install chromium

db:  ## Start only Postgres+pgvector
	docker compose up -d db

up:  ## Start Postgres and the API
	docker compose up -d --build db api

down:  ## Stop everything
	docker compose down

init-db:  ## Apply migrations against AUTOAPPLY_DATABASE_URL
	autoapply init-db

seed:  ## Load the example profile and one board per supported ATS
	autoapply load-profile docs/profile.example.json --email you@example.com
	autoapply companies import docs/companies.example.json

test:  ## Run the test suite (no network, no API key needed)
	pytest -q

lint:  ## Static checks
	ruff check src tests

fmt:  ## Autofix
	ruff check --fix src tests
	ruff format src tests

serve:  ## Run the API + review UI locally
	autoapply serve --reload

ingest:  ## Pull every tracked board
	autoapply ingest

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
