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


def read_cycles(path: Path = LOG_PATH, limit: int = 50) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    entries = [json.loads(line) for line in lines[-limit:] if line.strip()]
    entries.reverse()
    return entries
