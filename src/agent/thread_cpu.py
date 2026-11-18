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


def _parse_ticks(stat: bytes) -> int:
    # The command name is parenthesised and may itself contain spaces.
    fields = stat.rpartition(b")")[2].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def _task_name(tid: int) -> str:
    try:
        return (_TASKS / str(tid) / "comm").read_text().strip()
    except OSError:
        return OTHER


class ThreadCpu:
    """Percent of one core each role used between successive ``observe`` calls."""

    def __init__(self, roles: list[str]) -> None:
        self.roles = [*roles, OTHER]
        self._ticks_per_s = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._last_ticks: dict[int, int] = {}
        self._stat_fds: dict[int, int] = {}
        self._names: dict[int, str] = {}
        self._last_mono = time.monotonic()

    def _ticks(self, tid: int) -> int | None:
        """CPU ticks of one task, through a descriptor kept open between calls."""
        descriptor = self._stat_fds.get(tid)
        try:
            if descriptor is None:
                descriptor = os.open(f"/proc/self/task/{tid}/stat", os.O_RDONLY)
                self._stat_fds[tid] = descriptor
            return _parse_ticks(os.pread(descriptor, 512, 0))
        except (OSError, ValueError, IndexError):
            self._forget(tid)
            return None

    def _forget(self, tid: int) -> None:
        self._names.pop(tid, None)
        descriptor = self._stat_fds.pop(tid, None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

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
            tids = [int(name) for name in os.listdir(_TASKS)]
        except OSError:
            tids = list(named)
        ticks: dict[int, int] = {}
        used = dict.fromkeys(self.roles, 0.0)
        for tid in tids:
            total = self._ticks(tid)
            if total is None:
                continue
            ticks[tid] = total
            before = self._last_ticks.get(tid)
            if before is None or elapsed <= 0:
                continue
            role = named.get(tid)
            if role is None:
                # Not a Python thread: the compiled CAN reader names its own.
                # A task's name does not change, so it is read once.
                role = self._names.get(tid)
                if role is None:
                    role = self._names[tid] = role_of(_task_name(tid))
            if role not in used:
                role = OTHER
            used[role] += 100.0 * (total - before) / self._ticks_per_s / elapsed
        for gone in set(self._stat_fds) - set(ticks):
            self._forget(gone)
        self._last_ticks = ticks
        return used
