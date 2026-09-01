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
    find_partial_take_profits,
    find_protective_exits,
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


def test_no_daily_circuit_breaker_by_design():
    """Deliberately removed: this is a paper account with no real capital
    at risk, and the goal is maximizing trade volume/P&L opportunity over
    the competition week -- a halt-all-new-entries gate works against that
    with no offsetting real-money protection to justify it. A large daily
    loss must NOT block further trades."""
    account = base_account(daily_pnl_pct=-40.0)  # a large daily loss
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


def _position(symbol="SPY", unrealized_plpc=0.0, qty=2.0, side="long") -> dict:
    # qty defaults to 2 (not Sentinel Gate's real 1-contract-per-leg norm)
    # so existing partial-take-profit tests exercise the "fires" path;
    # the qty=1 case (which can never satisfy a 50% partial close) has
    # its own dedicated test below.
    return {"symbol": symbol, "unrealized_plpc": unrealized_plpc, "qty": qty, "side": side}


def test_no_protective_exit_within_thresholds():
    """Alpaca doesn't support OCO/bracket orders for options, so this
    threshold check is the only thing standing between a losing position
    and it just sitting there until Claude happens to notice -- must not
    fire when nothing has actually breached."""
    config = base_config()
    positions = [_position(unrealized_plpc=10.0), _position("QQQ", unrealized_plpc=-10.0)]
    assert find_protective_exits(positions, config) == []


def test_stop_loss_triggers_exit():
    config = base_config()
    positions = [_position("SPY", unrealized_plpc=-55.0)]
    exits = find_protective_exits(positions, config)
    assert len(exits) == 1
    assert exits[0].symbol == "SPY"
    assert "stop-loss" in exits[0].reason


def test_take_profit_triggers_exit():
    config = base_config()
    positions = [_position("QQQ", unrealized_plpc=150.0)]
    exits = find_protective_exits(positions, config)
    assert len(exits) == 1
    assert exits[0].symbol == "QQQ"
    assert "take-profit" in exits[0].reason


def test_exit_exactly_at_threshold_triggers():
    config = base_config()
    positions = [_position("SPY", unrealized_plpc=config.stop_loss_pct)]
    assert len(find_protective_exits(positions, config)) == 1


def test_position_missing_unrealized_plpc_is_skipped():
    config = base_config()
    positions = [{"symbol": "SPY"}]  # no unrealized_plpc key
    assert find_protective_exits(positions, config) == []


def test_multiple_positions_only_breaching_ones_exit():
    config = base_config()
    positions = [
        _position("SPY", unrealized_plpc=-10.0),  # fine
        _position("QQQ", unrealized_plpc=-60.0),  # stop-loss
        _position("AAPL", unrealized_plpc=120.0),  # take-profit
    ]
    exits = find_protective_exits(positions, config)
    assert {e.symbol for e in exits} == {"QQQ", "AAPL"}


def test_partial_take_profit_fires_between_thresholds():
    config = base_config()  # partial=50%, full=100% by default
    positions = [_position("SPY", unrealized_plpc=60.0)]
    exits = find_partial_take_profits(positions, config, already_triggered=set())
    assert len(exits) == 1
    assert exits[0].symbol == "SPY"
    assert exits[0].close_percentage == config.partial_take_profit_close_fraction


def test_partial_take_profit_does_not_fire_below_threshold():
    config = base_config()
    positions = [_position("SPY", unrealized_plpc=20.0)]
    assert find_partial_take_profits(positions, config, already_triggered=set()) == []


def test_partial_take_profit_does_not_fire_at_or_above_full_target():
    """Once a position has crossed the full take-profit target, that's
    find_protective_exits' job (a full close) -- the partial tier must not
    also fire and double-report the same position."""
    config = base_config()
    positions = [_position("SPY", unrealized_plpc=150.0)]
    assert find_partial_take_profits(positions, config, already_triggered=set()) == []


def test_partial_take_profit_skips_already_triggered_symbol():
    """The core scenario this state-tracking exists for: unrealized_plpc
    doesn't reset after a partial close, so without already_triggered the
    same tier would fire again on every subsequent poll against the
    remaining runner shares."""
    config = base_config()
    positions = [_position("SPY", unrealized_plpc=60.0)]
    exits = find_partial_take_profits(positions, config, already_triggered={"SPY"})
    assert exits == []


def test_partial_take_profit_skips_single_contract_position():
    """Confirmed live: Sentinel Gate always trades 1 contract per leg, and
    50% of 1 rounds to 0 -- a close Alpaca will reject every time ('order
    size of zero'). Without this check, the protective loop retry-storms
    Alpaca every ~15s indefinitely since a failed close never gets added
    to already_triggered. Must skip cleanly instead."""
    config = base_config()
    positions = [_position("SPY", unrealized_plpc=60.0, qty=1.0)]
    assert find_partial_take_profits(positions, config, already_triggered=set()) == []


def test_partial_take_profit_handles_missing_qty():
    config = base_config()
    positions = [{"symbol": "SPY", "unrealized_plpc": 60.0}]  # no qty key
    assert find_partial_take_profits(positions, config, already_triggered=set()) == []


def test_protective_exit_closes_whole_spread_not_just_breaching_leg():
    """The core scenario this exists for, confirmed live: closing only one
    leg of a multi-leg spread can leave the other leg as an accidentally
    uncovered/naked position. A stop-loss on one leg of a two-leg AAPL
    spread must close BOTH legs, not just the one that breached."""
    config = base_config()
    positions = [
        _position("AAPL260918C00200000", unrealized_plpc=-60.0, side="long"),
        _position("AAPL260918C00210000", unrealized_plpc=10.0, side="short"),
    ]
    exits = find_protective_exits(positions, config)
    assert {e.symbol for e in exits} == {"AAPL260918C00200000", "AAPL260918C00210000"}


def test_protective_exit_closes_short_leg_before_long_leg():
    """Closing the short leg first (buying it back) never creates naked
    exposure at any intermediate step, even though these are separate,
    non-atomic API calls -- closing the long leg first would momentarily
    leave the short leg naked."""
    config = base_config()
    positions = [
        _position("AAPL260918C00200000", unrealized_plpc=10.0, side="long"),
        _position("AAPL260918C00210000", unrealized_plpc=-60.0, side="short"),
    ]
    exits = find_protective_exits(positions, config)
    assert [e.symbol for e in exits] == ["AAPL260918C00210000", "AAPL260918C00200000"]


def test_protective_exit_only_affects_breaching_underlying():
    """A breach on one underlying's spread must not touch an unrelated
    underlying's healthy position."""
    config = base_config()
    positions = [
        _position("AAPL260918C00200000", unrealized_plpc=-60.0, side="long"),
        _position("AAPL260918C00210000", unrealized_plpc=10.0, side="short"),
        _position("MSFT260918C00300000", unrealized_plpc=5.0, side="long"),
    ]
    exits = find_protective_exits(positions, config)
    assert {e.symbol for e in exits} == {"AAPL260918C00200000", "AAPL260918C00210000"}


def test_protective_exit_single_leg_position_still_works():
    """Single-leg strategies (long_call, long_put, covered_call,
    cash_secured_put) have no pairing risk -- must still trigger normally,
    not regress just because grouping logic was added."""
    config = base_config()
    positions = [_position("SPY260918C00450000", unrealized_plpc=-55.0, side="long")]
    exits = find_protective_exits(positions, config)
    assert [e.symbol for e in exits] == ["SPY260918C00450000"]
