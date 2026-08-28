"""Tests for the execute.py safety checks that sit between an approved
risk-gate decision and an actual order reaching Alpaca: the legs string
must actually match what the strategy name claims, and covered_call /
cash_secured_put must be backed by a real covering position or cash.
"""

from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError

from agent.execute import (
    OrderLeg,
    _parse_legs,
    _parse_occ_symbol,
    _validate_legs_match_strategy,
    _validate_symbol_matches_legs,
    _verify_coverage,
    close_position,
)

CALL = "SPY260904C00650000"
CALL_HIGHER_STRIKE = "SPY260904C00655000"
PUT = "SPY260904P00600000"


def test_parse_legs_single_buy():
    legs = _parse_legs(f"{CALL}:buy")
    assert legs == [OrderLeg(occ_symbol=CALL, side="buy")]


def test_parse_legs_multi():
    legs = _parse_legs(f"{CALL}:buy;{CALL_HIGHER_STRIKE}:sell")
    assert legs == [
        OrderLeg(occ_symbol=CALL, side="buy"),
        OrderLeg(occ_symbol=CALL_HIGHER_STRIKE, side="sell"),
    ]


def test_parse_legs_rejects_missing_side():
    with pytest.raises(ValueError, match="buy'"):
        _parse_legs(CALL)


def test_parse_legs_rejects_malformed_side():
    with pytest.raises(ValueError, match="buy'"):
        _parse_legs(f"{CALL}:")


def test_parse_legs_rejects_unrecognized_side():
    with pytest.raises(ValueError):
        _parse_legs(f"{CALL}:short")


def test_parse_occ_symbol():
    parsed = _parse_occ_symbol(CALL)
    assert parsed.underlying == "SPY"
    assert parsed.right == "C"
    assert parsed.strike == 650.0


def test_parse_occ_symbol_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_occ_symbol("not-an-occ-symbol")


def test_long_call_requires_single_buy_leg():
    _validate_legs_match_strategy("long_call", [OrderLeg(CALL, "buy")])  # does not raise


def test_long_call_rejects_sell_leg():
    """The core scenario the check exists for: a strategy label that passed
    the risk-gate allowlist paired with a leg spec that's actually a naked
    short -- exactly what the allowlist claims can never happen."""
    with pytest.raises(ValueError, match="long_call"):
        _validate_legs_match_strategy("long_call", [OrderLeg(CALL, "sell")])


def test_covered_call_requires_single_sell_leg():
    _validate_legs_match_strategy("covered_call", [OrderLeg(CALL, "sell")])  # does not raise


def test_covered_call_rejects_buy_leg():
    with pytest.raises(ValueError, match="covered_call"):
        _validate_legs_match_strategy("covered_call", [OrderLeg(CALL, "buy")])


def test_vertical_spread_requires_one_buy_one_sell():
    _validate_legs_match_strategy(
        "vertical_debit_spread", [OrderLeg(CALL, "buy"), OrderLeg(CALL_HIGHER_STRIKE, "sell")]
    )  # does not raise


def test_vertical_spread_rejects_two_buys():
    with pytest.raises(ValueError, match="vertical_debit_spread"):
        _validate_legs_match_strategy(
            "vertical_debit_spread",
            [OrderLeg(CALL, "buy"), OrderLeg(CALL_HIGHER_STRIKE, "buy")],
        )


def test_iron_condor_requires_two_buys_two_sells():
    legs = [
        OrderLeg(CALL, "sell"),
        OrderLeg(CALL_HIGHER_STRIKE, "buy"),
        OrderLeg(PUT, "sell"),
        OrderLeg("SPY260904P00590000", "buy"),
    ]
    _validate_legs_match_strategy("iron_condor", legs)  # does not raise


def test_iron_condor_rejects_wrong_composition():
    legs = [OrderLeg(CALL, "sell"), OrderLeg(CALL_HIGHER_STRIKE, "sell"), OrderLeg(PUT, "buy")]
    with pytest.raises(ValueError, match="iron_condor"):
        _validate_legs_match_strategy("iron_condor", legs)


def test_covered_call_blocked_without_underlying_position():
    client = MagicMock()
    client.get_open_position.side_effect = APIError("position does not exist")

    with pytest.raises(ValueError, match="requires an existing long stock position"):
        _verify_coverage(client, "covered_call", [OrderLeg(CALL, "sell")])


def test_covered_call_blocked_with_insufficient_shares():
    client = MagicMock()
    client.get_open_position.return_value = MagicMock(qty="50")

    with pytest.raises(ValueError, match=">= 100 shares"):
        _verify_coverage(client, "covered_call", [OrderLeg(CALL, "sell")])


def test_covered_call_allowed_with_sufficient_shares():
    client = MagicMock()
    client.get_open_position.return_value = MagicMock(qty="100")

    _verify_coverage(client, "covered_call", [OrderLeg(CALL, "sell")])  # does not raise


def test_cash_secured_put_blocked_without_enough_cash():
    client = MagicMock()
    client.get_account.return_value = MagicMock(cash="1000.00")

    with pytest.raises(ValueError, match="secured cash"):
        _verify_coverage(client, "cash_secured_put", [OrderLeg(PUT, "sell")])


def test_cash_secured_put_allowed_with_enough_cash():
    client = MagicMock()
    client.get_account.return_value = MagicMock(cash="100000.00")

    _verify_coverage(client, "cash_secured_put", [OrderLeg(PUT, "sell")])  # does not raise


def test_symbol_matches_legs_underlying():
    _validate_symbol_matches_legs("SPY", [OrderLeg(CALL, "buy")])  # does not raise


def test_symbol_case_insensitive():
    _validate_symbol_matches_legs("spy", [OrderLeg(CALL, "buy")])  # does not raise


def test_symbol_rejects_mismatched_underlying():
    """The core scenario this check exists for: a proposal that claims one
    symbol but whose legs actually trade a different underlying entirely --
    cooldowns and the decision log must not silently track the wrong
    symbol."""
    with pytest.raises(ValueError, match="does not match leg underlying"):
        _validate_symbol_matches_legs("SPY", [OrderLeg("AAPL260904C00200000", "buy")])


def test_symbol_rejects_mismatch_on_any_leg():
    legs = [OrderLeg(CALL, "buy"), OrderLeg("AAPL260904C00200000", "sell")]
    with pytest.raises(ValueError, match="does not match leg underlying"):
        _validate_symbol_matches_legs("SPY", legs)


def test_close_position_submits_and_returns_summary():
    mock_client = MagicMock()
    mock_client.close_position.return_value = MagicMock(id="order-123", status="accepted")

    with patch("agent.execute._trading_client", return_value=mock_client):
        result = close_position("AAPL")

    mock_client.close_position.assert_called_once_with("AAPL")
    assert "order-123" in result
    assert "accepted" in result


def test_close_position_raises_on_missing_position():
    mock_client = MagicMock()
    mock_client.close_position.side_effect = APIError("position does not exist")

    with patch("agent.execute._trading_client", return_value=mock_client):
        with pytest.raises(ValueError, match="could not close position"):
            close_position("AAPL")
