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


# SPDR sector ETFs that appear in the default watchlist -- used to build a
# sector-momentum leaderboard from the same snapshot fetch as the per-symbol
# quotes, no extra API call. Real estate (XLRE) is deliberately not in the
# default watchlist, so it's omitted here too.
SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLC": "Communication Services",
}


def _pct_change(snap) -> float | None:
    if snap is None or snap.daily_bar is None:
        return None
    prev_close = snap.previous_daily_bar.close if snap.previous_daily_bar else None
    if not prev_close:
        return None
    return (snap.daily_bar.close - prev_close) / prev_close * 100


def _format_snapshots(watchlist: list[str], snapshots) -> str:
    lines = []
    for symbol in watchlist:
        snap = snapshots.get(symbol)
        if snap is None or snap.daily_bar is None:
            lines.append(f"  {symbol}: no data")
            continue
        pct = _pct_change(snap)
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        lines.append(f"  {symbol}: ${snap.daily_bar.close:.2f} ({pct_str})")
    return "\n".join(lines)


def _format_sector_leaderboard(snapshots) -> str:
    """Ranks the SPDR sector ETFs by today's % change, strongest to weakest,
    so Claude can spot sector-wide moves directly instead of having to infer
    them from individual tickers scattered through the flat watchlist list.
    Best-effort: a sector ETF missing from the watchlist/snapshot is just
    skipped, not an error -- this is a convenience ranking, not a gate."""
    ranked = []
    for symbol, name in SECTOR_ETFS.items():
        pct = _pct_change(snapshots.get(symbol))
        if pct is not None:
            ranked.append((pct, symbol, name))
    if not ranked:
        return "  (no sector ETF data)"
    ranked.sort(reverse=True)
    return "\n".join(f"  {i}. {sym} ({name}): {pct:+.2f}%" for i, (pct, sym, name) in enumerate(ranked, 1))


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
        snapshots = _data_client().get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=watchlist)
        )
        snapshots_text = _format_snapshots(watchlist, snapshots)
        sector_text = _format_sector_leaderboard(snapshots)
    except Exception as exc:
        snapshots_text = f"  (snapshot fetch failed: {exc})"
        sector_text = f"  (sector leaderboard unavailable: {exc})"

    try:
        news_text = _format_news(watchlist)
    except Exception as exc:
        news_text = f"  (news fetch failed: {exc})"

    try:
        movers_text = _format_movers()
    except Exception as exc:
        movers_text = f"  (movers fetch failed: {exc})"

    return (
        "Sector leaderboard (SPDR sector ETFs, ranked by today's % change, "
        "strongest to weakest):\n"
        f"{sector_text}\n\n"
        "Watchlist snapshot (price, % change from prior close):\n"
        f"{snapshots_text}\n\n"
        "Recent news (last 24h, watchlist symbols):\n"
        f"{news_text}\n\n"
        "Market movers (broad market, top gainers/losers):\n"
        f"{movers_text}"
    )
