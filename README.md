## Quick start

# 1. Install all dependencies
make install

# 2. Configure secrets
cp .env.example .env
# open .env and fill in your values

# 3. Run
make run

# 4. Test
make test
```

## Project structure

```
.
├── src/
│   ├── main.py        # Entry point — reads env, starts bot
│   └── utils.py       # Pure utility functions (position sizing, clamp)
├── tests/
│   └── test_utils.py  # Unit tests (happy path + edge cases)
├── scripts/
│   └── check_secrets.sh
├── configs/
│   └── settings.yaml  # Non-sensitive runtime config
├── docs/
├── .env.example        # Secret template — copy to .env, never commit .env
├── .gitignore
├── .pre-commit-config.yaml
├── .secrets.baseline
├── Makefile
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

## Output examples

### `make run`
```
2026-03-28 15:00:00 [INFO] Starting bot in 'development' environment
2026-03-28 15:00:00 [WARNING] API_KEY is not set — running in dry-run / simulation mode
2026-03-28 15:00:00 [INFO] Bot initialised successfully. Replace this stub with real logic.
```

### `make test`
```
tests/test_utils.py::TestClamp::test_value_below_low_returns_low PASSED
tests/test_utils.py::TestClamp::test_value_above_high_returns_high PASSED
tests/test_utils.py::TestClamp::test_value_within_range_unchanged PASSED
tests/test_utils.py::TestClamp::test_boundary_low_inclusive PASSED
tests/test_utils.py::TestClamp::test_boundary_high_inclusive PASSED
tests/test_utils.py::TestClamp::test_deterministic_same_input_same_output PASSED
tests/test_utils.py::TestClamp::test_invalid_range_raises PASSED
tests/test_utils.py::TestClamp::test_float_precision_stable PASSED
tests/test_utils.py::TestPositionSize::test_basic_calculation PASSED
tests/test_utils.py::TestPositionSize::test_result_scales_with_equity PASSED
tests/test_utils.py::TestPositionSize::test_deterministic PASSED
tests/test_utils.py::TestPositionSize::test_zero_equity_raises PASSED
tests/test_utils.py::TestPositionSize::test_negative_equity_raises PASSED
tests/test_utils.py::TestPositionSize::test_zero_risk_pct_raises PASSED
tests/test_utils.py::TestPositionSize::test_risk_pct_above_one_raises PASSED
tests/test_utils.py::TestPositionSize::test_zero_stop_distance_raises PASSED
tests/test_utils.py::TestPositionSize::test_negative_stop_distance_raises PASSED

17 passed in 0.12s
```

## Available commands

| Command | What it does |
|---|---|
| `make install` | Install all dependencies |
| `make run` | Run the bot |
| `make test` | Run the full test suite |
| `make lint` | Lint with ruff |
| `make format` | Auto-format with ruff |
| `make pre-commit-install` | Install git hooks |
| `make clean` | Remove cache files |

## CI

GitHub Actions runs on every push — secret scan → lint → pytest on Python 3.11 and 3.12.

## Limitations & assumptions

- This is a **template / stub** — `src/main.py` contains no real trading logic yet
- Bot runs in **dry-run mode** by default when `API_KEY` is not set
- `make clean` uses Python internally for Windows compatibility (no Unix `find`/`rm` required)
- Tested on Windows 11 (PowerShell) and Ubuntu via GitHub Actions
- Python 3.11+ required
