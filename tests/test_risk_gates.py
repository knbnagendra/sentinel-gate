"""Regression tests for the risk gates. These aren't just sanity checks --
each one is here because it's the exact failure mode the gate exists to
prevent. Confirm each test fails if you comment out its corresponding
check in risk_gates.evaluate_trade before trusting this file.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.risk_gates import (
    AccountState,
    RiskConfig,
    TradeProposal,
    evaluate_trade,
    is_market_hours,
    is_in_cooldown,
    mark_cooldown,
    cooldown_key,
)

ET = ZoneInfo("America/New_York")


def market_hours_now() -> datetime:
    # A Tuesday, 11am ET -- safely inside market hours.
    return datetime(2026, 9, 1, 11, 0, tzinfo=ET)


def base_config() -> RiskConfig:
    return RiskConfig(
        daily_loss_limit_pct=5,
        max_position_pct=10,
        max_concurrent_positions=6,
        cooldown_minutes=30,
    )


def base_account(**overrides) -> AccountState:
    defaults = dict(equity=100_000, daily_pnl_pct=0.0, open_positions_count=0)
    defaults.update(overrides)
    return AccountState(**defaults)


def base_proposal(**overrides) -> TradeProposal:
    defaults = dict(
        symbol="SPY",
        strategy="long_call",
        max_loss=500.0,
        now=market_hours_now(),
    )
    defaults.update(overrides)
    return TradeProposal(**defaults)


def test_happy_path_allowed():
    decision = evaluate_trade(base_proposal(), base_account(), base_config(), {})
    assert decision.allowed


def test_blocks_outside_market_hours():
    weekend = datetime(2026, 8, 29, 11, 0, tzinfo=ET)  # a Saturday
    decision = evaluate_trade(base_proposal(now=weekend), base_account(), base_config(), {})
    assert not decision.allowed
    assert "market hours" in decision.reason


def test_blocks_undefined_risk_strategy():
    decision = evaluate_trade(
        base_proposal(strategy="naked_call"), base_account(), base_config(), {}
    )
    assert not decision.allowed
    assert "approved defined-risk strategy" in decision.reason


def test_blocks_missing_or_zero_max_loss():
    decision = evaluate_trade(base_proposal(max_loss=0), base_account(), base_config(), {})
    assert not decision.allowed
    assert "max_loss" in decision.reason


def test_daily_circuit_breaker_trips_on_loss_breach():
    """Guards against a circuit breaker that exists conceptually but doesn't
    actually stop new entries once the day has gone bad -- this must block,
    not just warn."""
    account = base_account(daily_pnl_pct=-6.0)  # breached the -5% limit
    decision = evaluate_trade(base_proposal(), account, base_config(), {})
    assert not decision.allowed
    assert "circuit breaker" in decision.reason


def test_circuit_breaker_does_not_trip_below_threshold():
    account = base_account(daily_pnl_pct=-2.0)  # inside the -5% limit
    decision = evaluate_trade(base_proposal(), account, base_config(), {})
    assert decision.allowed


def test_blocks_when_max_concurrent_positions_reached():
    account = base_account(open_positions_count=6)  # equals the cap of 6
    decision = evaluate_trade(base_proposal(), account, base_config(), {})
    assert not decision.allowed
    assert "max concurrent positions" in decision.reason


def test_blocks_oversized_position():
    # 10% of $100k equity = $10k cap; propose a trade above it.
    decision = evaluate_trade(
        base_proposal(max_loss=15_000), base_account(), base_config(), {}
    )
    assert not decision.allowed
    assert "position cap" in decision.reason


def test_cooldown_blocks_repeat_signal_within_window():
    """Guards against a persisting signal re-firing every cycle without a
    cooldown -- the same trade proposed again immediately must be blocked,
    not re-approved."""
    now = market_hours_now()
    key = cooldown_key("SPY", "long_call")
    cooldown_data = mark_cooldown({}, key, now)

    five_minutes_later = base_proposal(now=now + timedelta(minutes=5))
    decision = evaluate_trade(five_minutes_later, base_account(), base_config(), cooldown_data)
    assert not decision.allowed
    assert "cooldown" in decision.reason


def test_cooldown_clears_after_window_elapses():
    now = market_hours_now()
    key = cooldown_key("SPY", "long_call")
    cooldown_data = mark_cooldown({}, key, now)

    after_window = base_proposal(now=now + timedelta(minutes=31))
    decision = evaluate_trade(after_window, base_account(), base_config(), cooldown_data)
    assert decision.allowed


def test_cooldown_is_scoped_per_symbol_and_strategy():
    now = market_hours_now()
    key = cooldown_key("SPY", "long_call")
    cooldown_data = mark_cooldown({}, key, now)

    other_symbol = base_proposal(symbol="QQQ", now=now + timedelta(minutes=1))
    decision = evaluate_trade(other_symbol, base_account(), base_config(), cooldown_data)
    assert decision.allowed

    other_strategy = base_proposal(strategy="long_put", now=now + timedelta(minutes=1))
    decision = evaluate_trade(other_strategy, base_account(), base_config(), cooldown_data)
    assert decision.allowed


@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 9, 1, 9, 29, tzinfo=ET), False),  # 1 min before open
        (datetime(2026, 9, 1, 9, 30, tzinfo=ET), True),  # at open
        (datetime(2026, 9, 1, 16, 0, tzinfo=ET), True),  # at close
        (datetime(2026, 9, 1, 16, 1, tzinfo=ET), False),  # 1 min after close
        (datetime(2026, 8, 30, 11, 0, tzinfo=ET), False),  # a Sunday
    ],
)
def test_is_market_hours_boundaries(when, expected):
    assert is_market_hours(when) is expected
