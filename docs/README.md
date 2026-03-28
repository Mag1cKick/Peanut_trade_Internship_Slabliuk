# trading-bot-template

> Engineering baseline for the trading internship — Module 0 prerequisite.

## Quick start

```bash
# 1. Clone & enter
git clone <your-repo-url>
cd trading-bot-template

# 2. Install dependencies
make install

# 3. Configure secrets (never commit .env)
cp .env.example .env
# → edit .env and fill in your values

# 4. Run
make run

# 5. Test
make test
```

## Project structure

```
.
├── src/            # Application source code
│   ├── main.py     # Entry point  (make run)
│   └── utils.py    # Pure utility functions
├── tests/          # Pytest test suite  (make test)
│   └── test_utils.py
├── scripts/        # Helper shell scripts
│   └── check_secrets.sh
├── configs/        # Non-sensitive YAML config
│   └── settings.yaml
├── docs/           # Documentation
├── .env.example    # Secret template — copy to .env, never commit .env
├── .gitignore      # Blocks .env and other sensitive files
├── .pre-commit-config.yaml
├── pyproject.toml  # Ruff + pytest config
├── requirements.txt
├── requirements-dev.txt
└── Makefile
```

## Secrets management

| Do ✅ | Don't ❌ |
|---|---|
| Store secrets in `.env` | Hardcode secrets in source files |
| Use `os.environ.get(...)` | Commit `.env` to Git |
| Rotate keys if exposed | Share keys in Slack/Discord |

`.env` is listed in `.gitignore`. CI uses GitHub Actions secrets.

## Available make commands

| Command | What it does |
|---|---|
| `make install` | Install all dependencies |
| `make run` | Run the bot |
| `make test` | Run the full test suite |
| `make lint` | Lint with ruff |
| `make format` | Auto-format with ruff |
| `make pre-commit-install` | Install pre-commit hooks |
| `make clean` | Remove cache files |

## CI

GitHub Actions runs on every push:
1. Scans for hardcoded secrets
2. Lints with `ruff`
3. Runs `pytest` on Python 3.11 and 3.12

See `.github/workflows/ci.yml`.

## Adding pre-commit hooks

```bash
make pre-commit-install
# From now on, every git commit will run lint + secret checks automatically
```
