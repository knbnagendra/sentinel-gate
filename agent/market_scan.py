"""Prefetches broad, cheap market data (quotes, news, movers) via alpaca-py
directly -- zero Anthropic tokens -- instead of Claude spending several MCP
tool-call round-trips per cycle on the broad scanning phase (checking movers,
checking individual quotes, pulling news) before it narrows down to a few
names worth a closer look. This only replaces that broad first pass: Claude
still uses its MCP tools for the narrow, expensive follow-up (option chains)
once it's picked candidates, since prefetching full chains for the entire
watchlist would be more data than Claude fetches today, not less.

Every field/shape here was verified against a live call, not assumed: the
News API returns parsed `News` objects (attribute access), but Market Movers
returns plain dicts (subscript access) -- an inconsistency in alpaca-py's own
response handling, not a typo here.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import MarketMoversRequest, NewsRequest, StockSnapshotRequest


def _data_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"], secret_key=os.environ["ALPACA_SECRET_KEY"]
    )


def _news_client() -> NewsClient:
    return NewsClient(
        api_key=os.environ["ALPACA_API_KEY"], secret_key=os.environ["ALPACA_SECRET_KEY"]
    )


def _screener_client() -> ScreenerClient:
    return ScreenerClient(
        api_key=os.environ["ALPACA_API_KEY"], secret_key=os.environ["ALPACA_SECRET_KEY"]
    )


def _format_snapshots(watchlist: list[str]) -> str:
    snapshots = _data_client().get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=watchlist)
    )
    lines = []
    for symbol in watchlist:
        snap = snapshots.get(symbol)
        if snap is None or snap.daily_bar is None:
            lines.append(f"  {symbol}: no data")
            continue
        close = snap.daily_bar.close
        prev_close = snap.previous_daily_bar.close if snap.previous_daily_bar else None
        pct_str = f"{(close - prev_close) / prev_close * 100:+.2f}%" if prev_close else "N/A"
        lines.append(f"  {symbol}: ${close:.2f} ({pct_str})")
    return "\n".join(lines)


def _format_news(watchlist: list[str], limit: int = 40, lookback_hours: int = 24) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    news = _news_client().get_news(
        NewsRequest(
            symbols=",".join(watchlist),
            start=since,
            limit=limit,
            exclude_contentless=True,
        )
    )
    # NewsClient.get_news returns a raw dict {'news': [News, ...]} rather
    # than the parsed NewsSet the type hint suggests -- confirmed live.
    items = news.data.get("news", []) if isinstance(news.data, dict) else news.data
    if not items:
        return "  (no recent news)"
    lines = [f"  [{','.join(item.symbols)}] {item.headline}" for item in items]
    return "\n".join(lines)


def _format_movers(top: int = 10) -> str:
    movers = _screener_client().get_market_movers(
        MarketMoversRequest(top=top, market_type="stocks")
    )
    gainers = ", ".join(f"{m['symbol']} {m['percent_change']:+.2f}%" for m in movers.gainers)
    losers = ", ".join(f"{m['symbol']} {m['percent_change']:+.2f}%" for m in movers.losers)
    return f"  Gainers: {gainers}\n  Losers: {losers}"


def build_market_scan(watchlist: list[str]) -> str:
    """One consolidated text block for the volatile part of the prompt,
    replacing what would otherwise be several MCP tool-call round-trips
    per cycle for the broad scanning phase. Each section fails
    independently (a broken news call shouldn't take down snapshots too)
    since this is best-effort context, not a gated risk check."""
    try:
        snapshots_text = _format_snapshots(watchlist)
    except Exception as exc:
        snapshots_text = f"  (snapshot fetch failed: {exc})"

    try:
        news_text = _format_news(watchlist)
    except Exception as exc:
        news_text = f"  (news fetch failed: {exc})"

    try:
        movers_text = _format_movers()
    except Exception as exc:
        movers_text = f"  (movers fetch failed: {exc})"

    return (
        "Watchlist snapshot (price, % change from prior close):\n"
        f"{snapshots_text}\n\n"
        "Recent news (last 24h, watchlist symbols):\n"
        f"{news_text}\n\n"
        "Market movers (broad market, top gainers/losers):\n"
        f"{movers_text}"
    )
