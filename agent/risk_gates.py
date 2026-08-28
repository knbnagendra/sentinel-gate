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
    daily_loss_limit_pct: float
    max_position_pct: float
    max_concurrent_positions: int
    cooldown_minutes: int

    @classmethod
    def from_env(cls) -> "RiskConfig":
        return cls(
            daily_loss_limit_pct=float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "5")),
            max_position_pct=float(os.environ.get("MAX_POSITION_PCT", "10")),
            max_concurrent_positions=int(os.environ.get("MAX_CONCURRENT_POSITIONS", "6")),
            cooldown_minutes=int(os.environ.get("COOLDOWN_MINUTES", "30")),
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

    if account.daily_pnl_pct <= -abs(config.daily_loss_limit_pct):
        return RiskDecision(
            False,
            f"daily circuit breaker tripped ({account.daily_pnl_pct:.2f}% <= "
            f"-{config.daily_loss_limit_pct}%), no new entries until next session",
        )

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
