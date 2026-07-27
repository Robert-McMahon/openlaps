"""Lean sample object shared by telemetry collectors and the pipeline."""

from dataclasses import dataclass

SampleValue = float | int | bool | str


@dataclass(slots=True, frozen=True)
class Sample:
    """A source-native telemetry value stamped at capture time."""

    source_ref: str
    t_mono_ns: int
    t_wall_ms: float
    value: SampleValue
