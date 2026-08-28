"""Claude reasoning loop: one non-interactive turn per cycle.

Uses the Anthropic SDK's beta Tool Runner (client.beta.messages.tool_runner),
not the separate Claude Agent SDK product -- the Tool Runner is the documented
way to combine custom tools with local MCP server tools without hand-writing
the agentic loop.

Claude gets read-only market context via the official Alpaca MCP server
(a subprocess, launched with ALPACA_TOOLSETS restricting it to non-trading
categories -- see .env.example) plus a single custom tool, propose_trade,
which is the only way Claude can act. propose_trade runs the deterministic
risk gates in risk_gates.py before anything reaches Alpaca's Trading API.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic, beta_async_tool
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.risk_gates import (
    AccountState,
    RiskConfig,
    TradeProposal,
    cooldown_key,
    evaluate_trade,
    is_market_hours,
    mark_cooldown,
    save_cooldowns,
)
from agent.execute import execute_trade
from agent.execute import close_position as execute_close_position
from agent.log_store import log_cycle

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")

DEFAULT_ALPACA_TOOLSETS = (
    "account,assets,stock-data,options-data,corporate-actions,news,watchlists,index-data"
)

SYSTEM_PROMPT = """You are Sentinel Gate, an autonomous options-trading agent \
competing in a week-long paper-trading hackathon (Alpaca AI Trading Agents \
Hackathon). Your account is a dedicated $100k paper account -- no real money \
is at risk, but the competition is scored on realized P&L, so trade to win, \
not to stay idle.

You reason over live market data (quotes, option chains, news) available \
through your MCP tools. You have two tools that can act, and no other path \
to affecting the account:

- propose_trade -- open a new position. Every proposal is checked against \
  deterministic risk gates (daily circuit breaker, per-symbol cooldown, \
  position sizing cap, market-hours check, and a hard requirement that every \
  strategy has a known, finite max loss -- no naked/undefined-risk short \
  options, ever) before anything reaches Alpaca. A rejection is not a bug to \
  work around -- it means that specific trade isn't allowed right now.
- close_position -- close an existing open position, to take a profit or \
  cut a loss. You are given your current open positions with their entry \
  price, current price, and unrealized P&L every cycle -- review them each \
  time, not just opportunities for new trades. An agent that only ever opens \
  positions and never manages them out is leaving P&L on the table both ways.

Within propose_trade's rails, size up whatever structure -- single-leg \
calls/puts, vertical spreads, iron condors, covered calls, cash-secured puts \
-- best fits each opportunity. Every trade must be an options trade or built \
on one; this is not a stock-picking exercise.

Explain your reasoning briefly before calling a tool. If you see nothing \
worth trading or closing this cycle, say so and don't force it."""


def build_alpaca_mcp_params() -> StdioServerParameters:
    return StdioServerParameters(
        command="uvx",
        args=["alpaca-mcp-server"],
        env={
            "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
            "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
            "ALPACA_PAPER_TRADE": os.environ.get("ALPACA_PAPER_TRADE", "true"),
            "ALPACA_TOOLSETS": os.environ.get("ALPACA_TOOLSETS", DEFAULT_ALPACA_TOOLSETS),
        },
    )


def make_propose_trade_tool(
    account: AccountState,
    config: RiskConfig,
    cooldown_data: dict,
    cooldown_path: Path,
    decisions: list[dict],
):
    """Returns a propose_trade tool closed over this cycle's account snapshot
    and cooldown state. A fresh closure is built every cycle so the account
    snapshot (equity, daily P&L, open positions) can't go stale between
    cycles -- and within a single cycle, `positions_opened` tracks trades
    already executed this turn so a second propose_trade call in the same
    turn is checked against the real running position count, not the count
    from the start of the cycle.

    Every call appends to `decisions` -- the ground-truth record of what the
    risk gates actually did, independent of how Claude narrates it, since
    that's what the dashboard and write-up should be built from."""

    positions_opened = 0

    @beta_async_tool
    async def propose_trade(symbol: str, strategy: str, max_loss: float, legs: str) -> str:
        """Propose and, if approved, execute an options trade.

        This is the only way to place a trade. Every proposal is checked
        against code-enforced risk gates (circuit breaker, cooldown,
        position cap, market hours, defined-risk-only) before anything
        reaches Alpaca. A rejection means try a different trade, not the
        same one again -- cooldowns and caps don't lift on retry.

        Args:
            symbol: Underlying ticker, e.g. "SPY".
            strategy: One of long_call, long_put, vertical_debit_spread,
                vertical_credit_spread, iron_condor, covered_call,
                cash_secured_put. No other strategy is accepted.
            max_loss: Maximum possible dollar loss on this trade if it goes
                to worst case. Must be a real, finite number you calculated
                from the option premium/spread width, not an estimate.
            legs: The exact OCC option symbol(s) for this trade, each with its
                side, e.g. "SPY260904C00650000:buy" for a single-leg trade, or
                "SPY260904C00650000:buy;SPY260904C00655000:sell" for a
                multi-leg spread (semicolon-separated). Look up the precise
                OCC symbol via your option-chain tools first -- do not guess it.
        """
        nonlocal positions_opened
        now = datetime.now(timezone.utc)
        proposal = TradeProposal(symbol=symbol, strategy=strategy, max_loss=max_loss, now=now)
        effective_account = replace(
            account, open_positions_count=account.open_positions_count + positions_opened
        )
        decision = evaluate_trade(proposal, effective_account, config, cooldown_data)

        record = {
            "timestamp": now.isoformat(),
            "symbol": symbol,
            "strategy": strategy,
            "max_loss": max_loss,
            "legs": legs,
            "gate_allowed": decision.allowed,
            "gate_reason": decision.reason,
        }

        if not decision.allowed:
            decisions.append(record)
            return f"REJECTED: {decision.reason}"

        try:
            result = execute_trade(proposal, legs)
        except Exception as exc:
            # Broad on purpose: the gates approved the strategy/max_loss,
            # but anything from here on (malformed legs, an Alpaca API
            # error, a bug) must become a REJECTED record, never an
            # unhandled exception that crashes the whole cycle silently.
            record["gate_allowed"] = False
            record["gate_reason"] = f"execution validation failed: {exc}"
            decisions.append(record)
            return f"REJECTED: {exc}"

        # The order is now live on Alpaca -- record that immediately,
        # before any further bookkeeping, so a failure in cooldown
        # persistence below can never leave an executed trade unlogged.
        # No orphan executions: if it happened, it's in decisions.
        record["execution_result"] = result
        decisions.append(record)

        try:
            mark_cooldown(cooldown_data, cooldown_key(symbol, strategy), now)
            save_cooldowns(cooldown_data, cooldown_path)
            positions_opened += 1
        except Exception as exc:
            print(
                f"WARNING: {symbol}/{strategy} executed but cooldown "
                f"persistence failed: {exc} (trade is still recorded above)"
            )

        return f"EXECUTED: {result}"

    return propose_trade


def make_close_position_tool(decisions: list[dict]):
    """Returns a close_position tool. Closing a position reduces risk (or
    realizes it) rather than adding to it, so it isn't run through
    evaluate_trade's new-entry gates -- it only needs a market-hours check,
    since Alpaca can't fill outside market hours anyway."""

    @beta_async_tool
    async def close_position(symbol: str, reason: str) -> str:
        """Close an existing open position.

        Args:
            symbol: The exact position symbol to close, as it appears in
                your open positions list.
            reason: Brief reason for closing (e.g. "profit target hit",
                "cutting a loser", "near expiration").
        """
        now = datetime.now(timezone.utc)
        record = {
            "timestamp": now.isoformat(),
            "symbol": symbol,
            "strategy": "close_position",
            "max_loss": None,
            "legs": None,
            "gate_allowed": None,
            "gate_reason": reason,
        }

        if not is_market_hours(now):
            record["gate_allowed"] = False
            record["gate_reason"] = "outside market hours (9:30-16:00 ET, Mon-Fri)"
            decisions.append(record)
            return f"REJECTED: {record['gate_reason']}"

        try:
            result = execute_close_position(symbol)
        except Exception as exc:  # broad on purpose -- see propose_trade above
            record["gate_allowed"] = False
            record["gate_reason"] = f"close failed: {exc}"
            decisions.append(record)
            return f"REJECTED: {exc}"

        record["gate_allowed"] = True
        record["execution_result"] = result
        decisions.append(record)
        return f"EXECUTED: {result}"

    return close_position


async def run_cycle(
    account: AccountState,
    watchlist: list[str],
    cooldown_path: Path,
    positions: list[dict] | None = None,
) -> None:
    """Runs exactly one non-interactive Claude turn: gather context via MCP
    tools, reason, optionally call propose_trade and/or close_position, then
    return. No chat loop -- loop.py calls this once per scheduled cycle."""

    from agent.risk_gates import load_cooldowns

    config = RiskConfig.from_env()
    cooldown_data = load_cooldowns(cooldown_path)
    positions = positions or []

    client = AsyncAnthropic()
    server_params = build_alpaca_mcp_params()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()
            tools_result = await mcp_client.list_tools()
            data_tools = [async_mcp_tool(t, mcp_client) for t in tools_result.tools]

            decisions: list[dict] = []
            propose_trade = make_propose_trade_tool(
                account, config, cooldown_data, cooldown_path, decisions
            )
            close_position = make_close_position_tool(decisions)

            if positions:
                positions_lines = "\n".join(
                    f"  {p['symbol']}: {p['side']} qty={p['qty']} "
                    f"entry=${p['avg_entry_price']:,.2f} "
                    f"current=${p['current_price']:,.2f} "
                    f"unrealized_pl=${p['unrealized_pl']:,.2f} "
                    f"({p['unrealized_plpc']:+.2f}%)"
                    for p in positions
                )
            else:
                positions_lines = "  (none)"

            user_prompt = (
                f"Watchlist: {', '.join(watchlist)}\n"
                f"Account equity: ${account.equity:,.2f}\n"
                f"Today's P&L: {account.daily_pnl_pct:+.2f}%\n"
                f"Open positions ({account.open_positions_count}):\n{positions_lines}\n\n"
                "First review your open positions above and decide whether any "
                "should be closed (profit target, cutting a loss, near "
                "expiration). Then review current market conditions for the "
                "watchlist and decide whether any new options trade is worth "
                "proposing this cycle."
            )

            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                tools=[*data_tools, propose_trade, close_position],
                messages=[{"role": "user", "content": user_prompt}],
            )

            transcript = []
            async for message in runner:
                transcript.append(message)

            log_cycle(account, transcript, decisions)
