# Sentinel Gate -- Alpaca AI Trading Agents Hackathon

*One-page write-up. The strategy, reasoning flow, and risk-gate design below
are final. The "Results" section and the fired-gate examples are filled in
from real `state/cycles.jsonl` history near the Sep 4 deadline.*

## AI logic

Each cycle (`agent/loop.py`, on a market-hours schedule) is one non-interactive
Claude turn -- no chat history carries over, so every decision is made fresh
against the current account state. Claude sees:

- **Account snapshot** (`agent/context.py`): equity, today's realized +
  unrealized P&L as a %, open position count -- pulled live via `alpaca-py`,
  not cached.
- **Watchlist**: a fixed symbol list from `WATCHLIST` in `.env`.
- **Live market data**, on demand, through the official Alpaca MCP server:
  quotes, option chains, corporate actions, news. Claude decides what to look
  up; nothing is force-fed.

From that, Claude picks *one* of seven allowed structures per idea, each with
a known, finite max loss (`agent/risk_gates.py::ALLOWED_STRATEGIES`):

| Strategy | Shape | Why Claude would reach for it |
|---|---|---|
| `long_call` / `long_put` | Single leg, buy only | Simple directional conviction; max loss = premium paid |
| `vertical_debit_spread` | Buy + sell same-side, different strikes | Directional view, but cheaper entry and capped upside in exchange for capped risk |
| `vertical_credit_spread` | Sell + buy same-side, different strikes | Collects premium on a view that price *won't* reach the short strike |
| `iron_condor` | Two credit spreads, both sides | Range-bound view -- profits if price stays between the short strikes |
| `covered_call` | Long 100 shares + short call | Yield on an existing/new equity position, caps upside |
| `cash_secured_put` | Short put, cash-backed | Get paid to set a limit-buy below market, or just collect premium |

Every one of these has a contractually bounded worst case -- that's the
allowlist's entire point (fail-closed: an unrecognized strategy name is
rejected outright, not evaluated on its claimed `max_loss`). Naked short
calls/puts and anything else with theoretically unlimited loss are simply not
in the set, so they can never be proposed, let alone approved.

Claude's only path to acting on any of this is the single `propose_trade`
tool (`agent/brain.py`) -- it must name the strategy, the exact OCC leg
symbols, and a calculated `max_loss` before the gates even look at it. A
rejection is designed to read as "try something else," not "retry the same
thing" -- cooldowns and caps don't lift because Claude asks twice.

*TODO near deadline: 2-3 real examples from the decision log of strategy
choice reacting to changing conditions over the week.*

## Risk gates

All gates live in `agent/risk_gates.py::evaluate_trade`, run in a fixed order,
first failure wins, and are checked in code on every single proposal -- not
suggested in the system prompt, not something Claude can reason its way
around. 16/16 unit tests (`tests/test_risk_gates.py`) cover this layer before
anything else in the system was built.

- **Market-hours check** -- rejects anything outside 9:30-16:00 ET, Mon-Fri.
  Options liquidity and pricing go strange outside regular hours; simplest
  fix is to just not trade then.
- **Defined-risk allowlist** -- strategy must be one of the seven named
  above. This is the hard block on undefined-risk/naked options: fail closed
  on an allowlist, not fail open on a denylist of "known bad" strategies.
- **`max_loss` sanity check** -- must be a real, positive, finite number
  Claude calculated from the actual option premium/spread width. No trade
  gets through on an estimate or a missing figure.
- **Daily loss circuit breaker** (`DAILY_LOSS_LIMIT_PCT`, default 5%) -- once
  today's P&L hits the floor, no new entries until next session. Stops a bad
  day from compounding into a worse one from an agent that doesn't feel
  loss aversion.
- **Max concurrent positions** (`MAX_CONCURRENT_POSITIONS`, default 6) --
  caps how many open bets exist at once, independent of how good any single
  idea looks in isolation.
- **Position sizing cap** (`MAX_POSITION_PCT`, default 10% of equity) --
  bounds any single trade's `max_loss` relative to account size, so one wrong
  idea can't be sized up into a large fraction of the account.
- **Per-symbol/strategy cooldown** (`COOLDOWN_MINUTES`, default 30) -- once a
  `(symbol, strategy)` pair trades, it's locked out for the window. Stops the
  agent from re-entering the same idea cycle after cycle chasing a move.

*TODO near deadline: a real example of each gate actually firing, pulled from
`state/cycles.jsonl`.*

## Alpaca infrastructure

Two separate paths into Alpaca, deliberately never merged:

- **Official Alpaca MCP server** (subprocess, launched via `uvx
  alpaca-mcp-server` in `agent/brain.py::build_alpaca_mcp_params`) gives
  Claude read-only market/account context. `ALPACA_TOOLSETS` in `.env`
  explicitly excludes the `trading` category -- Claude's MCP tools can look
  up quotes, option chains, positions, and news, but the category that
  bundles order placement is never exposed to it at all.
- **Direct `alpaca-py` Trading API calls** (`agent/execute.py`) are the *only*
  way an order reaches Alpaca, and that code path only runs after
  `evaluate_trade` returns allowed. Claude never has a tool that calls this
  directly.

So the split isn't just "read tools vs write tools" -- it's that the write
path doesn't exist as an LLM-callable tool at all. `propose_trade` is a
custom tool that runs the gates and only *then*, in plain Python, calls
`execute_trade`. There's no MCP tool, prompt instruction, or code path that
lets Claude place an order except by going through that gate.

## Results

TODO: final P&L, notable trades, what worked, what didn't.
