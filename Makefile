.PHONY: install test run migrate seed docker-up docker-down lint

install:
	pip install -r requirements.txt

test:
	python -m pytest tests/ -v --tb=short

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	alembic upgrade head

seed:
	python scripts/seed.py

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

lint:
	python -m py_compile app/models.py
	python -m py_compile app/scoring.py
	python -m py_compile app/state_machine.py
	python -m py_compile app/pipeline.py
	python -m py_compile app/main.py
