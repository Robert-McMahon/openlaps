"""Minimal deterministic fault injection shared by replay acceptance tests."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScaleFault:
    channel: str = "car.oil_pressure"
    after_s: float = 180
    factor: float = 0.7

    def apply(self, channel: str, value: object, elapsed_s: float) -> object:
        if (
            channel == self.channel
            and elapsed_s >= self.after_s
            and isinstance(value, (int, float))
        ):
            return value * self.factor
        return value
