# Mini Design Memo — Peanut Trade Baseline Template

**Author:** Slabliuk
**Date:** 2026-03-28
**Scope:** Engineering prerequisite (Module 0)

---

## Key Invariants

These are properties that must always be true, regardless of input:

**1. Secrets never enter the repository**
- `.env` is blocked by `.gitignore` — Git refuses to track it
- `detect-secrets` scans every file on every `git commit` via pre-commit
- CI repeats the scan on every push — two independent checkpoints

**2. Same input always produces same output**
- `clamp()` and `calculate_position_size()` are pure functions — no randomness, no I/O, no global state
- Verified by explicit determinism tests (`test_deterministic_*`)

**3. Invalid inputs are always rejected loudly**
- Zero or negative equity → `ValueError`
- Risk percentage outside `(0, 1]` → `ValueError`
- Zero or negative stop distance → `ValueError`
- No silent failures, no returning `None`, no swallowed exceptions

**4. Tests must pass before code enters the repo**
- pre-commit runs `pytest` on every `git commit` — broken code cannot be committed
- GitHub Actions runs `pytest` on every push — broken code cannot survive in the remote

---

## Failure Modes

What can go wrong, and how the template handles it:

| Failure | How it's handled |
|---|---|
| Developer hardcodes an API key | `detect-secrets` blocks the commit |
| Developer commits `.env` by mistake | `.gitignore` prevents Git from tracking it |
| A function returns wrong result for edge input | Negative tests catch it before commit |
| Code is pushed with failing tests | GitHub Actions blocks the PR/push |
| Bot starts without credentials | Logs a warning, runs in dry-run mode — does not crash |
| `clamp()` called with `low > high` | Raises `ValueError` immediately — fails loud, not silent |
| Float precision issues in position sizing | `pytest.approx` tolerance used in tests; clamping bounds checked |

---

## What Was Tested and How

### Testing approach
All tests are **unit tests** — isolated, no network calls, no file I/O, no external dependencies. Each function is tested independently.

### Test coverage

**`clamp(value, low, high)`**
- Below range → returns `low` (boundary)
- Above range → returns `high` (boundary)
- Inside range → value unchanged
- Exactly at `low` → inclusive boundary
- Exactly at `high` → inclusive boundary
- Determinism → same call twice = same result
- `low > high` → `ValueError` raised (negative test)
- IEEE 754 float (`0.1 + 0.2`) → result stays within `[0.0, 1.0]` (float precision edge case)

**`calculate_position_size(equity, risk_pct, stop_distance)`**
- Basic calculation → `10_000 * 0.01 / 50 = 2.0` (verified numerically)
- Linear scaling → doubling equity doubles position size
- Determinism → same call twice = same result
- Zero equity → `ValueError` (negative test)
- Negative equity → `ValueError` (negative test)
- Zero risk → `ValueError` (negative test)
- Risk > 1 (>100%) → `ValueError` (negative test)
- Zero stop distance → `ValueError` — would cause division by zero (negative test)
- Negative stop distance → `ValueError` (negative test)

### Why these cases?
Trading functions are unforgiving. A position sizing function that silently accepts `equity=0` or `stop_distance=-5` could produce `inf`, `nan`, or a negative position size — all of which would cause real financial loss in production. The negative tests exist to guarantee the function fails loudly on nonsense input rather than producing nonsense output.
