.PHONY: install run test lint format pre-commit-install clean

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

run:
	python -m src.main

test:
	python -m pytest tests/ -v --tb=short

lint:
	python -m ruff check src/ tests/ core/ chain/

format:
	python -m ruff format src/ tests/ core/ chain/

pre-commit-install:
	python -m pre_commit install

clean:
	python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache', '.ruff_cache']]; [p.unlink() for p in pathlib.Path('.').rglob('*.pyc')]"
	@echo "cleaned"
