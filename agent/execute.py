"""Direct Alpaca Trading API execution -- the only path an approved trade
takes to become a real (paper) order. Runs only after risk_gates.evaluate_trade
has already approved the proposal; this module trusts that gate and does not
re-validate risk, it only translates the approved trade into an Alpaca order.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest

from agent.risk_gates import TradeProposal

MULTI_LEG_STRATEGIES = {"vertical_debit_spread", "vertical_credit_spread", "iron_condor"}

# Exactly which leg count + buy/sell composition each strategy is allowed to
# submit as. This is what stops a strategy label that passed risk_gates
# (e.g. "long_call") from being executed with a leg spec that actually
# describes something else entirely (e.g. a naked short) -- the gates only
# ever see the strategy name and max_loss, never the legs themselves.
_SINGLE_LONG = {"long_call", "long_put"}
_SINGLE_SHORT = {"covered_call", "cash_secured_put"}
_TWO_LEG_SPREADS = {"vertical_debit_spread", "vertical_credit_spread"}

_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class OrderLeg:
    occ_symbol: str
    side: str  # "buy" or "sell"
    ratio_qty: float = 1.0


@dataclass(frozen=True)
class ParsedOccSymbol:
    underlying: str
    right: str  # "C" or "P"
    strike: float


def _trading_client() -> TradingClient:
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=os.environ.get("ALPACA_PAPER_TRADE", "true").lower() == "true",
    )


def _parse_legs(legs_description: str) -> list[OrderLeg]:
    legs = []
    for chunk in legs_description.split(";"):
        symbol, sep, side = chunk.strip().partition(":")
        symbol = symbol.strip()
        side = side.strip().lower()
        if not symbol:
            continue
        if not sep or side not in ("buy", "sell"):
            raise ValueError(
                f"leg {chunk.strip()!r} must be '<OCC symbol>:buy' or '<OCC symbol>:sell'"
            )
        legs.append(OrderLeg(occ_symbol=symbol, side=side))
    if not legs:
        raise ValueError(f"could not parse any option legs from {legs_description!r}")
    return legs


def _parse_occ_symbol(occ_symbol: str) -> ParsedOccSymbol:
    match = _OCC_RE.match(occ_symbol)
    if not match:
        raise ValueError(f"not a valid OCC option symbol: {occ_symbol!r}")
    underlying, _expiry, right, strike_raw = match.groups()
    return ParsedOccSymbol(underlying=underlying, right=right, strike=int(strike_raw) / 1000)


def _validate_legs_match_strategy(strategy: str, legs: list[OrderLeg]) -> None:
    """Rejects any leg spec whose buy/sell shape doesn't match what the
    strategy name claims -- this is the check that makes the strategy
    allowlist in risk_gates.py actually bind to what gets submitted."""
    sides = [leg.side for leg in legs]

    if strategy in _SINGLE_LONG:
        if sides != ["buy"]:
            raise ValueError(f"{strategy} must be exactly one long (buy) leg, got {sides!r}")
    elif strategy in _SINGLE_SHORT:
        if sides != ["sell"]:
            raise ValueError(f"{strategy} must be exactly one short (sell) leg, got {sides!r}")
    elif strategy in _TWO_LEG_SPREADS:
        if len(legs) != 2 or sorted(sides) != ["buy", "sell"]:
            raise ValueError(
                f"{strategy} must be exactly one buy leg and one sell leg, got {sides!r}"
            )
    elif strategy == "iron_condor":
        if len(legs) != 4 or sides.count("buy") != 2 or sides.count("sell") != 2:
            raise ValueError(
                f"iron_condor must be exactly two buy legs and two sell legs, got {sides!r}"
            )
    else:
        raise ValueError(f"unknown strategy {strategy!r}")


def _validate_symbol_matches_legs(symbol: str, legs: list[OrderLeg]) -> None:
    """The proposed `symbol` (what cooldowns and the decision log are keyed
    on) must actually be the underlying encoded in the legs -- otherwise a
    mismatched proposal executes one symbol while tracking another."""
    for leg in legs:
        parsed = _parse_occ_symbol(leg.occ_symbol)
        if parsed.underlying.upper() != symbol.upper():
            raise ValueError(
                f"proposed symbol {symbol!r} does not match leg underlying "
                f"{parsed.underlying!r} (leg {leg.occ_symbol!r})"
            )


def _verify_coverage(client: TradingClient, strategy: str, legs: list[OrderLeg]) -> None:
    """covered_call and cash_secured_put are only defined-risk if the thing
    that 'covers' them actually exists -- this checks that against the real
    account instead of trusting the strategy label."""
    parsed = _parse_occ_symbol(legs[0].occ_symbol)

    if strategy == "covered_call":
        try:
            position = client.get_open_position(parsed.underlying)
        except APIError:
            raise ValueError(
                f"covered_call on {parsed.underlying} requires an existing long stock "
                f"position of >= 100 shares; none found"
            )
        if float(position.qty) < 100:
            raise ValueError(
                f"covered_call on {parsed.underlying} requires >= 100 shares, "
                f"account holds {position.qty}"
            )

    elif strategy == "cash_secured_put":
        required_cash = parsed.strike * 100
        available_cash = float(client.get_account().cash)
        if available_cash < required_cash:
            raise ValueError(
                f"cash_secured_put on {parsed.underlying} at strike {parsed.strike} "
                f"requires ${required_cash:,.2f} secured cash, only "
                f"${available_cash:,.2f} available"
            )


def execute_trade(proposal: TradeProposal, legs_description: str) -> str:
    client = _trading_client()
    legs = _parse_legs(legs_description)
    _validate_symbol_matches_legs(proposal.symbol, legs)
    _validate_legs_match_strategy(proposal.strategy, legs)
    _verify_coverage(client, proposal.strategy, legs)

    if proposal.strategy in MULTI_LEG_STRATEGIES:
        order = MarketOrderRequest(
            qty=1,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.MLEG,
            legs=[
                OptionLegRequest(
                    symbol=leg.occ_symbol,
                    ratio_qty=leg.ratio_qty,
                    side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
                )
                for leg in legs
            ],
        )
    else:
        leg = legs[0]
        order = MarketOrderRequest(
            symbol=leg.occ_symbol,
            qty=1,
            side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
        )

    submitted = client.submit_order(order)
    return f"order {submitted.id} ({submitted.status}) for {legs_description}"


def close_position(symbol: str) -> str:
    """Close an existing open position -- the counterpart to execute_trade
    for managing a position out (profit-take or cut a loss) rather than
    entering a new one."""
    client = _trading_client()
    try:
        order = client.close_position(symbol)
    except APIError as exc:
        raise ValueError(f"could not close position {symbol!r}: {exc}")
    return f"close order {order.id} ({order.status}) for {symbol}"
