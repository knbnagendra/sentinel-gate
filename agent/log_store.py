"""Append-only cycle log: every cycle's account snapshot, Claude's reasoning
text, and every propose_trade decision (gate verdict + execution result).
Feeds the dashboard's decision feed and the eventual WRITEUP.md -- this is
the ground-truth history the write-up gets built from near the deadline.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from agent.risk_gates import AccountState

LOG_PATH = Path(__file__).resolve().parent.parent / "state" / "cycles.jsonl"


def _extract_reasoning(transcript: list) -> str:
    parts = []
    for message in transcript:
        for block in getattr(message, "content", []):
            if getattr(block, "type", None) == "text" and block.text.strip():
                parts.append(block.text.strip())
    return "\n\n".join(parts)


def log_cycle(
    account: AccountState,
    transcript: list,
    decisions: list[dict],
    path: Path = LOG_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account": asdict(account),
        "reasoning": _extract_reasoning(transcript),
        "decisions": decisions,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_auto_close(
    account: AccountState,
    decisions: list[dict],
    path: Path = LOG_PATH,
) -> None:
    """Records automatic stop-loss/take-profit closes -- same log, same
    shape read_cycles/the dashboard already expect, but with no Claude
    transcript since these run independent of any reasoning turn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account": asdict(account),
        "reasoning": "Automatic stop-loss/take-profit check (code-enforced, no Claude reasoning this pass).",
        "decisions": decisions,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_cycle_failure(
    account: AccountState | None,
    error: str,
    path: Path = LOG_PATH,
) -> None:
    """Records a reasoning-cycle or protective-check crash in the same
    audit trail as everything else -- no failure should be visible only in
    journalctl and nowhere else. `account` may be None if the failure
    happened before an account snapshot could even be fetched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account": asdict(account) if account is not None else None,
        "reasoning": f"CYCLE FAILED: {error}",
        "decisions": [],
    }
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_cycles(path: Path = LOG_PATH, limit: int = 50) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    entries = [json.loads(line) for line in lines[-limit:] if line.strip()]
    entries.reverse()
    return entries
