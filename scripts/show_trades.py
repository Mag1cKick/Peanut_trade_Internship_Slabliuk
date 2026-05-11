"""Show all trades from the DB. Usage: python scripts/show_trades.py [date]"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime

from db.trades import daily_summary, print_ledger

date = sys.argv[1] if len(sys.argv) > 1 else None
label = date or "all time"
print(f"\nTrade Ledger — {label}\n")
print_ledger(date)

today = date or datetime.now().strftime("%Y-%m-%d")
s = daily_summary(today)
if s and s.get("n"):
    wr = s["wins"] / s["n"] * 100 if s["n"] else 0
    print(
        f"\nDaily summary ({today}): "
        f"{s['n']} trades  win={wr:.0f}%  "
        f"net=${s['total_net']:+.4f}  "
        f"avg_spread={s['avg_spread']:.1f}bps"
    )
