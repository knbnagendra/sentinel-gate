"""Tests for get_account_state's position-counting logic -- confirmed live
2026-09-01 that counting raw option legs instead of distinct trades let a
handful of multi-leg spreads exhaust max_concurrent_positions and silently
block all new trades for the rest of the session, well before 20 genuine
trade ideas were on the book.
"""

from unittest.mock import MagicMock, patch

from agent.context import get_account_state


def _position(symbol):
    p = MagicMock()
    p.symbol = symbol
    return p


def test_open_positions_count_is_distinct_underlyings_not_raw_legs():
    mock_account = MagicMock(equity="100000", last_equity="100000")
    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account
    # One iron condor (4 legs) + one vertical spread (2 legs) == 2 trades,
    # not 6 raw legs.
    mock_client.get_all_positions.return_value = [
        _position("SPY260918C00450000"),
        _position("SPY260918C00460000"),
        _position("SPY260918P00440000"),
        _position("SPY260918P00430000"),
        _position("AAPL260918C00230000"),
        _position("AAPL260918C00235000"),
    ]

    with patch("agent.context._trading_client", return_value=mock_client):
        state = get_account_state()

    assert state.open_positions_count == 2


def test_open_positions_count_zero_when_flat():
    mock_account = MagicMock(equity="100000", last_equity="100000")
    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account
    mock_client.get_all_positions.return_value = []

    with patch("agent.context._trading_client", return_value=mock_client):
        state = get_account_state()

    assert state.open_positions_count == 0
