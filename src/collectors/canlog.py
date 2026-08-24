"""Raw CAN capture: relay ``candump -L`` into crash-safe segments.

``openlaps-canlog can0`` spawns candump (via ``stdbuf -oL`` so idle-rate
frames don't linger in a stdio block buffer) and appends each frame line to
a :class:`collectors.rawlog.RawLogWriter` run under
``OPENLAPS_RAW_CAPTURE_DIR``. candump owns the line format — the same
``(sec.usec) iface id#data`` text that ``tools/replay.py`` and the
``tests/fixtures/candump/`` fixtures consume — so captures are replay
fixtures with no conversion step.

Reading the bus this way is side-effect free: socketCAN is broadcast, so
this process and the agent's ``CanCollector`` each get every frame on their
own socket. Supervision is split by failure class: a candump exit (interface
down, not yet created) is retried here with backoff into the *same* run,
while a capture-write failure (disk full, permissions) exits the process so
systemd restarts it into a fresh run (``deploy/systemd/
openlaps-canlog@.service``).
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

from collectors.rawlog import (
    DEFAULT_FSYNC_INTERVAL_S,
    DEFAULT_ROTATE_INTERVAL_S,
    RawLogWriter,
)

logger = logging.getLogger(__name__)

_BACKOFF_START_S = 0.5
_BACKOFF_MAX_S = 30.0
_CHILD_STOP_TIMEOUT_S = 5.0


class CanLogService:
    """Spawn candump, relay its stdout lines into a capture run, forever."""

    def __init__(
        self,
        interface: str,
        base_dir: Path,
        *,
        command: list[str] | None = None,
        rotate_interval_s: float = DEFAULT_ROTATE_INTERVAL_S,
        fsync_interval_s: float = DEFAULT_FSYNC_INTERVAL_S,
        backoff_start_s: float = _BACKOFF_START_S,
        backoff_max_s: float = _BACKOFF_MAX_S,
    ) -> None:
        self.interface = interface
        self.base_dir = base_dir
        self.command = (
            command if command is not None else ["stdbuf", "-oL", "candump", "-L", interface]
        )
        self._rotate_interval_s = rotate_interval_s
        self._fsync_interval_s = fsync_interval_s
        self._backoff_start_s = backoff_start_s
        self._backoff_max_s = backoff_max_s
        self._stop = threading.Event()
        self._child_lock = threading.Lock()
        self._child: subprocess.Popen[bytes] | None = None

    def stop(self) -> None:
        """Ask the relay loop to finish: terminate candump, then run() returns."""
        self._stop.set()
        with self._child_lock:
            child = self._child
        if child is not None and child.poll() is None:
            child.terminate()

    def run(self) -> int:
        """Relay until stopped. Returns 0 on a clean stop, 1 on capture failure."""
        writer = RawLogWriter(
            self.base_dir,
            self.interface,
            manifest={"format": "candump-log", "command": shlex.join(self.command)},
            rotate_interval_s=self._rotate_interval_s,
            fsync_interval_s=self._fsync_interval_s,
        )
        logger.info("canlog %s: capturing to %s", self.interface, writer.run_dir)
        backoff = self._backoff_start_s
        try:
            while not self._stop.is_set():
                relayed = self._relay_one_child(writer)
                if self._stop.is_set():
                    break
                if relayed:
                    backoff = self._backoff_start_s
                logger.warning(
                    "canlog %s: %s exited, retrying in %.1fs",
                    self.interface,
                    self.command[0],
                    backoff,
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self._backoff_max_s)
        except OSError:
            logger.exception("canlog %s: capture write failed, exiting", self.interface)
            self.stop()
            return 1
        finally:
            writer.close()
        return 0

    def _relay_one_child(self, writer: RawLogWriter) -> bool:
        """Run one candump; relay lines until it exits. True if any line arrived."""
        try:
            child = subprocess.Popen(self.command, stdout=subprocess.PIPE)
        except OSError as exc:
            logger.error("canlog %s: cannot spawn %r: %s", self.interface, self.command, exc)
            return False
        with self._child_lock:
            self._child = child
        relayed = False
        try:
            if self._stop.is_set():
                child.terminate()
            assert child.stdout is not None
            for line in child.stdout:
                writer.write_line(line)
                relayed = True
        finally:
            with self._child_lock:
                self._child = None
            try:
                child.wait(timeout=_CHILD_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return relayed


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Capture raw candump output to segmented files")
    parser.add_argument("interface", help="CAN interface to capture, e.g. can0")
    parser.add_argument(
        "--dir",
        default=os.environ.get("OPENLAPS_RAW_CAPTURE_DIR") or None,
        help="capture base directory (default: $OPENLAPS_RAW_CAPTURE_DIR)",
    )
    parser.add_argument(
        "--rotate-s",
        type=float,
        default=DEFAULT_ROTATE_INTERVAL_S,
        help="segment rotation interval in seconds",
    )
    parser.add_argument(
        "--fsync-s",
        type=float,
        default=DEFAULT_FSYNC_INTERVAL_S,
        help="flush+fsync interval in seconds",
    )
    parser.add_argument(
        "--command",
        default=None,
        help="override the capture command (a shell-quoted string; stdout is captured)",
    )
    args = parser.parse_args(argv)
    if not args.dir:
        parser.error("no capture directory: pass --dir or set OPENLAPS_RAW_CAPTURE_DIR")

    service = CanLogService(
        args.interface,
        Path(args.dir),
        command=shlex.split(args.command) if args.command else None,
        rotate_interval_s=args.rotate_s,
        fsync_interval_s=args.fsync_s,
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: service.stop())
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
