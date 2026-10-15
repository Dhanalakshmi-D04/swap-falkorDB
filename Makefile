PY := .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff
PORT ?= 8000
SEEDS ?= 5

export PYTHONPATH := src

.PHONY: up test verify run lint

up:  ## start FalkorDB on 127.0.0.1:16400 (idempotent)
	@docker inspect swap-falkordb >/dev/null 2>&1 || \
		docker run -d --name swap-falkordb -p 127.0.0.1:16400:6379 falkordb/falkordb:latest
	@docker start swap-falkordb >/dev/null
	@echo "FalkorDB: 127.0.0.1:16400 (container swap-falkordb)"

test:
	$(PYTEST) -q

verify:
	$(PY) -m swap.verify --seeds $(SEEDS)

run:
	$(PY) -m uvicorn swap.api:app --port $(PORT)

lint:
	$(RUFF) check .
