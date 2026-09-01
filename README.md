# Sentinel Gate

An autonomous Claude-powered options trading agent for the [Alpaca AI Trading
Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
(Aug 28 - Sep 4, 2026).

Claude reasons live each cycle over market data pulled through Alpaca's
official MCP server, then proposes trades through a single custom tool,
`propose_trade`. That tool is the *only* way a trade can happen -- every
proposal runs through code-enforced risk gates (market-hours check,
defined-risk-only allowlist, position sizing cap, per-symbol cooldown, and a
code-only stop-loss/take-profit loop that runs independent of Claude) before
anything reaches Alpaca's Trading API. The gates aren't a suggestion in a
system prompt; they're checked in code, on every call, with no path around
them. There is deliberately no daily loss circuit breaker -- see
[WRITEUP.md](WRITEUP.md) for why.

## Architecture

```
agent/
  risk_gates.py   cooldowns, position cap, undefined-risk block, protective stop-loss/take-profit
  context.py      live account/position snapshot via alpaca-py
  brain.py        Claude reasoning turn: Alpaca MCP tools (read-only) + propose_trade
  execute.py      places the order via alpaca-py, after gates pass
  log_store.py    every cycle's reasoning + gate decisions, ground truth for the dashboard
  loop.py         market-hours scheduler (python -m agent.loop)
dashboard/
  app.py          read-only Streamlit dashboard: positions, P&L, decision feed
tests/
  test_risk_gates.py   safety-critical gate + protective-exit layer, proven before anything trades
  test_execute.py      leg/strategy validation and covered/secured checks
  test_log_store.py    logging + usage-tracking layer
  test_market_scan.py  pre-fetched market data formatting
  (75 tests total across the suite -- `pytest tests/`)
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

## Disclosure

The judging paper account (`PA3YL75LTR3W`) was created 2026-08-26 and began
live trading Friday, Aug 28 at 9:30am ET, once the hackathon window opened.
All code, prompts, and risk-gate logic in this repo were written during the
competition window; no pre-existing trading strategy or model was reused.
