"""Pure state machine for pit-side lap and sector elapsed clocks."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClockPolicy:
    fallback_max_lap_s: float = 300.0
    max_clock_offset_s: float = 0.1
    max_clock_stratum: int = 4
    allowed_clock_sources: tuple[str, ...] = ("GPS", "PPS", "NTP")
    best_lap_multiple: float = 3.0


@dataclass(frozen=True, slots=True)
class ClockValue:
    value: float | None
    status: str
    reason: str | None = None


class TimingExtrapolator:
    def __init__(self, policy: ClockPolicy) -> None:
        self.policy = policy
        self._lap_started_at: float | None = None
        self._sector_started_at: float | None = None
        self._best_lap_s: float | None = None
        self._clock: dict[str, object] = {}

    def observe(self, channel: str, value: object) -> dict[str, ClockValue]:
        if channel.startswith("sys.host.clock_"):
            self._clock[channel] = value
            return {}
        if channel == "lap.best_time":
            best = float(value)  # registry typing guarantees a number; reject non-positive values
            if best > 0:
                self._best_lap_s = best
            return {}
        if channel == "lap.event":
            event = json.loads(str(value))
            event_type = event.get("type")
            crossed_at = float(event["time"])
            if event_type == "sector_completed":
                split = float(event["split_time"])
                self._sector_started_at = crossed_at
                if int(event.get("sector", 0)) == 1 and self._lap_started_at is None:
                    self._lap_started_at = crossed_at - split
                return {"timing.sector_elapsed_pit": ClockValue(split, "authoritative")}
            if event_type == "lap_completed":
                self._lap_started_at = crossed_at
                self._sector_started_at = crossed_at
                return {
                    "timing.lap_elapsed_pit": ClockValue(float(event["lap_time"]), "authoritative")
                }
        return {}

    def tick(self, now: float) -> dict[str, ClockValue]:
        starts = {
            "timing.lap_elapsed_pit": self._lap_started_at,
            "timing.sector_elapsed_pit": self._sector_started_at,
        }
        reason = self._clock_gate_reason()
        output: dict[str, ClockValue] = {}
        for channel, started_at in starts.items():
            if started_at is None:
                continue
            if reason is not None:
                output[channel] = ClockValue(None, "gated", reason)
            else:
                elapsed = max(0.0, now - started_at)
                bound = self._runaway_bound()
                if elapsed > bound:
                    output[channel] = ClockValue(bound, "degraded", "runaway")
                else:
                    output[channel] = ClockValue(elapsed, "extrapolating")
        return output

    def _runaway_bound(self) -> float:
        if self._best_lap_s is None:
            return self.policy.fallback_max_lap_s
        return min(
            self.policy.fallback_max_lap_s,
            self._best_lap_s * self.policy.best_lap_multiple,
        )

    def _clock_gate_reason(self) -> str | None:
        required = {
            "sys.host.clock_offset_s",
            "sys.host.clock_stratum",
            "sys.host.clock_source",
        }
        if not required <= self._clock.keys():
            return "clock_health_missing"
        try:
            if abs(float(self._clock["sys.host.clock_offset_s"])) > self.policy.max_clock_offset_s:
                return "clock_offset"
            if int(self._clock["sys.host.clock_stratum"]) > self.policy.max_clock_stratum:
                return "clock_stratum"
        except (TypeError, ValueError, OverflowError):
            return "clock_health_invalid"
        source = str(self._clock["sys.host.clock_source"]).upper()
        if source not in self.policy.allowed_clock_sources:
            return "clock_source"
        return None
