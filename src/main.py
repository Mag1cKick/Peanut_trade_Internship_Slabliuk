"""
Entry point for the trading bot template.
Replace this with your actual strategy logic.
"""

import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def get_config() -> dict:
    """Load config from environment variables (never from hardcoded values)."""
    return {
        "api_key": os.environ.get("API_KEY"),  # populated via .env / Vault
        "api_secret": os.environ.get("API_SECRET"),
        "environment": os.environ.get("ENVIRONMENT", "development"),
    }


def run() -> None:
    config = get_config()
    log.info("Starting bot in '%s' environment", config["environment"])

    if not config["api_key"]:
        log.warning("API_KEY is not set — running in dry-run / simulation mode")

    log.info("Bot initialised successfully. Replace this stub with real logic.")


if __name__ == "__main__":
    run()
