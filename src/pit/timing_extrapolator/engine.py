"""Pure state machine for pit-side lap and sector elapsed clocks."""

from __future__ import annotations

import json
from dataclasses import dataclass

LAP_CHANNEL = "timing.lap_elapsed_pit"
SECTOR_CHANNEL = "timing.sector_elapsed_pit"
# One channel per sector, so the wall can list them side by side: the sector
# the car is in runs, the others hold their last completed time. Sectors past
# the cap still drive the single running clock above; they just get no
# channel of their own.
MAX_SECTOR_CHANNELS = 6


def sector_channel(sector: int) -> str:
    return f"timing.sector_{sector}_pit"


OUTPUT_CHANNELS: tuple[str, ...] = (
    LAP_CHANNEL,
    SECTOR_CHANNEL,
    *(sector_channel(n) for n in range(1, MAX_SECTOR_CHANNELS + 1)),
)


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
        # The sector the car is in, and each sector's last completed time. A
        # sector keeps its time until the car is in it again.
        self._running_sector: int | None = None
        self._sector_splits: dict[int, float] = {}
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
                sector = int(event.get("sector", 0))
                self._sector_started_at = crossed_at
                if sector == 1 and self._lap_started_at is None:
                    self._lap_started_at = crossed_at - split
                output = {SECTOR_CHANNEL: ClockValue(split, "authoritative")}
                if sector >= 1:
                    # The last sector's completion is followed at once by the
                    # lap completion, which puts the car back in sector 1.
                    self._running_sector = sector + 1
                    if sector <= MAX_SECTOR_CHANNELS:
                        self._sector_splits[sector] = split
                        output[sector_channel(sector)] = ClockValue(split, "authoritative")
                return output
            if event_type == "lap_completed":
                self._lap_started_at = crossed_at
                self._sector_started_at = crossed_at
                self._running_sector = 1
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
        # Per-sector channels: the running sector follows the sector clock,
        # every other known sector repeats its held time so a live panel
        # keeps showing it, and one not yet timed says so.
        running = self._running_sector
        if running is not None and SECTOR_CHANNEL in output:
            known = max([running, *self._sector_splits])
            for sector in range(1, min(known, MAX_SECTOR_CHANNELS) + 1):
                if sector == running:
                    value = output[SECTOR_CHANNEL]
                elif sector in self._sector_splits:
                    value = ClockValue(self._sector_splits[sector], "authoritative")
                else:
                    value = ClockValue(None, "pending", "not_timed")
                output[sector_channel(sector)] = value
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
