"""Tests for the prefetched market scan -- verifies the formatting logic
against the exact response shapes observed from live Alpaca calls (a raw
dict for news, plain dicts for movers, pydantic objects for snapshots/news
items -- an inconsistency in alpaca-py's own response handling that was
confirmed live, not assumed, before writing this module).
"""

from unittest.mock import MagicMock, patch

from agent.market_scan import build_market_scan


def _snapshot(close, prev_close):
    snap = MagicMock()
    snap.daily_bar.close = close
    snap.previous_daily_bar.close = prev_close
    return snap


def test_build_market_scan_happy_path():
    mock_data_client = MagicMock()
    mock_data_client.get_stock_snapshot.return_value = {
        "SPY": _snapshot(close=450.0, prev_close=445.0),
    }

    news_item = MagicMock()
    news_item.symbols = ["SPY"]
    news_item.headline = "Something happened"
    mock_news_client = MagicMock()
    mock_news_client.get_news.return_value = MagicMock(data={"news": [news_item]})

    mock_screener_client = MagicMock()
    mock_screener_client.get_market_movers.return_value = MagicMock(
        gainers=[{"symbol": "AAA", "percent_change": 5.0}],
        losers=[{"symbol": "BBB", "percent_change": -3.0}],
    )

    with (
        patch("agent.market_scan._data_client", return_value=mock_data_client),
        patch("agent.market_scan._news_client", return_value=mock_news_client),
        patch("agent.market_scan._screener_client", return_value=mock_screener_client),
    ):
        scan = build_market_scan(["SPY"])

    assert "SPY: $450.00 (+1.12%)" in scan
    assert "[SPY] Something happened" in scan
    assert "AAA +5.00%" in scan
    assert "BBB -3.00%" in scan


def test_build_market_scan_handles_missing_snapshot():
    mock_data_client = MagicMock()
    mock_data_client.get_stock_snapshot.return_value = {}  # SPY not returned

    with (
        patch("agent.market_scan._data_client", return_value=mock_data_client),
        patch("agent.market_scan._news_client", return_value=MagicMock()),
        patch("agent.market_scan._screener_client", return_value=MagicMock()),
    ):
        scan = build_market_scan(["SPY"])

    assert "SPY: no data" in scan


def test_build_market_scan_handles_no_news():
    mock_news_client = MagicMock()
    mock_news_client.get_news.return_value = MagicMock(data={"news": []})

    with (
        patch("agent.market_scan._data_client", return_value=MagicMock()),
        patch("agent.market_scan._news_client", return_value=mock_news_client),
        patch("agent.market_scan._screener_client", return_value=MagicMock()),
    ):
        scan = build_market_scan(["SPY"])

    assert "(no recent news)" in scan


def test_build_market_scan_sector_leaderboard_ranks_strongest_first():
    mock_data_client = MagicMock()
    mock_data_client.get_stock_snapshot.return_value = {
        "XLF": _snapshot(close=50.0, prev_close=49.0),  # +2.04%
        "XLE": _snapshot(close=80.0, prev_close=82.0),  # -2.44%
        "XLK": _snapshot(close=200.0, prev_close=196.0),  # +2.04% (tie, alphabetical after XLF by symbol)
    }

    with (
        patch("agent.market_scan._data_client", return_value=mock_data_client),
        patch("agent.market_scan._news_client", return_value=MagicMock()),
        patch("agent.market_scan._screener_client", return_value=MagicMock()),
    ):
        scan = build_market_scan(["XLF", "XLE", "XLK"])

    leaderboard = scan.split("Watchlist snapshot")[0]
    xle_pos = leaderboard.index("XLE")
    xlf_pos = leaderboard.index("XLF")
    assert xle_pos > xlf_pos  # XLE (-2.44%) ranked after the positive movers


def test_build_market_scan_sector_leaderboard_skips_missing_etfs():
    mock_data_client = MagicMock()
    mock_data_client.get_stock_snapshot.return_value = {
        "SPY": _snapshot(close=450.0, prev_close=445.0),
    }

    with (
        patch("agent.market_scan._data_client", return_value=mock_data_client),
        patch("agent.market_scan._news_client", return_value=MagicMock()),
        patch("agent.market_scan._screener_client", return_value=MagicMock()),
    ):
        scan = build_market_scan(["SPY"])

    assert "(no sector ETF data)" in scan


def test_build_market_scan_section_failure_is_isolated():
    """One section failing (e.g. news API hiccup) must not take down the
    others -- this is best-effort context, not a gated risk check."""
    mock_data_client = MagicMock()
    mock_data_client.get_stock_snapshot.return_value = {
        "SPY": _snapshot(close=450.0, prev_close=445.0),
    }
    mock_news_client = MagicMock()
    mock_news_client.get_news.side_effect = RuntimeError("news API down")

    with (
        patch("agent.market_scan._data_client", return_value=mock_data_client),
        patch("agent.market_scan._news_client", return_value=mock_news_client),
        patch("agent.market_scan._screener_client", return_value=MagicMock()),
    ):
        scan = build_market_scan(["SPY"])

    assert "SPY: $450.00" in scan  # snapshots still worked
    assert "news fetch failed" in scan  # news failure surfaced, not silent
