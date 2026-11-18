"""An opt-in sampling profiler that runs inside the agent.

The agent's threads share one interpreter, so the useful question on a
loaded board is not how busy the process is but which function each thread
is in. An external sampler needs ptrace and a toolchain the vehicle host
does not have; this one needs neither. With ``OPENLAPS_SELF_PROFILE_S=<n>``
the agent samples every thread's stack about 200 times a second and logs a
report for each of a few consecutive ``n``-second windows, then stops.

A sample taken while a thread is parked in a wait (a lock, ``select``, a
serial read) says nothing about cost, so those are counted apart as idle and
the tables cover the rest. "self" is the function a thread was executing;
"total" also counts every caller above it.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import Counter
from types import FrameType

from agent.thread_cpu import role_of

logger = logging.getLogger(__name__)

WINDOWS = 4
WARMUP_S = 15.0
INTERVAL_S = 0.005
TOP = 14
# Leaf functions that mean "blocked", not "working".
_IDLE_LEAVES = frozenset(
    {"wait", "select", "sleep", "poll", "recv", "read", "readline", "accept", "_recv", "join"}
)


def _label(frame: FrameType) -> str:
    code = frame.f_code
    path = code.co_filename
    for marker in ("/site-packages/", "/src/", "/lib/python"):
        if marker in path:
            path = path.rsplit(marker, 1)[1]
            break
    return f"{path}:{code.co_name}"


class _Window:
    def __init__(self) -> None:
        self.samples: Counter[str] = Counter()
        self.idle: Counter[str] = Counter()
        self.self_time: dict[str, Counter[str]] = {}
        self.total_time: dict[str, Counter[str]] = {}

    def add(self, role: str, frame: FrameType) -> None:
        self.samples[role] += 1
        if frame.f_code.co_name in _IDLE_LEAVES:
            self.idle[role] += 1
            return
        self.self_time.setdefault(role, Counter())[_label(frame)] += 1
        seen: set[str] = set()
        cursor: FrameType | None = frame
        while cursor is not None:
            seen.add(_label(cursor))
            cursor = cursor.f_back
        self.total_time.setdefault(role, Counter()).update(seen)

    def report(self, index: int, seconds: float) -> str:
        lines = [f"self-profile window {index} ({seconds:.0f} s)"]
        for role in sorted(self.samples, key=lambda r: self.idle[r] - self.samples[r]):
            taken = self.samples[role]
            active = taken - self.idle[role]
            if active * 200 < taken:  # under half a percent busy
                continue
            lines.append(f"  [{role}] busy in {100 * active / taken:.1f}% of {taken} samples")
            for title, table in (("self", self.self_time), ("total", self.total_time)):
                lines.append(f"    {title}:")
                for label, count in table.get(role, Counter()).most_common(TOP):
                    lines.append(f"      {100 * count / active:5.1f}%  {label}")
        return "\n".join(lines)


def start(window_s: float) -> threading.Thread:
    """Sample every thread for a few windows of ``window_s`` seconds, then stop."""

    def run() -> None:
        own = threading.get_ident()
        time.sleep(WARMUP_S)
        for index in range(1, WINDOWS + 1):
            window = _Window()
            started = time.monotonic()
            while time.monotonic() - started < window_s:
                names = {thread.ident: thread.name for thread in threading.enumerate()}
                for ident, frame in sys._current_frames().items():  # noqa: SLF001
                    if ident != own:
                        window.add(role_of(names.get(ident, "other")), frame)
                time.sleep(INTERVAL_S)
            logger.info("%s", window.report(index, time.monotonic() - started))
        logger.info("self-profile finished")

    thread = threading.Thread(target=run, name="self-profile", daemon=True)
    thread.start()
    return thread
