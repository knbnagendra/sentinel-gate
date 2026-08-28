"""Market-hours scheduler: runs one brain.run_cycle() per configured
interval while the market is open, skips cycles outside market hours.

Run with: python -m agent.loop
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent.brain import run_cycle
from agent.context import get_account_state, get_open_positions
from agent.risk_gates import is_market_hours

COOLDOWN_PATH = Path(__file__).resolve().parent.parent / "state" / "cooldowns.json"


def _watchlist() -> list[str]:
    raw = os.environ.get("WATCHLIST", "SPY,QQQ,AAPL,NVDA,MSFT,TSLA,AMZN,META")
    return [s.strip() for s in raw.split(",") if s.strip()]


async def main() -> None:
    cycle_minutes = int(os.environ.get("CYCLE_MINUTES", "15"))
    watchlist = _watchlist()

    while True:
        now = datetime.now(timezone.utc)
        if is_market_hours(now):
            print(f"[{now.isoformat()}] cycle starting")
            try:
                account = get_account_state()
                positions = get_open_positions()
                await run_cycle(account, watchlist, COOLDOWN_PATH, positions)
                print(f"[{now.isoformat()}] cycle complete")
            except Exception as exc:  # keep the loop alive across a bad cycle
                print(f"[{now.isoformat()}] cycle failed: {exc}")
        else:
            print(f"[{now.isoformat()}] outside market hours, skipping")

        await asyncio.sleep(cycle_minutes * 60)


if __name__ == "__main__":
    asyncio.run(main())
