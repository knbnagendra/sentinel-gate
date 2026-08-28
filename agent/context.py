"""Authoritative account/positions snapshot, pulled directly via alpaca-py --
the same client execute.py uses -- rather than through the MCP server, so
Claude's context always reflects the real account state with no extra hop.
Live market data (quotes, option chains, news) comes separately, through the
Alpaca MCP server tools Claude calls itself during its reasoning turn (see
brain.py) -- this module only covers account/position state.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

from agent.risk_gates import AccountState


def _trading_client() -> TradingClient:
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=os.environ.get("ALPACA_PAPER_TRADE", "true").lower() == "true",
    )


def get_account_state() -> AccountState:
    client = _trading_client()
    account = client.get_account()
    positions = client.get_all_positions()

    equity = float(account.equity)
    last_equity = float(account.last_equity)
    daily_pnl_pct = ((equity - last_equity) / last_equity) * 100 if last_equity else 0.0

    return AccountState(
        equity=equity,
        daily_pnl_pct=daily_pnl_pct,
        open_positions_count=len(positions),
    )


def get_open_positions() -> list[dict]:
    """Per-position detail for brain.py's context and the dashboard's Open
    Positions table -- AccountState only carries a bare count, which isn't
    enough for Claude (or a judge) to tell a winner from a loser."""
    client = _trading_client()
    positions = client.get_all_positions()
    return [
        {
            "symbol": p.symbol,
            "side": p.side.value if hasattr(p.side, "value") else str(p.side),
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price) if p.current_price is not None else None,
            "market_value": float(p.market_value) if p.market_value is not None else None,
            "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl is not None else None,
            "unrealized_plpc": (
                float(p.unrealized_plpc) * 100 if p.unrealized_plpc is not None else None
            ),
        }
        for p in positions
    ]


def get_daily_pnl_history(period: str = "2W") -> list[dict]:
    """Day-by-day equity/P&L for the dashboard's daily P&L chart. Days
    before the account existed (or with no activity yet) come back as
    zeros from Alpaca -- not an error, just nothing to report yet."""
    client = _trading_client()
    history = client.get_portfolio_history(
        GetPortfolioHistoryRequest(period=period, timeframe="1D")
    )
    return [
        {
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
            "equity": equity,
            "pnl_pct": (pct or 0.0) * 100,
        }
        for ts, equity, pct in zip(history.timestamp, history.equity, history.profit_loss_pct)
    ]
