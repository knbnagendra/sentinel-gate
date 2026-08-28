# Sentinel Gate

An autonomous Claude-powered options trading agent for the [Alpaca AI Trading
Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
(Aug 28 - Sep 4, 2026).

Claude reasons live each cycle over market data pulled through Alpaca's
official MCP server, then proposes trades through a single custom tool,
`propose_trade`. That tool is the *only* way a trade can happen -- every
proposal runs through code-enforced risk gates (daily circuit breaker,
per-symbol cooldown, position sizing cap, market-hours check, and a hard
block on undefined-risk strategies) before anything reaches Alpaca's Trading
API. The gates aren't a suggestion in a system prompt; they're checked in
code, on every call, with no path around them.

## Architecture

```
agent/
  risk_gates.py   circuit breaker, cooldowns, position cap, undefined-risk block
  context.py      live account/position snapshot via alpaca-py
  brain.py        Claude reasoning turn: Alpaca MCP tools (read-only) + propose_trade
  execute.py      places the order via alpaca-py, after gates pass
  log_store.py    every cycle's reasoning + gate decisions, ground truth for the dashboard
  loop.py         market-hours scheduler (python -m agent.loop)
dashboard/
  app.py          read-only Streamlit dashboard: positions, P&L, decision feed
tests/
  test_risk_gates.py   16 tests -- the safety-critical layer, proven before anything trades
  test_execute.py      17 tests -- leg/strategy validation and covered/secured checks
```

## Setup

```
python -m venv .venv
.venv/Scripts/activate  # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env    # fill in ALPACA_API_KEY/SECRET (dedicated paper account) + ANTHROPIC_API_KEY
```

## Run

```
pytest tests/                                       # verify the risk gates first
python -m agent.loop                                # the trading loop
streamlit run dashboard/app.py --server.port 8501   # the dashboard, separately
```
