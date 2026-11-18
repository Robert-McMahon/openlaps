"""An opt-in profiler that runs inside the agent.

The agent's threads share one interpreter, so the useful question on a
loaded board is not how busy the process is but which functions the time
goes to. An external profiler needs ptrace and a toolchain the vehicle host
does not have; this needs neither. With ``OPENLAPS_SELF_PROFILE_S=<n>`` the
agent runs the interpreter's own deterministic profiler (which, from Python
3.12, covers every thread) for a few consecutive ``n``-second windows, logs
a report for each, then stops.

Profiling slows interpreted code noticeably, so the shares are what to read,
not the absolute times. A function that only waits (a lock, ``select``, a
sleep) accumulates the time it was blocked; those are listed apart.
"""

from __future__ import annotations

import cProfile
import logging
import pstats
import threading
import time

logger = logging.getLogger(__name__)

WINDOWS = 3
WARMUP_S = 15.0
TOP = 28
# Callables whose time is time spent blocked, not working.
_WAITS = ("acquire", "select", "poll", "sleep", "wait", "recv", "read", "get", "join")


def _name(key: tuple[str, int, str]) -> str:
    path, line, function = key
    for marker in ("/site-packages/", "/src/", "/lib/python"):
        if marker in path:
            path = path.rsplit(marker, 1)[1]
            break
    return f"{path}:{line}:{function}" if line else function


def _is_wait(key: tuple[str, int, str]) -> bool:
    function = key[2]
    return any(word in function for word in _WAITS) and (
        "built-in" in function or key[1] == 0 or "method" in function
    )


def report(profile: cProfile.Profile, index: int, seconds: float) -> str:
    """The costliest functions of one window, as shares of the time not spent waiting."""
    stats = pstats.Stats(profile).stats  # type: ignore[attr-defined]
    working = {key: row for key, row in stats.items() if not _is_wait(key)}
    waiting = sum(row[2] for key, row in stats.items() if _is_wait(key))
    busy = sum(row[2] for row in working.values()) or 1e-9
    lines = [
        f"self-profile window {index}: {seconds:.0f} s, {busy:.1f} s working "
        f"({100 * busy / seconds:.0f}% of one core while profiled), {waiting:.0f} s waiting"
    ]
    lines.append("  own time (share of working time, calls, function):")
    for key, row in sorted(working.items(), key=lambda item: -item[1][2])[:TOP]:
        lines.append(f"    {100 * row[2] / busy:5.1f}%  {row[1]:>8}  {_name(key)}")
    lines.append("  including callees, openlaps code only:")
    ours = {
        key: row
        for key, row in working.items()
        if any(
            part in key[0] for part in ("/agent/", "/collectors/", "/core/", "/fusion/", "/timing/")
        )
    }
    for key, row in sorted(ours.items(), key=lambda item: -item[1][3])[:TOP]:
        lines.append(f"    {100 * row[3] / busy:5.1f}%  {row[1]:>8}  {_name(key)}")
    return "\n".join(lines)


def start(window_s: float) -> threading.Thread:
    """Profile every thread for a few windows of ``window_s`` seconds, then stop."""

    def run() -> None:
        time.sleep(WARMUP_S)
        for index in range(1, WINDOWS + 1):
            profile = cProfile.Profile()
            started = time.monotonic()
            profile.enable()
            time.sleep(window_s)
            profile.disable()
            logger.info("%s", report(profile, index, time.monotonic() - started))
        logger.info("self-profile finished")

    thread = threading.Thread(target=run, name="self-profile", daemon=True)
    thread.start()
    return thread
