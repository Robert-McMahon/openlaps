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

    def end_session(self, now_ms: int | None = None) -> dict[str, object]:
        """Close the current stint and mark the session ended."""
        if self.status != "active":
            raise SessionError("no active session to end")
        now = _now_ms() if now_ms is None else now_ms
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
        """Restore state written by ``to_dict``."""
        stints = data.get("stints", [])
        if not isinstance(stints, list):
            raise TypeError("stints must be a list")
        return cls(
            session_id=str(data.get("session_id", "")),
            session_type=str(data.get("session_type", "")),
            driver=str(data.get("driver", "")),
            track_name=str(data.get("track_name", "")),
            car=str(data.get("car", "")),
            stint_number=int(data.get("stint_number", 0)),
            session_start_ms=int(data.get("session_start", 0)),
            stint_start_ms=int(data.get("stint_start", 0)),
            status=str(data.get("status", "none")),
            stints=[dict(item) for item in stints if isinstance(item, dict)],
        )


def _now_ms() -> int:
    return int(time.time() * 1000)
