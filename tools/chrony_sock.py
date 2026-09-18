"""chrony's SOCK refclock wire format, shared by every openlaps time source.

Two shims feed chronyd the same way and must agree byte for byte on how a
sample is built: `gnss_receiver_interface_shim.py` (an RP2040 that captured the PPS edge
itself) and `pps_gpio_shim.py` (a PPS wire straight onto a SoC GPIO). The
thing they have to agree on is not the struct layout -- that is easy to get
right -- but the **sign of the offset**, which is easy to get backwards and
whose failure mode is a clock disciplined confidently in the wrong direction.

So it lives once, here, with the tests that pin the sign in
`tests/test_gnss_receiver_interface_shim.py` and `tests/test_pps_gpio_shim.py`.

Both shims are installed side by side into the same directory
(`deploy/README.md`), which is what lets them import this.
"""

from __future__ import annotations

import socket
import struct

SOCK_MAGIC = 0x534F434B
# chrony's `struct sock_sample`: struct timeval, double offset, int
# pulse/leap/_pad, int magic. Native sizes and alignment on purpose -- this
# crosses a unix socket to a local process, not a network.
SOCK_SAMPLE = struct.Struct("@lldiiii")


def pack_sock_sample(utc_second: int, estimated_edge_realtime_ns: int) -> bytes:
    """Build chrony's native ``struct sock_sample`` as a full time sample.

    `utc_second` is the second the edge *belongs to*; the timeval is the
    system clock's own reading of when it arrived. The offset between them is
    the correction, and chrony's convention is that a **positive offset means
    the local clock is behind** -- so a clock running fast, which stamps a
    true second boundary at N.003, yields -0.003.
    """
    tv_sec, remainder_ns = divmod(estimated_edge_realtime_ns, 1_000_000_000)
    tv_usec = remainder_ns // 1000
    # Subtract as integers before converting: epoch-scale floats otherwise
    # lose enough precision to add ~0.1 us of avoidable noise.
    offset_s = (utc_second * 1_000_000_000 - estimated_edge_realtime_ns) / 1_000_000_000.0
    return SOCK_SAMPLE.pack(tv_sec, tv_usec, offset_s, 0, 0, 0, SOCK_MAGIC)


class ChronySocket:
    """Unix datagram sender for a socket owned by chronyd."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def send(self, sample: bytes) -> None:
        self._socket.sendto(sample, self.path)

    def close(self) -> None:
        self._socket.close()
