"""Load .env before live tests so BINANCE_API_KEY / BINANCE_API_SECRET are available."""

from __future__ import annotations

from pathlib import Path


def pytest_configure(config):
    root = Path(__file__).parent.parent.parent
    env_file = root / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        except ImportError:
            pass
