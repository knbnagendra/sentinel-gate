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

Real examples from the decision log of strategy choice reacting to changing
conditions (`state/cycles.jsonl`):

- **Anticipatory de-risking**: opened a `PANW` iron condor on 8/28, then on
  8/31 closed it early -- "de-risking iron condor ahead of PANW earnings
  (Sept 1) with expiration Sept 4 ... to cap risk before binary catalyst" --
  and reopened a fresh iron condor the same day once the position was flat.
  Not a stop-loss or take-profit trigger; a judgment call about upcoming
  binary event risk that neither code gate covers.
- **Thesis invalidation, not just P&L**: an `MSTR` bull put credit spread was
  closed on 8/28 with reasoning "thesis invalidated: MSTR down ~8% on
  dilutive $2B capital [raise]" -- exited on the *reason* the position
  existed breaking, independent of whether the stop-loss threshold had been
  hit yet.
- **Catalyst-driven profit-take below the code threshold**: an `XOM` long
  call was closed on 9/1 at "+49.5% gain on oil-geopolitical spike (Strait of
  Hormuz headlines); locking in profit" -- well under the +100%
  code-enforced take-profit level, because Claude judged the move was a
  news spike unlikely to hold, not a sustained trend.

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
- **Max concurrent positions** (`MAX_CONCURRENT_POSITIONS`, default 20) --
  caps how many open bets exist at once, independent of how good any single
  idea looks in isolation. Deliberately generous: this is a paper account
  with no real capital at risk, and the goal is maximizing trade volume/P&L
  over the week, not conserving positions for their own sake.
- **Position sizing cap** (`MAX_POSITION_PCT`, default 10% of equity) --
  bounds any single trade's `max_loss` relative to account size, so one wrong
  idea can't be sized up into a large fraction of the account.
- **Per-symbol/strategy cooldown** (`COOLDOWN_MINUTES`, default 30) -- once a
  `(symbol, strategy)` pair trades, it's locked out for the window. Stops the
  agent from re-entering the same idea cycle after cycle chasing a move.
- **Code-enforced stop-loss/take-profit** (`STOP_LOSS_PCT`/`TAKE_PROFIT_PCT`,
  plus a partial take-profit tier) -- runs on its own fast (~15s) loop,
  decoupled from the 15-min reasoning cycle, since Alpaca doesn't support
  OCO/bracket orders for options. This is what actually manages positions
  out between reasoning turns, independent of Claude noticing.

**Deliberately not a gate: a daily loss circuit breaker.** Early versions had
one; it's removed. This is a paper account with no real capital at risk, and
the competition scores trade volume/P&L over the week -- a gate that halts
all new entries after a bad stretch protects capital that isn't actually at
risk, at the direct cost of missing the rest of the week's opportunities. The
structural safety gates above (defined-risk-only, leg/symbol validation,
per-trade size cap) stay in place regardless; this is specifically about not
stopping the agent from continuing to trade, not about loosening what counts
as a safe trade.

Real examples from `state/cycles.jsonl` of gates actually firing:

- **Max concurrent positions** blocked new entries twice on 8/28 (`MSTR`,
  `QQQ`) when the account hit the then-current cap of 6/6 open positions --
  proposals that otherwise passed every other gate were still rejected.
- **Code-enforced stop-loss** closed a `PANW` iron condor on 8/28 when "loss
  already ~2.2x credit collected amid elevated post-earnings IV" breached the
  -50% threshold, independent of the reasoning cycle noticing.
- The **defined-risk allowlist**, **max_loss sanity check**, **position
  sizing cap**, **market-hours check**, and **per-symbol cooldown** did not
  reject any live proposal over the week -- every trade Claude proposed
  already stayed inside those bounds. Their correctness is proven by the
  75-test unit suite (`tests/test_risk_gates.py`) instead, which exercises
  each one directly (oversized `max_loss`, out-of-hours timestamps, an
  unrecognized strategy name, a symbol/strategy pair inside its cooldown
  window, etc.) rather than waiting for a live proposal to happen to violate
  one.

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

*Final -- EOD Thursday Sep 3, 2026, the competition's official scoring snapshot.*

Starting equity: $100,000 (account created Aug 26, trading began Aug 28 -- see
Disclosure in [README.md](README.md)). Final equity: **$92,824.51**, down
**-$7,175.49 (-7.18%)** cumulative. Day-by-day: Aug 29 -0.54%, Sep 1 -1.75%,
Sep 2 -2.00%, Sep 3 -1.87%. 71 trades opened across the week, spanning
verticals, iron condors, single-leg calls/puts, and covered structures.

**What worked:**
- **Catalyst-aware exits below the code threshold** -- an `XOM` long call
  closed +49.5% on a geopolitical spike (Strait of Hormuz headlines), and an
  `NVDA` long put closed +56.67% ahead of an upcoming catalyst Claude judged
  "now a coin-flip" -- both taken well under the +100% code-enforced
  take-profit, on Claude's own read that the move likely wouldn't hold or the
  edge had disappeared.
- **Thesis-invalidation discipline** -- `MSTR` and `SHOP` credit spreads were
  cut fast when the fundamental reason for the trade broke (a dilutive
  capital raise, a broken bullish thesis on a down day), not held waiting for
  the -50% code stop-loss to eventually catch up.

**What didn't:**
- **PANW, repeatedly** -- the single largest loss driver of the week.
  Bullish-tilted PANW put credit spreads were opened and re-opened through
  Sep 1 while PANW was actively falling ~6% on bad news amid a broader
  risk-off day; each got cut at a loss as the stock kept moving the wrong
  way. This was the direct motivation for the sector-leaderboard prompt
  update shipped that evening (see AI logic) -- sector-wide context might
  have flagged the tape disagreeing with the single-name thesis before entry.

**Infrastructure reliability:** two live production incidents were caught and
fixed same-day with no extended downtime -- an upstream `fastmcp` 4.0.0
breaking release (Sep 1 morning) and a multi-leg naked-position/retry-storm
risk in the protective stop-loss loop (Sep 1 midday). A separate
position-counting bug (raw option legs counted instead of distinct trades,
silently capping real trade volume at a fraction of the intended limit) was
found and fixed Sep 1 evening -- all three are detailed in the project's
internal history and reflected in the current `agent/risk_gates.py` and
`agent/context.py`.
