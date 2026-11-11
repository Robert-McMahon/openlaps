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
    # Red-flag periods, ``{"start_ms", "end_ms"}`` in time order; only the
    # last may be open (``end_ms`` None). The race clock keeps running
    # through one while the driver's clock does not.
    reds: list[dict[str, int | None]] = field(default_factory=list)
    # What the crew knows was in the tank at a moment, ``{"at_ms",
    # "level_l"}`` in time order: the strategy service re-bases its fuel
    # model on the latest one.
    fuel_marks: list[dict[str, int | float]] = field(default_factory=list)

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
        self.reds = []
        self.fuel_marks = []
        return self.payload(now)

    def change_driver(
        self,
        driver: str,
        now_ms: int | None = None,
        out_ms: int | None = None,
    ) -> dict[str, object]:
        """Close the current stint and open the next one for ``driver``.

        ``now_ms`` is when the new driver's stint starts. ``out_ms`` is when
        the outgoing driver's ended, if that was earlier: strictly a stint
        ends at the pit-entry line and the next begins at pit exit, and the
        stop between them belongs to neither driver. Absent, the two stints
        meet at ``now_ms``.
        """
        if self.status != "active":
            raise SessionError("no active session - start one first")
        driver = (driver or "").strip()
        if not driver:
            raise SessionError("driver is required")
        if driver == self.driver:
            raise SessionError(f"{driver!r} is already the active driver")
        now = _now_ms() if now_ms is None else now_ms
        out = now if out_ms is None else out_ms
        if out < self.stint_start_ms:
            raise SessionError("driver change cannot occur before the active stint started")
        if now < out:
            raise SessionError("the new driver cannot start before the old one got out")
        self.stints.append(
            {
                "driver": self.driver,
                "stint_number": self.stint_number,
                "start_ms": self.stint_start_ms,
                "end_ms": out,
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

    def amend_timeline(
        self,
        stints: list[dict[str, object]],
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Replace the session's whole stint list in one step.

        Times and driver changes get missed on the day, so afterwards the
        timeline is simply restated: one ``{"driver", "start_ms", "end_ms"}``
        per stint, in order. That one operation covers correcting a time,
        adding a driver change nobody recorded, removing one that never
        happened and fixing who was driving. Works on an active session or
        an ended one.

        The first start is the session start. A stint may not start before
        the one before it ended, but it may start after: the gap is the pit
        stop. Every stint needs an end except the last one of an active
        session, which must not have one — it is the driver in the car.
        """
        if self.status not in {"active", "ended"}:
            raise SessionError("no session to amend - start one first")
        if not stints:
            raise SessionError("a session needs at least one stint")
        active = self.status == "active"
        rebuilt: list[dict[str, object]] = []
        previous_end = 0
        for number, item in enumerate(stints, start=1):
            driver = item.get("driver")
            start = item.get("start_ms")
            end = item.get("end_ms")
            last = number == len(stints)
            if not isinstance(driver, str) or not driver.strip():
                raise SessionError(f"stint {number} needs a driver")
            if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                raise SessionError(f"stint {number} needs a start time")
            if start < previous_end:
                raise SessionError(f"stint {number} cannot start before stint {number - 1} ended")
            if last and active:
                if end is not None:
                    raise SessionError(
                        "the session is still active - its last stint has no end until it ends"
                    )
            elif isinstance(end, bool) or not isinstance(end, int):
                raise SessionError(f"stint {number} needs an end time")
            elif end < start:
                raise SessionError(f"stint {number} cannot end before it started")
            rebuilt.append(
                {
                    "driver": driver.strip(),
                    "stint_number": number,
                    "start_ms": start,
                    "end_ms": end,
                }
            )
            previous_end = end if isinstance(end, int) else start
        current = rebuilt[-1]
        self.stints = rebuilt[:-1] if active else rebuilt
        self.driver = str(current["driver"])
        self.stint_number = len(rebuilt)
        self.session_start_ms = _int(rebuilt[0]["start_ms"])
        self.stint_start_ms = _int(current["start_ms"])
        return self.payload(now_ms)

    def reopen(self, now_ms: int | None = None) -> dict[str, object]:
        """Undo an end: the session is active again and its last stint open.

        Ending a 36-hour race by mistake at hour twenty must not cost the
        session. Nothing is lost by the round trip: the same session id, the
        same stints, the last one simply has no end again.
        """
        if self.status != "ended":
            raise SessionError("only an ended session can be reopened")
        last = self.stints.pop()
        self.driver = str(last["driver"])
        self.stint_number = _int(last["stint_number"])
        self.stint_start_ms = _int(last["start_ms"])
        self.status = "active"
        return self.payload(now_ms)

    def red_flag(self, now_ms: int | None = None) -> dict[str, object]:
        """The race is stopped: open a red-flag period."""
        if self.status != "active":
            raise SessionError("no active session - start one first")
        now = _now_ms() if now_ms is None else now_ms
        if self.reds and self.reds[-1]["end_ms"] is None:
            raise SessionError("the race is already under a red flag")
        latest = max(
            [self.session_start_ms, *(_int(red["end_ms"]) for red in self.reds)],
        )
        if now < latest:
            raise SessionError("a red flag cannot start before the session or the last red flag")
        self.reds.append({"start_ms": now, "end_ms": None})
        return self.payload(now_ms)

    def green_flag(self, now_ms: int | None = None) -> dict[str, object]:
        """The race is under way again: close the open red-flag period."""
        if self.status != "active":
            raise SessionError("no active session - start one first")
        now = _now_ms() if now_ms is None else now_ms
        if not self.reds or self.reds[-1]["end_ms"] is not None:
            raise SessionError("the race is not under a red flag")
        if now < _int(self.reds[-1]["start_ms"]):
            raise SessionError("the race cannot go green before the red flag came out")
        self.reds[-1]["end_ms"] = now
        return self.payload(now_ms)

    def amend_reds(
        self, reds: list[dict[str, object]], now_ms: int | None = None
    ) -> dict[str, object]:
        """Replace the red-flag periods, e.g. to add one nobody recorded."""
        if self.status not in {"active", "ended"}:
            raise SessionError("no session to amend - start one first")
        rebuilt: list[dict[str, int | None]] = []
        previous_end = 0
        for number, item in enumerate(reds, start=1):
            start = item.get("start_ms")
            end = item.get("end_ms")
            if isinstance(start, bool) or not isinstance(start, int):
                raise SessionError(f"red flag {number} needs a start time")
            if start < previous_end:
                raise SessionError(f"red flag {number} cannot start before the one before it ended")
            if end is None:
                if number != len(reds) or self.status != "active":
                    raise SessionError(f"red flag {number} needs an end time")
            elif isinstance(end, bool) or not isinstance(end, int) or end < start:
                raise SessionError(f"red flag {number} cannot end before it started")
            rebuilt.append({"start_ms": start, "end_ms": end})
            previous_end = end if isinstance(end, int) else start
        self.reds = rebuilt
        return self.payload(now_ms)

    def mark_fuel(self, level_l: float, now_ms: int | None = None) -> dict[str, object]:
        """Record what the crew knows is in the tank as of ``now_ms``."""
        if self.status != "active":
            raise SessionError("no active session - start one first")
        now = _now_ms() if now_ms is None else now_ms
        marks: list[dict[str, object]] = [dict(mark) for mark in self.fuel_marks]
        marks.append({"at_ms": now, "level_l": level_l})
        return self.amend_fuel_marks(marks, now_ms)

    def amend_fuel_marks(
        self, marks: list[dict[str, object]], now_ms: int | None = None
    ) -> dict[str, object]:
        """Replace the fuel marks; they are kept in time order."""
        if self.status not in {"active", "ended"}:
            raise SessionError("no session to amend - start one first")
        rebuilt: list[dict[str, int | float]] = []
        for number, item in enumerate(marks, start=1):
            at = item.get("at_ms")
            level = item.get("level_l")
            if isinstance(at, bool) or not isinstance(at, int) or at < 0:
                raise SessionError(f"fuel mark {number} needs a time")
            if (
                isinstance(level, bool)
                or not isinstance(level, int | float)
                or not 0 <= level <= _MAX_FUEL_L
            ):
                raise SessionError(f"fuel mark {number} needs a level in litres")
            rebuilt.append({"at_ms": at, "level_l": float(level)})
        self.fuel_marks = sorted(rebuilt, key=lambda mark: mark["at_ms"])
        return self.payload(now_ms)

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
        if self.reds and self.reds[-1]["end_ms"] is None:
            # A race that ends under a red flag: the period ends with it.
            self.reds[-1]["end_ms"] = max(now, _int(self.reds[-1]["start_ms"]))
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
        state["reds"] = [dict(red) for red in self.reds]
        state["fuel_marks"] = [dict(mark) for mark in self.fuel_marks]
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

        restored = cls(
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
        # Both lists are optional so a state file or a queued snapshot
        # written before they existed still restores, and they go through
        # the same checks as an operator's amendment. A list that fails them
        # is dropped rather than refused: these annotate the session, and a
        # bad annotation must never cost the session itself on a restart.
        try:
            restored.amend_reds(_object_list(data, "reds"))
        except (SessionError, TypeError):
            restored.reds = []
        try:
            restored.amend_fuel_marks(_object_list(data, "fuel_marks"))
        except (SessionError, TypeError):
            restored.fuel_marks = []
        return restored


_MAX_FUEL_L = 1000.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer")
    return value


def _object_list(data: dict[str, object], key: str) -> list[dict[str, object]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{key} must be a list of objects")
    return [dict(item) for item in value]


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
