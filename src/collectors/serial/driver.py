"""The contract a serial receiver driver satisfies, and nothing else.

A leaf module on purpose: `um980.py` implements this, `drivers.py` registers
implementations, and `transport.py` consumes the registry. Putting the
Protocol here rather than beside either of them is what keeps that a line
instead of a cycle.

The transport owns the port, the reconnect loop and the decode path -- none
of which are receiver-specific. A driver owns exactly the two things that
are: what to say to the receiver at startup, and how to hand it corrections.
"""

from __future__ import annotations

from typing import Protocol


class SerialPort(Protocol):
    """The subset of pyserial the transport and its drivers actually use."""

    def reset_input_buffer(self) -> None: ...

    def write(self, data: bytes, /) -> int | None: ...

    def readline(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


class DriverConfigurationError(RuntimeError):
    """A receiver rejected or failed to acknowledge its startup configuration.

    The transport catches this and retries with backoff, so it must stay the
    common supertype for every driver's own configuration failure -- a driver
    raising something else takes the collector thread down with it.
    """


class SerialDriver(Protocol):
    """Receiver-specific startup configuration and correction write-back."""

    def configure(self) -> None:
        """Apply startup settings, raising `DriverConfigurationError` on refusal."""
        ...

    def write_rtcm(self, payload: bytes) -> int:
        """Write one correction payload unchanged; returns bytes written."""
        ...
