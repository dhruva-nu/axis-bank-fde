"""One record per customer query: every Jev call, tool call and the reply, in order.

Written as one JSON line per query to logs/jev_turns.jsonl (JEV_LOG_FILE overrides), and
read by the log viewer on :8001 (jev_log.py). A file rather than shared memory, so the
viewer can run in the same process as the chat or on its own, and the log survives a restart.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import ROOT

LOG_FILE = Path(os.getenv("JEV_LOG_FILE") or ROOT / "logs" / "jev_turns.jsonl")
_write_lock = threading.Lock()


class TurnLog:
    def __init__(self, message: str, customer: str, pending: Any) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.message = message
        self.customer = customer
        self.pending_before = pending
        self.started = time.time()
        self._t0 = time.perf_counter()
        self.steps: list[dict] = []

    def ms(self) -> int:
        return round((time.perf_counter() - self._t0) * 1000)

    def step(self, kind: str, title: str, **data: Any) -> dict:
        """kind: jev | resolve | tool | llm | note. Returns the entry so it can be filled in later."""
        entry = {"kind": kind, "title": title, "at_ms": self.ms(), **data}
        self.steps.append(entry)
        return entry

    def finish(self, reply: str, pending: Any, error: str | None = None) -> None:
        record = {
            "id": self.id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "customer": self.customer,
            "message": self.message,
            "pending_before": self.pending_before,
            "pending_after": pending,
            "steps": self.steps,
            "reply": reply,
            "error": error,
            "total_ms": self.ms(),
            "jev_calls": sum(1 for s in self.steps if s["kind"] == "jev"),
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _write_lock:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def read_turns(after: int = 0) -> tuple[list[dict], int]:
    """Records from line `after` on, and the total line count (the next `after`)."""
    if not LOG_FILE.exists():
        return [], 0
    with _write_lock:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[after:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written line from a crash; skip it
    return out, len(lines)


def clear() -> None:
    with _write_lock:
        if LOG_FILE.exists():
            LOG_FILE.write_text("", encoding="utf-8")
