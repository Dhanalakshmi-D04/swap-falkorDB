PY := .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff
PORT ?= 8000
APP_PORT ?= 80
SEEDS ?= 5
FAIRNESS_SEEDS ?= 10

export PYTHONPATH := src

.PHONY: up test verify verify-fairness graph-bench run lint docker-up docker-down

up:  ## start FalkorDB on 127.0.0.1:16400 (idempotent)
	@docker inspect swap-falkordb >/dev/null 2>&1 || \
		docker run -d --name swap-falkordb -p 127.0.0.1:16400:6379 falkordb/falkordb:latest
	@docker start swap-falkordb >/dev/null
	@echo "FalkorDB: 127.0.0.1:16400 (container swap-falkordb)"

test:
	$(PYTEST) -q

verify:
	$(PY) -m swap.verify --seeds $(SEEDS)

verify-fairness:  ## priority weighting (weight 1 vs 3) on highly sensitised patients
	$(PY) -m swap.verify --fairness --seeds $(FAIRNESS_SEEDS)

graph-bench:  ## time the graph work behind the headline loop (N=300, seed 1000, median of 5)
	$(PY) -m swap.bench

run:
	$(PY) -m uvicorn swap.api:app --port $(PORT)

lint:
	$(RUFF) check .

docker-up:  ## build and start the app + FalkorDB via docker compose (host port $(APP_PORT))
	APP_PORT=$(APP_PORT) docker compose up -d --build
	@echo "swap app: http://127.0.0.1:$(APP_PORT)"

docker-down:  ## stop the compose stack and drop its volumes
	docker compose down -v
