"""Per-thread CPU of this process, by the role each thread plays.

Every thread in the agent shares one interpreter lock, so "the process is at
60 % of a core" says nothing about which thread is holding the others up.
This reads each kernel task's CPU time from ``/proc/self/task`` and reports
the share of one core it used since the last call, keyed by role.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_TASKS = Path("/proc/self/task")
OTHER = "other"


def role_of(thread_name: str) -> str:
    """Map a thread name to the role its CPU is reported under."""
    if thread_name == "MainThread":
        return "main"
    for prefix in ("can-", "serial-"):
        if thread_name.startswith(prefix):
            return thread_name[len(prefix) :]
    return thread_name


def _task_cpu_ticks(tid: int) -> int | None:
    try:
        stat = (_TASKS / str(tid) / "stat").read_text()
    except OSError:
        return None
    # The command name is parenthesised and may itself contain spaces.
    fields = stat.rpartition(")")[2].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


class ThreadCpu:
    """Percent of one core each role used between successive ``observe`` calls."""

    def __init__(self, roles: list[str]) -> None:
        self.roles = [*roles, OTHER]
        self._ticks_per_s = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._last_ticks: dict[int, int] = {}
        self._last_mono = time.monotonic()

    def observe(self) -> dict[str, float]:
        """CPU percent per role since the previous call (zeros on the first)."""
        now = time.monotonic()
        elapsed = now - self._last_mono
        self._last_mono = now
        named = {
            thread.native_id: role_of(thread.name)
            for thread in threading.enumerate()
            if thread.native_id is not None
        }
        try:
            tids = [int(entry.name) for entry in _TASKS.iterdir()]
        except OSError:
            tids = list(named)
        ticks: dict[int, int] = {}
        used = dict.fromkeys(self.roles, 0.0)
        for tid in tids:
            total = _task_cpu_ticks(tid)
            if total is None:
                continue
            ticks[tid] = total
            before = self._last_ticks.get(tid)
            if before is None or elapsed <= 0:
                continue
            role = named.get(tid, OTHER)
            if role not in used:
                role = OTHER
            used[role] += 100.0 * (total - before) / self._ticks_per_s / elapsed
        self._last_ticks = ticks
        return used
