"""Pure session and driver-stint state machine.

This is the predecessor's side-effect-free state machine, ported for the pit
session-control service. ``payload()`` is the normative
``cmd.<vehicle>.session`` wire schema pinned in ``docs/WIRE_FORMAT.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

SESSION_TYPES = ("practice", "qualifying", "race", "test")


class SessionError(ValueError):
    """Invalid session transition or input."""


@dataclass
class SessionState:
    """Current session plus the closed-stint history needed for persistence."""

    session_id: str = ""
    session_type: str = ""
    driver: str = ""
    track_name: str = ""
    car: str = ""
    stint_number: int = 0
    session_start_ms: int = 0
    stint_start_ms: int = 0
    status: str = "none"
    stints: list[dict[str, object]] = field(default_factory=list)

    def start_session(
        self,
        session_type: str,
        driver: str,
        track_name: str = "",
        car: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Open a session and its first stint."""
        if self.status == "active":
            raise SessionError(f"session {self.session_id!r} is already active - end it first")
        session_type = (session_type or "").strip().lower()
        driver = (driver or "").strip()
        if session_type not in SESSION_TYPES:
            raise SessionError(f"session_type must be one of {SESSION_TYPES}, got {session_type!r}")
        if not driver:
            raise SessionError("driver is required")
        now = _now_ms() if now_ms is None else now_ms
        stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(now / 1000))
        self.session_id = f"{stamp}-{uuid4().hex}-{session_type}"
        self.session_type = session_type
        self.driver = driver
        self.track_name = (track_name or "").strip()
        self.car = (car or "").strip()
        self.stint_number = 1
        self.session_start_ms = now
        self.stint_start_ms = now
        self.status = "active"
        self.stints = []
        return self.payload(now)

    def change_driver(self, driver: str, now_ms: int | None = None) -> dict[str, object]:
        """Close the current stint and open the next one for ``driver``."""
        if self.status != "active":
            raise SessionError("no active session - start one first")
        driver = (driver or "").strip()
        if not driver:
            raise SessionError("driver is required")
        if driver == self.driver:
            raise SessionError(f"{driver!r} is already the active driver")
        now = _now_ms() if now_ms is None else now_ms
        if now < self.stint_start_ms:
            raise SessionError("driver change cannot occur before the active stint started")
        self.stints.append(
            {
                "driver": self.driver,
                "stint_number": self.stint_number,
                "start_ms": self.stint_start_ms,
                "end_ms": now,
            }
        )
        self.driver = driver
        self.stint_number += 1
        self.stint_start_ms = now
        return self.payload(now)

    def amend_stint_start(self, at_ms: int) -> dict[str, object]:
        """Correct when the active stint began; the previous stint's end moves with it.

        The boundary just recorded was wrong — the operator pressed the
        button and only then worked out the real time. The correction can
        move either direction, bounded by the previous stint's start (a
        zero-length previous stint is allowed, matching ``change_driver``'s
        own bound). The first stint cannot be moved: it starts with the
        session.
        """
        if self.status != "active":
            raise SessionError("no active session - start one first")
        if self.stint_number < 2:
            raise SessionError("the first stint starts with the session and cannot be moved")
        previous = self.stints[-1]
        previous_start = previous["start_ms"]
        if not isinstance(previous_start, int) or at_ms < previous_start:
            raise SessionError("stint start cannot move before the previous stint started")
        previous["end_ms"] = at_ms
        self.stint_start_ms = at_ms
        return self.payload(at_ms)

    def end_session(self, now_ms: int | None = None) -> dict[str, object]:
        """Close the current stint and mark the session ended."""
        if self.status != "active":
            raise SessionError("no active session to end")
        now = _now_ms() if now_ms is None else now_ms
        if now < self.stint_start_ms:
            raise SessionError("session end cannot occur before the active stint started")
        self.stints.append(
            {
                "driver": self.driver,
                "stint_number": self.stint_number,
                "start_ms": self.stint_start_ms,
                "end_ms": now,
            }
        )
        self.status = "ended"
        return self.payload(now)

    def payload(self, now_ms: int | None = None) -> dict[str, object]:
        """Return the pinned ``cmd.<vehicle>.session`` payload."""
        return {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "driver": self.driver,
            "track_name": self.track_name,
            "car": self.car,
            "stint_number": self.stint_number,
            "session_start": self.session_start_ms,
            "stint_start": self.stint_start_ms,
            "status": self.status,
            "timestamp": _now_ms() if now_ms is None else now_ms,
        }

    def to_dict(self) -> dict[str, object]:
        """Serialise full state, including closed stints, for restart continuity."""
        state = self.payload()
        state["stints"] = [dict(stint) for stint in self.stints]
        return state

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionState:
        """Restore and strictly validate state written by ``to_dict``."""
        status_value = data.get("status", "none")
        if not isinstance(status_value, str):
            raise TypeError("status must be a string")
        status = status_value.strip().lower()
        if status == "none":
            return cls()
        if status not in {"active", "ended"}:
            raise ValueError(f"invalid session status {status!r}")

        session_type = _required_string(data, "session_type").lower()
        if session_type not in SESSION_TYPES:
            raise ValueError(f"session_type must be one of {SESSION_TYPES}")
        session_start = _required_integer(data, "session_start")
        stint_start = _required_integer(data, "stint_start")
        stint_number = _required_integer(data, "stint_number")
        if session_start < 0 or stint_start < session_start or stint_number < 1:
            raise ValueError("invalid session/stint start or stint number")

        stints = data.get("stints", [])
        if not isinstance(stints, list):
            raise TypeError("stints must be a list")
        closed: list[dict[str, object]] = []
        previous_end = session_start
        for expected_number, item in enumerate(stints, start=1):
            if not isinstance(item, dict):
                raise TypeError("each stint must be an object")
            number = _required_integer(item, "stint_number")
            start = _required_integer(item, "start_ms")
            end = _required_integer(item, "end_ms")
            if number != expected_number or start < previous_end or end < start:
                raise ValueError("invalid stint ordering or timestamps")
            closed.append(
                {
                    "driver": _required_string(item, "driver"),
                    "stint_number": number,
                    "start_ms": start,
                    "end_ms": end,
                }
            )
            previous_end = end

        expected_closed = stint_number if status == "ended" else stint_number - 1
        if len(closed) != expected_closed:
            raise ValueError("stint history does not match current stint number")
        driver = _required_string(data, "driver")
        if status == "active" and stint_start < previous_end:
            raise ValueError("active stint starts before the previous stint ended")
        if status == "ended":
            last = closed[-1]
            if driver != last["driver"] or stint_start != last["start_ms"]:
                raise ValueError("ended state does not match its final stint")

        return cls(
            session_id=_required_string(data, "session_id"),
            session_type=session_type,
            driver=driver,
            track_name=_optional_string(data, "track_name"),
            car=_optional_string(data, "car"),
            stint_number=stint_number,
            session_start_ms=session_start,
            stint_start_ms=stint_start,
            status=status,
            stints=closed,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(data: dict[str, object], key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value.strip()


def _required_integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value
