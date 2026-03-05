# Makefile — EventFlux dev shortcuts

.PHONY: up down build logs shell-api shell-worker seed \
        load-test-scenario1 load-test-scenario2 migrate psql

# ── Docker ───────────────────────────────────────────────────────────────────
up:
	docker compose up --build -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

logs-api:
	docker compose logs -f api

logs-worker:
	docker compose logs -f worker

# ── Shells ───────────────────────────────────────────────────────────────────
shell-api:
	docker compose exec api bash

shell-worker:
	docker compose exec worker bash

# ── Database ─────────────────────────────────────────────────────────────────
psql:
	docker compose exec postgres psql -U eventflux -d eventflux

migrate:
	docker compose exec postgres psql -U eventflux -d eventflux \
	  -f /docker-entrypoint-initdb.d/01_init.sql

# ── Seeding ──────────────────────────────────────────────────────────────────
seed:
	python scripts/seed.py

# ── Load tests ───────────────────────────────────────────────────────────────
load-test-scenario1:
	k6 run \
	  -e API_BASE_URL=http://localhost:8000 \
	  -e EVENTS_PER_REQUEST=100 \
	  -e VUS=100 \
	  load_tests/scenarios/bulk_ingest.js

load-test-scenario2:
	k6 run \
	  -e API_BASE_URL=http://localhost:8000 \
	  -e EVENTS_PER_REQUEST=1000 \
	  -e VUS=50 \
	  load_tests/scenarios/bulk_ingest.js
