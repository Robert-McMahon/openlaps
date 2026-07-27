"""Pure per-channel report-by-exception filtering."""

from dataclasses import dataclass

from core.config import RbeConfig
from core.samples import Sample, SampleValue


def _deadband_value(value: SampleValue) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadband requires numeric sample values")
    return value


@dataclass(frozen=True, slots=True)
class _SentState:
    value: float | int | bool | str
    t_mono_ns: int


class RbeFilter:
    """Apply catalog RBE policies while retaining only last-sent state."""

    def __init__(self) -> None:
        self._state: dict[int, _SentState] = {}
        self.suppressed_count = 0

    def accept(self, channel_id: int, sample: Sample, policy: RbeConfig | None) -> bool:
        """Return whether a mapped sample should proceed to batching."""
        if policy is None:
            return True

        previous = self._state.get(channel_id)
        if previous is None:
            if policy.deadband is not None:
                _deadband_value(sample.value)
            self._record(channel_id, sample)
            return True

        elapsed_ns = sample.t_mono_ns - previous.t_mono_ns
        if policy.max_interval_ns is not None and elapsed_ns >= policy.max_interval_ns:
            self._record(channel_id, sample)
            return True
        if policy.min_interval_ns is not None and elapsed_ns < policy.min_interval_ns:
            self.suppressed_count += 1
            return False

        if policy.deadband is not None:
            deadband_value = _deadband_value(sample.value)
            previous_value = _deadband_value(previous.value)
            if abs(deadband_value - previous_value) <= policy.deadband:
                self.suppressed_count += 1
                return False

        self._record(channel_id, sample)
        return True

    def _record(self, channel_id: int, sample: Sample) -> None:
        self._state[channel_id] = _SentState(sample.value, sample.t_mono_ns)
