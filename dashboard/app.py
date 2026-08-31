"""Read-only dashboard: current positions/P&L pulled live from Alpaca, plus
the decision feed from log_store. No write endpoints -- this is observation
only, for the demo period and for judges.

Streamlit escapes all text passed to st.write/st.markdown (without
unsafe_allow_html) by default, so Claude's freeform reasoning text -- which
can be influenced by untrusted news/market data read via MCP -- can't inject
markup into the page. The only unsafe_allow_html use below is a fixed,
hardcoded CSS block with no data interpolated into it.

Run with: streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run dashboard/app.py` puts this file's own directory on
# sys.path, not the repo root -- add the root explicitly so the sibling
# `agent` package is importable regardless of the cwd this is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import altair as alt
import pandas as pd
import streamlit as st

from agent.context import get_account_state, get_daily_pnl_history, get_open_positions
from agent.log_store import read_cycles

st.set_page_config(page_title="Sentinel Gate", page_icon=":chart_with_upwards_trend:", layout="wide")

st.markdown(
    """
    <style>
      div[data-testid="stMetric"] {
        background: #1C2128;
        border: 1px solid #2A303C;
        border-radius: 12px;
        padding: 1rem 1.25rem;
      }
      div[data-testid="stMetricValue"] { font-size: 1.9rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title(":chart_with_upwards_trend: Sentinel Gate")
st.caption("Autonomous options trading agent -- Alpaca AI Trading Agents Hackathon")

if st.button("Refresh"):
    st.rerun()

account = get_account_state()
col1, col2, col3 = st.columns(3)
col1.metric(":moneybag: Equity", f"${account.equity:,.2f}")
col2.metric(
    ":calendar: Today's P&L",
    f"{account.daily_pnl_pct:+.2f}%",
    delta=f"{account.daily_pnl_pct:+.2f}%",
    delta_color="normal",
)
col3.metric(":bar_chart: Open Positions", account.open_positions_count)

st.header(":pushpin: Open Positions")

positions = get_open_positions()
if not positions:
    st.write("No open positions.")
else:
    df_positions = pd.DataFrame(positions).rename(
        columns={
            "symbol": "Symbol",
            "side": "Side",
            "qty": "Qty",
            "avg_entry_price": "Entry Price",
            "current_price": "Current Price",
            "market_value": "Market Value",
            "unrealized_pl": "Unrealized P&L ($)",
            "unrealized_plpc": "Unrealized P&L (%)",
        }
    )

    def _color_pnl(val: float) -> str:
        if val is None:
            return ""
        return f"color: {'#00D9A3' if val >= 0 else '#FF4B4B'}"

    styled = df_positions.style.map(_color_pnl, subset=["Unrealized P&L ($)", "Unrealized P&L (%)"]).format(
        {
            "Entry Price": "${:,.2f}",
            "Current Price": "${:,.2f}",
            "Market Value": "${:,.2f}",
            "Unrealized P&L ($)": "${:,.2f}",
            "Unrealized P&L (%)": "{:+.2f}%",
        },
        na_rep="N/A",
    )
    st.dataframe(styled, width="stretch", hide_index=True)

st.header(":dollar: Day-wise P&L")

pnl_history = get_daily_pnl_history()
if pnl_history:
    df = pd.DataFrame(pnl_history)
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("date:N", title="Date"),
            y=alt.Y("pnl_pct:Q", title="P&L %"),
            color=alt.condition(
                alt.datum.pnl_pct >= 0,
                alt.value("#00D9A3"),
                alt.value("#FF4B4B"),
            ),
            tooltip=[
                alt.Tooltip("date:N", title="Date"),
                alt.Tooltip("equity:Q", title="Equity", format=",.2f"),
                alt.Tooltip("pnl_pct:Q", title="P&L %", format="+.2f"),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, width="stretch")
else:
    st.write("No portfolio history yet.")

st.header(":clipboard: Decision feed")

cycles = read_cycles(limit=30)
if not cycles:
    st.write("No cycles logged yet.")

for cycle in cycles:
    is_failure = cycle["reasoning"].startswith("CYCLE FAILED:")
    with st.container(border=True):
        st.subheader(cycle["timestamp"])
        if is_failure:
            # log_cycle_failure stores the full traceback in state/cycles.jsonl
            # (still there for SSH/journalctl-style debugging), but the
            # dashboard only ever shows a one-line summary -- a judge-facing
            # page doesn't need it and a raw multi-hundred-line trace reads
            # as broken even when the system actually handled it correctly.
            # A Python traceback always ends with "ExceptionType: message"
            # as its last line, which is all that's shown here.
            lines = [line for line in cycle["reasoning"].splitlines() if line.strip()]
            summary = lines[-1] if lines else cycle["reasoning"]
            st.error(f"Cycle failed: {summary}")
        elif cycle["reasoning"]:
            st.write(cycle["reasoning"])

        usage = cycle.get("usage")
        if usage and usage.get("input_tokens"):
            cached = usage.get("cache_read_input_tokens", 0)
            fresh = usage.get("input_tokens", 0)
            total_in = cached + fresh
            hit_pct = (cached / total_in * 100) if total_in else 0
            st.caption(
                f"tokens: in={fresh:,} cache_read={cached:,} "
                f"cache_write={usage.get('cache_creation_input_tokens', 0):,} "
                f"out={usage.get('output_tokens', 0):,} "
                f"({hit_pct:.0f}% cache hit)"
            )

        decisions = cycle["decisions"]
        if not decisions:
            if not is_failure:
                st.caption("no trade proposed")
        else:
            for d in decisions:
                badge_col, detail_col = st.columns([1, 5])
                with badge_col:
                    if d["gate_allowed"]:
                        st.badge("EXECUTED", color="green", icon="✅")
                    else:
                        st.badge("REJECTED", color="red", icon="⛔")
                with detail_col:
                    st.write(f"**{d['symbol']} -- {d['strategy']}**: {d['gate_reason']}")
