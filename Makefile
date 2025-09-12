.PHONY: fmt lint test migrate seed

fmt:
black app ops_ext.py org_ext.py

lint:
ruff app ops_ext.py org_ext.py

migrate:
@echo "run your migrations here"

seed:
@echo "seed data"

test:
pytest --cov=app --cov-report=term-missing
