"""Send a test message to Telegram to verify alerts are working."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from config.settings import Config
from monitoring.telegram import make_alerter
from safety import RiskLimits


async def main() -> None:
    alerter = make_alerter()
    limits = RiskLimits()

    await alerter.send(
        "👋 <b>PeanutTrade — Telegram test</b>\n\n"
        "✅ Alerts are working\n\n"
        "<b>Config:</b>\n"
        f"  Chain: Arbitrum ({Config.ARBITRUM_CHAIN_ID})\n"
        f"  CEX fee: {Config.CEX_TAKER_BPS}bps  DEX fee: {Config.DEX_SWAP_BPS}bps\n"
        f"  Gas: ${Config.GAS_COST_USD}\n\n"
        "<b>Risk limits:</b>\n"
        f"  Max trade: ${limits.max_trade_usd}  Max daily loss: ${limits.max_daily_loss}\n"
        f"  Max drawdown: {limits.max_drawdown_pct:.0%}  Trades/hr: {limits.max_trades_per_hour}\n\n"
        "Kill switch: <code>touch /tmp/arb_bot_kill</code>"
    )
    print("Message sent!")


if __name__ == "__main__":
    asyncio.run(main())
