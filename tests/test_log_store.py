"""Tests for the audit-trail log itself -- the "no silent failures, no
untracked positions" guarantee only holds if every entry actually lands in
state/cycles.jsonl and read_cycles can read it back correctly.
"""

from pathlib import Path
from unittest.mock import MagicMock

from agent.log_store import log_auto_close, log_cycle, log_cycle_failure, read_cycles
from agent.risk_gates import AccountState


def _account() -> AccountState:
    return AccountState(equity=100_000.0, daily_pnl_pct=0.0, open_positions_count=1)


def _message_with_usage(input_tokens=100, output_tokens=50, cache_read=0, cache_creation=0):
    message = MagicMock()
    message.content = []
    message.usage.input_tokens = input_tokens
    message.usage.output_tokens = output_tokens
    message.usage.cache_read_input_tokens = cache_read
    message.usage.cache_creation_input_tokens = cache_creation
    return message


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


def test_log_cycle_sums_usage_across_transcript(tmp_path):
    """The only real way to verify prompt caching is working, rather than
    assuming it from the code alone -- one cycle's tool-calling loop makes
    several internal API calls, and each one's usage must be counted."""
    path = tmp_path / "cycles.jsonl"
    transcript = [
        _message_with_usage(input_tokens=50, output_tokens=20, cache_read=0, cache_creation=2000),
        _message_with_usage(input_tokens=10, output_tokens=30, cache_read=2000, cache_creation=0),
        _message_with_usage(input_tokens=10, output_tokens=15, cache_read=2000, cache_creation=0),
    ]
    log_cycle(_account(), transcript, decisions=[], path=path)

    entries = read_cycles(path=path)
    usage = entries[0]["usage"]
    assert usage["input_tokens"] == 70
    assert usage["output_tokens"] == 65
    assert usage["cache_read_input_tokens"] == 4000
    assert usage["cache_creation_input_tokens"] == 2000


def test_log_cycle_handles_messages_without_usage(tmp_path):
    path = tmp_path / "cycles.jsonl"
    log_cycle(_account(), transcript=[object()], decisions=[], path=path)

    entries = read_cycles(path=path)
    assert entries[0]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
