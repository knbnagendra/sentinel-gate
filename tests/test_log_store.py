"""Tests for the audit-trail log itself -- the "no silent failures, no
untracked positions" guarantee only holds if every entry actually lands in
state/cycles.jsonl and read_cycles can read it back correctly.
"""

from pathlib import Path

from agent.log_store import log_auto_close, log_cycle_failure, read_cycles
from agent.risk_gates import AccountState


def _account() -> AccountState:
    return AccountState(equity=100_000.0, daily_pnl_pct=0.0, open_positions_count=1)


def test_log_cycle_failure_roundtrips(tmp_path):
    path = tmp_path / "cycles.jsonl"
    log_cycle_failure(_account(), "boom", path=path)

    entries = read_cycles(path=path)
    assert len(entries) == 1
    assert "CYCLE FAILED: boom" in entries[0]["reasoning"]
    assert entries[0]["decisions"] == []
    assert entries[0]["account"]["equity"] == 100_000.0


def test_log_cycle_failure_handles_missing_account(tmp_path):
    """A failure can happen before an account snapshot is even fetched --
    must still be logged, not silently dropped, just without account detail."""
    path = tmp_path / "cycles.jsonl"
    log_cycle_failure(None, "no account yet", path=path)

    entries = read_cycles(path=path)
    assert len(entries) == 1
    assert entries[0]["account"] is None
    assert "no account yet" in entries[0]["reasoning"]


def test_log_auto_close_records_every_decision(tmp_path):
    path = tmp_path / "cycles.jsonl"
    decisions = [
        {"symbol": "SPY", "strategy": "protective_exit", "gate_allowed": True},
        {"symbol": "QQQ", "strategy": "protective_exit", "gate_allowed": False},
    ]
    log_auto_close(_account(), decisions, path=path)

    entries = read_cycles(path=path)
    assert len(entries) == 1
    assert entries[0]["decisions"] == decisions


def test_read_cycles_returns_newest_first(tmp_path):
    path = tmp_path / "cycles.jsonl"
    log_cycle_failure(_account(), "first", path=path)
    log_cycle_failure(_account(), "second", path=path)

    entries = read_cycles(path=path)
    assert len(entries) == 2
    assert "second" in entries[0]["reasoning"]
    assert "first" in entries[1]["reasoning"]


def test_read_cycles_missing_file_returns_empty():
    assert read_cycles(path=Path("/nonexistent/path/cycles.jsonl")) == []


def test_read_cycles_skips_corrupted_line(tmp_path):
    """cycles.jsonl is written concurrently by both the reasoning loop and
    the protective loop from separate threads -- a rare interleaved write
    corrupting one line must not take down the entire dashboard."""
    path = tmp_path / "cycles.jsonl"
    log_cycle_failure(_account(), "good entry one", path=path)
    with path.open("a") as f:
        f.write("{not valid json at all\n")
    log_cycle_failure(_account(), "good entry two", path=path)

    entries = read_cycles(path=path)
    assert len(entries) == 2
    reasonings = {e["reasoning"] for e in entries}
    assert any("good entry one" in r for r in reasonings)
    assert any("good entry two" in r for r in reasonings)
