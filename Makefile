.PHONY: install run test lint format pre-commit-install clean

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

run:
	python -m src.main

test:
	python -m pytest tests/ -v --tb=short

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

pre-commit-install:
	python -m pre_commit install

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
	find . -type f -name "*.pyc" -delete 2>/dev/null; \
	rm -rf .pytest_cache .ruff_cache; \
	echo "cleaned"
