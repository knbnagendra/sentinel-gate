"""Market-hours scheduler: runs Claude reasoning cycles on a slower cadence
and a fast, code-only stop-loss/take-profit sweep on a much faster cadence,
concurrently. Alpaca doesn't support OCO/bracket orders for options, so the
fast sweep is the real protection between reasoning cycles -- especially
since 0DTE is allowed by default (see execute.py's MIN_DTE_DAYS), which can
move fast enough that the reasoning cycle's own cadence alone isn't safe.

Run with: python -m agent.loop
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent.brain import run_cycle
from agent.context import get_account_state, get_open_positions
from agent.execute import close_position
from agent.log_store import log_auto_close, log_cycle_failure
from agent.risk_gates import (
    AccountState,
    RiskConfig,
    find_partial_take_profits,
    find_protective_exits,
    is_market_hours,
)

COOLDOWN_PATH = Path(__file__).resolve().parent.parent / "state" / "cooldowns.json"
PARTIAL_TP_STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "partial_tp_triggered.json"


def _load_partial_tp_triggered(path: Path = PARTIAL_TP_STATE_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_partial_tp_triggered(triggered: set[str], path: Path = PARTIAL_TP_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(triggered)))


def _watchlist() -> list[str]:
    raw = os.environ.get(
        "WATCHLIST",
        "SPY,QQQ,IWM,DIA,XLF,XLE,XLK,XLV,XLY,XLI,XLP,XLU,XLB,XLC,TQQQ,SQQQ,SOXL,UVXY,VXX,"
        "AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,NFLX,ORCL,CRM,ADBE,AMD,INTC,QCOM,MU,AVGO,ARM,SMH,"
        "JPM,BAC,V,MA,GS,MS,WFC,C,RIVN,LCID,F,GM,LLY,UNH,JNJ,ABBV,MRNA,XOM,CVX,COP,WMT,COST,HD,"
        "NKE,MCD,SBUX,BA,CAT,GE,DIS,COIN,PLTR,MSTR,SOFI,GME,RBLX,UBER,PYPL,SHOP,SMCI,PANW",
    )
    return [s.strip() for s in raw.split(",") if s.strip()]


def _close_protective_exits_sync(account: AccountState, positions: list[dict]) -> None:
    """Synchronous Alpaca calls -- always run this via asyncio.to_thread,
    never awaited directly, so it doesn't block the other concurrent loop.

    Full stop-loss/take-profit exits are checked first (unconditional,
    every poll); partial take-profits are checked second and only for
    symbols that haven't already had that tier fire, using persisted state
    so the same tier doesn't re-trigger every poll against the remaining
    runner shares after a partial close."""
    config = RiskConfig.from_env()
    triggered = _load_partial_tp_triggered()
    open_symbols = {p["symbol"] for p in positions}
    decisions = []

    full_exits = find_protective_exits(positions, config)
    for finding in full_exits:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": finding.symbol,
            "strategy": "protective_exit",
            "max_loss": None,
            "legs": None,
            "gate_allowed": None,
            "gate_reason": finding.reason,
        }
        try:
            result = close_position(finding.symbol)
            record["gate_allowed"] = True
            record["execution_result"] = result
            print(f"[protective exit] {finding.symbol}: {finding.reason} -> {result}")
        except Exception as exc:  # broad on purpose -- every attempt must be logged, no exceptions
            record["gate_allowed"] = False
            record["gate_reason"] = f"{finding.reason}, but close failed: {exc}"
            print(f"[protective exit] {finding.symbol}: close FAILED -- {exc}")
        decisions.append(record)

    partial_exits = find_partial_take_profits(positions, config, triggered)
    for finding in partial_exits:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": finding.symbol,
            "strategy": "partial_take_profit",
            "max_loss": None,
            "legs": None,
            "gate_allowed": None,
            "gate_reason": finding.reason,
        }
        try:
            result = close_position(finding.symbol, percentage=finding.close_percentage)
            record["gate_allowed"] = True
            record["execution_result"] = result
            triggered.add(finding.symbol)
            print(f"[partial take-profit] {finding.symbol}: {finding.reason} -> {result}")
        except Exception as exc:  # broad on purpose -- every attempt must be logged, no exceptions
            record["gate_allowed"] = False
            record["gate_reason"] = f"{finding.reason}, but close failed: {exc}"
            print(f"[partial take-profit] {finding.symbol}: close FAILED -- {exc}")
        decisions.append(record)

    # Prune symbols no longer open (fully closed by any path since) so a
    # future position on the same symbol isn't wrongly treated as already
    # having had its partial tier fire.
    triggered &= open_symbols
    _save_partial_tp_triggered(triggered)

    if decisions:
        log_auto_close(account, decisions)


async def protective_loop(check_seconds: int) -> None:
    """Fast, code-only stop-loss/take-profit sweep -- no Claude, no
    Anthropic cost, pure Alpaca REST calls. Runs independent of the slower
    reasoning cycle so a position isn't left unprotected for the full
    reasoning-cycle interval."""
    while True:
        now = datetime.now(timezone.utc)
        if is_market_hours(now):
            account = None
            try:
                account = await asyncio.to_thread(get_account_state)
                positions = await asyncio.to_thread(get_open_positions)
                await asyncio.to_thread(_close_protective_exits_sync, account, positions)
            except Exception as exc:  # keep this loop alive across a bad check
                tb = traceback.format_exc()
                print(f"[{now.isoformat()}] protective check failed: {exc}")
                print(tb)
                await asyncio.to_thread(
                    log_cycle_failure, account, f"protective check failed: {exc}\n{tb}"
                )
        await asyncio.sleep(check_seconds)


async def reasoning_loop(cycle_minutes: int, watchlist: list[str]) -> None:
    """The slower Claude reasoning cycle -- unchanged in spirit from before,
    just running concurrently with protective_loop instead of alone."""
    while True:
        now = datetime.now(timezone.utc)
        if is_market_hours(now):
            print(f"[{now.isoformat()}] cycle starting")
            account = None
            try:
                account = await asyncio.to_thread(get_account_state)
                positions = await asyncio.to_thread(get_open_positions)
                await run_cycle(account, watchlist, COOLDOWN_PATH, positions)
                print(f"[{now.isoformat()}] cycle complete")
            except Exception as exc:  # keep the loop alive across a bad cycle
                tb = traceback.format_exc()
                print(f"[{now.isoformat()}] cycle failed: {exc}")
                print(tb)
                await asyncio.to_thread(log_cycle_failure, account, f"cycle failed: {exc}\n{tb}")
        else:
            print(f"[{now.isoformat()}] outside market hours, skipping")
        await asyncio.sleep(cycle_minutes * 60)


async def main() -> None:
    cycle_minutes = int(os.environ.get("CYCLE_MINUTES", "15"))
    check_seconds = int(os.environ.get("PROTECTIVE_CHECK_SECONDS", "60"))
    watchlist = _watchlist()

    await asyncio.gather(
        reasoning_loop(cycle_minutes, watchlist),
        protective_loop(check_seconds),
    )


if __name__ == "__main__":
    asyncio.run(main())
