"""Deterministic, code-enforced pre-trade risk gates.

Every proposed trade from brain.py must pass through evaluate_trade()
before execute.py is allowed to touch the Alpaca Trading API. Nothing
here is a suggestion in a system prompt -- it's checked in code, on
every call, with no path around it. See the "no undefined-risk
strategies" and "no naked short options" defaults below: those are
safety rails, not strategy limiters, and are not configurable via env
vars on purpose.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Defined-risk strategies only. A strategy not in this set is rejected
# outright, regardless of its claimed max_loss -- this is an allowlist
# (fail closed), not a denylist of known-bad strategies (fail open).
ALLOWED_STRATEGIES = {
    "long_call",
    "long_put",
    "vertical_debit_spread",
    "vertical_credit_spread",
    "iron_condor",
    "covered_call",
    "cash_secured_put",
}

DEFAULT_COOLDOWN_STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "cooldowns.json"


@dataclass(frozen=True)
class RiskConfig:
    # No daily loss circuit breaker by design: this is a paper account with
    # no real capital at risk, and the explicit goal is maximizing trade
    # volume/P&L opportunity over the competition week -- a halt-all-new-
    # entries gate works against that with no offsetting real-money
    # protection to justify it. Structural safety (defined-risk-only,
    # leg/symbol validation, per-trade size cap) is unaffected by this.
    max_position_pct: float
    max_concurrent_positions: int
    cooldown_minutes: int
    # Alpaca doesn't support OCO/bracket orders for options (verified against
    # their docs -- only order_class "simple"/"mleg" apply to options), so
    # stop-loss/take-profit can't be attached at entry like on equities. These
    # thresholds are the code-enforced equivalent: checked every cycle,
    # independent of Claude's judgment, in find_protective_exits() below.
    stop_loss_pct: float = -50.0
    take_profit_pct: float = 100.0
    # Scales out part of a winner early instead of all-or-nothing at
    # take_profit_pct -- locks in gains on part of the position while the
    # rest keeps running toward the full target (or gets stopped out).
    # Must stay below take_profit_pct or the partial tier would never fire.
    partial_take_profit_pct: float = 50.0
    partial_take_profit_close_fraction: float = 50.0  # % of the position to close at that tier

    @classmethod
    def from_env(cls) -> "RiskConfig":
        return cls(
            max_position_pct=float(os.environ.get("MAX_POSITION_PCT", "10")),
            max_concurrent_positions=int(os.environ.get("MAX_CONCURRENT_POSITIONS", "20")),
            cooldown_minutes=int(os.environ.get("COOLDOWN_MINUTES", "30")),
            stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "-50")),
            take_profit_pct=float(os.environ.get("TAKE_PROFIT_PCT", "100")),
            partial_take_profit_pct=float(os.environ.get("PARTIAL_TAKE_PROFIT_PCT", "50")),
            partial_take_profit_close_fraction=float(
                os.environ.get("PARTIAL_TAKE_PROFIT_CLOSE_FRACTION", "50")
            ),
        )


@dataclass(frozen=True)
class TradeProposal:
    symbol: str
    strategy: str
    max_loss: float  # dollar amount, must be a known finite figure
    now: datetime  # injectable for tests; must be tz-aware


@dataclass(frozen=True)
class AccountState:
    equity: float
    daily_pnl_pct: float  # (today's realized + unrealized) / start-of-day equity, as a signed pct
    open_positions_count: int


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


def load_cooldowns(path: Path = DEFAULT_COOLDOWN_STATE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_cooldowns(data: dict, path: Path = DEFAULT_COOLDOWN_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def cooldown_key(symbol: str, strategy: str) -> str:
    return f"{symbol}:{strategy}"


def is_in_cooldown(data: dict, key: str, now: datetime, cooldown_minutes: int) -> bool:
    last = data.get(key)
    if last is None:
        return False
    last_dt = datetime.fromisoformat(last)
    elapsed_minutes = (now - last_dt).total_seconds() / 60
    return elapsed_minutes < cooldown_minutes


def mark_cooldown(data: dict, key: str, now: datetime) -> dict:
    data[key] = now.isoformat()
    return data


def is_market_hours(now: datetime) -> bool:
    local = now.astimezone(ET)
    if local.weekday() > 4:  # Sat=5, Sun=6
        return False
    open_t = local.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = local.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= local <= close_t


def evaluate_trade(
    proposal: TradeProposal,
    account: AccountState,
    config: RiskConfig,
    cooldown_data: dict,
) -> RiskDecision:
    """Runs every gate in order, returns the first failure or an allow.

    cooldown_data is passed in (not loaded internally) so callers control
    exactly when it's persisted -- evaluate_trade never writes to disk
    itself. Callers should mark_cooldown() + save_cooldowns() only after
    the trade is actually, successfully executed.
    """
    if not is_market_hours(proposal.now):
        return RiskDecision(False, "outside market hours (9:30-16:00 ET, Mon-Fri)")

    if proposal.strategy not in ALLOWED_STRATEGIES:
        return RiskDecision(
            False,
            f"strategy '{proposal.strategy}' is not an approved defined-risk strategy",
        )

    if proposal.max_loss is None or proposal.max_loss <= 0:
        return RiskDecision(False, "max_loss must be a known, positive, finite dollar amount")

    if account.open_positions_count >= config.max_concurrent_positions:
        return RiskDecision(
            False,
            f"max concurrent positions reached ({account.open_positions_count}/"
            f"{config.max_concurrent_positions})",
        )

    max_allowed_loss = account.equity * (config.max_position_pct / 100)
    if proposal.max_loss > max_allowed_loss:
        return RiskDecision(
            False,
            f"proposed max_loss ${proposal.max_loss:,.2f} exceeds position cap "
            f"${max_allowed_loss:,.2f} ({config.max_position_pct}% of equity)",
        )

    key = cooldown_key(proposal.symbol, proposal.strategy)
    if is_in_cooldown(cooldown_data, key, proposal.now, config.cooldown_minutes):
        return RiskDecision(
            False,
            f"{key} is in cooldown ({config.cooldown_minutes}min window)",
        )

    return RiskDecision(True, "all gates passed")


@dataclass(frozen=True)
class ProtectiveExit:
    symbol: str
    reason: str


_OCC_RE = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")


def _underlying_of(symbol: str) -> str:
    """Best-effort: strip the OCC option suffix to recover the underlying
    ticker, so multi-leg positions sharing an underlying can be grouped and
    closed together instead of one leg at a time."""
    match = _OCC_RE.match(symbol)
    return match.group(1) if match else symbol


def find_protective_exits(positions: list[dict], config: RiskConfig) -> list[ProtectiveExit]:
    """Positions whose unrealized P&L% has breached the stop-loss or
    take-profit threshold. Checked every cycle by loop.py, independent of
    Claude's judgment -- this is the deterministic backstop, not a
    suggestion; Claude's close_position tool is the discretionary layer
    on top of it, not a replacement for it.

    Groups legs by underlying and closes the WHOLE group together if any
    one leg breaches -- closing a single leg of a multi-leg spread in
    isolation can leave the other leg as an accidentally uncovered/naked
    position. Confirmed live: Alpaca rejected exactly this
    ("account not eligible to trade uncovered option contracts") when the
    old per-leg-only logic tried to close just one leg of a debit spread --
    a lucky rejection from Alpaca's own account restriction, not something
    to rely on as the actual safety mechanism. Within a breaching group,
    short legs are ordered before long legs: closing the short leg first
    (buying it back) never creates naked exposure at any intermediate step
    even though these are separate, non-atomic API calls, whereas closing
    a long leg first would momentarily leave the short leg naked.
    """
    groups: dict[str, list[dict]] = {}
    for position in positions:
        groups.setdefault(_underlying_of(position["symbol"]), []).append(position)

    exits = []
    for legs in groups.values():
        breach_reason = None
        for leg in legs:
            plpc = leg.get("unrealized_plpc")
            if plpc is None:
                continue
            if plpc <= config.stop_loss_pct:
                breach_reason = (
                    f"stop-loss triggered on {leg['symbol']} "
                    f"({plpc:+.2f}% <= {config.stop_loss_pct:+.2f}%)"
                )
                break
            if plpc >= config.take_profit_pct:
                breach_reason = (
                    f"take-profit triggered on {leg['symbol']} "
                    f"({plpc:+.2f}% >= {config.take_profit_pct:+.2f}%)"
                )
                break
        if breach_reason is None:
            continue
        ordered_legs = sorted(legs, key=lambda leg: 0 if leg.get("side") == "short" else 1)
        for leg in ordered_legs:
            exits.append(ProtectiveExit(symbol=leg["symbol"], reason=breach_reason))
    return exits


@dataclass(frozen=True)
class PartialExit:
    symbol: str
    close_percentage: float
    reason: str


def find_partial_take_profits(
    positions: list[dict], config: RiskConfig, already_triggered: set[str]
) -> list[PartialExit]:
    """Scales out of winners incrementally instead of all-or-nothing --
    locks in gains on part of a position once it crosses
    partial_take_profit_pct, letting the rest run toward the full
    take_profit_pct target (handled separately by find_protective_exits)
    or get stopped out.

    `already_triggered` (symbols that already had this tier fire) is
    required because unrealized_plpc doesn't reset after a partial close --
    without it, the same tier would fire again on every subsequent poll
    against the remaining runner shares. Callers own persisting this set
    across polls (see loop.py) and should clear a symbol once its position
    is fully closed.
    """
    exits = []
    for position in positions:
        plpc = position.get("unrealized_plpc")
        symbol = position["symbol"]
        if plpc is None or symbol in already_triggered:
            continue
        if config.partial_take_profit_pct <= plpc < config.take_profit_pct:
            # Alpaca's percentage-based close requires a whole-share result.
            # Every position here is 1 contract per leg, so 50% of 1 rounds
            # to 0 -- a request Alpaca will reject every time. Confirmed
            # live: this fired every ~15s for 19 minutes straight against a
            # single-contract position before anyone noticed. Skip cleanly
            # rather than retry-storm a request that can never succeed.
            qty = abs(position.get("qty") or 0)
            if int(qty * config.partial_take_profit_close_fraction / 100) < 1:
                continue
            exits.append(
                PartialExit(
                    symbol=symbol,
                    close_percentage=config.partial_take_profit_close_fraction,
                    reason=(
                        f"partial take-profit triggered ({plpc:+.2f}% >= "
                        f"{config.partial_take_profit_pct:+.2f}%), scaling out "
                        f"{config.partial_take_profit_close_fraction:.0f}%"
                    ),
                )
            )
    return exits
