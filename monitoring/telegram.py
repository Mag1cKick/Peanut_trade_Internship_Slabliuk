"""
monitoring/telegram.py — Async Telegram alert sender for the arb bot.

Reads credentials from environment variables:
  TELEGRAM_BOT_TOKEN — token from @BotFather
  TELEGRAM_CHAT_ID   — your personal chat ID (get it by messaging your bot
                        then calling /getUpdates via the Telegram API)

If either variable is absent the module silently no-ops so the bot can run
without Telegram configured (dev / CI environments).

Usage:
    from monitoring.telegram import TelegramAlerter
    alerter = TelegramAlerter.from_env()
    await alerter.send("Bot started")
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerter:
    """Fire-and-forget Telegram message sender."""

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._url = _API_BASE.format(token=token)

    @classmethod
    def from_env(cls) -> TelegramAlerter | None:
        """Return an alerter if env vars are set, else None (silent no-op)."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return None
        return cls(token, chat_id)

    async def send(self, text: str) -> None:
        """Send *text* asynchronously; swallows all errors to never break the bot."""
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._send_sync, text)
        except Exception as exc:
            log.debug("Telegram send failed (non-fatal): %s", exc)

    def _send_sync(self, text: str) -> None:
        payload = urllib.parse.urlencode(
            {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = urllib.request.Request(self._url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass


class _NoOpAlerter:
    """Returned when Telegram is not configured — all calls are silent no-ops."""

    async def send(self, text: str) -> None:
        pass


def make_alerter() -> TelegramAlerter | _NoOpAlerter:
    """Return a configured alerter or a silent no-op if env vars are missing."""
    alerter = TelegramAlerter.from_env()
    return alerter if alerter is not None else _NoOpAlerter()
