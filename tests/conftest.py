"""Shared fixtures: synthetic Wanneroo lap paths, NMEA encoding, docker NATS."""

from __future__ import annotations

import base64
import json
import math
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from timing.tracks import TrackDefinition, load_track

EXAMPLE_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer"
WANNEROO_KML = EXAMPLE_PROFILE / "tracks" / "Wanneroo.kml"

_M_PER_DEG_LAT = 111_320.0
_NATS_IMAGE = "nats:2.12-alpine"
_TIMESCALE_IMAGE = "timescale/timescaledb:latest-pg17"
_MOSQUITTO_IMAGE = "eclipse-mosquitto:2"
_LAP_POINT_ORDER = ("StartFinish", "Sector1", "Sector2")

# Size of the `tight_store_nats_url` file store. A stream asking for more than
# half of it cannot be reserved twice over.
TIGHT_STORE_BYTES = 64 * 1024 * 1024

# Size of the leafnode pair's JetStream file stores. Left unset, nats-server
# sizes `max_file_store` from the free space of whatever filesystem holds the
# store, so whether a stream reservation succeeds depends on the machine the
# tests happen to run on: the pit's 32 GiB stream is nothing on a workstation
# and fails with "insufficient storage resources available" (10047) on a CI
# runner with less than that free. A fixed tmpfs makes the limit the same
# number everywhere. These tests move a few hundred small messages, so the
# cap is three orders of magnitude clear of what they need.
LEAF_STORE_BYTES = 256 * 1024 * 1024

# Same reasoning for the single-server fixture below.
SERVER_STORE_BYTES = 512 * 1024 * 1024

# TELE's real 8 GiB reservation (src/agent/publisher.py) is sized for three
# days of a running car. Reserving it in a test makes that test depend on the
# free disk of whichever machine runs it, and the agent publisher retries the
# rejection internally -- so the failure surfaces as "timed out waiting for
# publisher connect", saying nothing about storage. Every test publisher asks
# for this instead; nothing in the suite writes more than a few MiB.
TEST_TELE_MAX_BYTES = 64 * 1024 * 1024


def crossing_pair(line, offset_m: float = 20.0) -> tuple[tuple[float, float], tuple[float, float]]:
    """Two points straddling a timing line, ``offset_m`` either side of its middle."""
    mid_lat = (line.start.lat + line.end.lat) / 2
    mid_lon = (line.start.lon + line.end.lon) / 2
    cos_lat = math.cos(math.radians(mid_lat))
    along_x = (line.end.lon - line.start.lon) * _M_PER_DEG_LAT * cos_lat
    along_y = (line.end.lat - line.start.lat) * _M_PER_DEG_LAT
    norm = math.hypot(along_x, along_y)
    perp_x, perp_y = -along_y / norm, along_x / norm
    dlat = perp_y * offset_m / _M_PER_DEG_LAT
    dlon = perp_x * offset_m / (_M_PER_DEG_LAT * cos_lat)
    return (mid_lat - dlat, mid_lon - dlon), (mid_lat + dlat, mid_lon + dlon)


def wanneroo_track() -> TrackDefinition:
    """The example profile's track fixture."""
    return load_track(WANNEROO_KML)


def lap_path(
    track: TrackDefinition,
    laps: int = 1,
    *,
    t0_s: float = 0.0,
    leg_s: float = 30.0,
    points_per_leg: int = 8,
) -> list[tuple[float, float, float]]:
    """Synthesize ``(t_s, lat, lon)`` fixes that lap the track's timing lines.

    Crossing order is StartFinish -> Sector1 -> Sector2 -> StartFinish with
    ``leg_s`` seconds per leg, always crossing each line in the same
    direction so the engine's direction gate stays satisfied. Verified to
    produce only clean, valid laps against the Wanneroo fixture.
    """
    lines = {line.name: line for line in track.lines}
    pairs = {name: crossing_pair(lines[name]) for name in _LAP_POINT_ORDER}
    points: list[tuple[float, float, float]] = []
    t = t0_s
    for lap in range(laps + 1):
        for index, name in enumerate(_LAP_POINT_ORDER):
            if lap == laps and index > 0:
                break
            before, after = pairs[name]
            if points:
                previous = points[-1]
                arrive = t + leg_s - 1.0
                for step in range(1, points_per_leg):
                    fraction = step / points_per_leg
                    points.append(
                        (
                            previous[0] + fraction * (arrive - previous[0]),
                            previous[1] + fraction * (before[0] - previous[1]),
                            previous[2] + fraction * (before[1] - previous[2]),
                        )
                    )
                t += leg_s
            points.append((t, before[0], before[1]))
            points.append((t + 1.0, after[0], after[1]))
            t += 1.0
        if lap == laps:
            break
    return points


def rmc_sentence(lat: float, lon: float, *, speed_kn: float = 60.0, heading: float = 90.0) -> bytes:
    """Encode one valid ``$GPRMC`` sentence for a fix."""
    ns, alat = ("N", lat) if lat >= 0 else ("S", -lat)
    ew, alon = ("E", lon) if lon >= 0 else ("W", -lon)
    lat_field = f"{int(alat):02d}{(alat - int(alat)) * 60:07.4f}"
    lon_field = f"{int(alon):03d}{(alon - int(alon)) * 60:07.4f}"
    body = (
        f"GPRMC,000000.00,A,{lat_field},{ns},{lon_field},{ew},"
        f"{speed_kn:.2f},{heading:.1f},010126,,,A"
    )
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _nats_server(*docker_args: str, server_args: tuple[str, ...] = ("-js",)):
    """A fresh throwaway nats-server in docker, yielding its client URL."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    port = _free_port()
    name = f"openlaps-test-nats-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:4222",
            *docker_args,
            _NATS_IMAGE,
            *server_args,
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"cannot start nats container: {run.stderr.strip()[:200]}")
    try:
        deadline = time.monotonic() + 20.0
        ready = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
                    sock.settimeout(1.0)
                    if sock.recv(64).startswith(b"INFO"):
                        ready = True
                        break
            except OSError:
                pass
            time.sleep(0.1)
        if not ready:
            pytest.skip("nats container did not become ready")
        yield f"nats://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def nats_url():
    """A fresh throwaway JetStream-enabled nats-server in docker (skip-less-able).

    The store is a fixed-size tmpfs rather than the container's writable
    layer, so `max_file_store` -- and therefore which stream reservations fit
    -- is the same number on every machine instead of a function of the
    host's free disk. See SERVER_STORE_BYTES.
    """
    with _nats_server(
        "--tmpfs", f"/data:size={SERVER_STORE_BYTES}", server_args=("-js", "-sd", "/data")
    ) as url:
        yield url


@pytest.fixture
def tight_store_nats_url():
    """A JetStream whose file store is capped at ``TIGHT_STORE_BYTES``.

    Left unconfigured, ``max_file_store`` is sized from the free space on the
    store's filesystem, which on any development machine is far too large to
    exercise a stream reservation against its limit. A small tmpfs is the
    cheapest way to get a server that has to say no.
    """
    with _nats_server(
        "--tmpfs", f"/data:size={TIGHT_STORE_BYTES}", server_args=("-js", "-sd", "/data")
    ) as url:
        yield url


@pytest.fixture
def timescale_dsn():
    """A fresh throwaway TimescaleDB in docker, empty and unmigrated."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    port = _free_port()
    name = f"openlaps-test-timescale-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            "POSTGRES_PASSWORD=openlaps",
            "-e",
            "POSTGRES_DB=openlaps",
            _TIMESCALE_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"cannot start timescale container: {run.stderr.strip()[:200]}")
    dsn = f"postgresql://postgres:openlaps@127.0.0.1:{port}/openlaps"
    try:
        # The image restarts the server partway through first-time init, so a
        # completed connection — not an open socket — is the readiness signal.
        deadline = time.monotonic() + 60.0
        ready = False
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                ready = True
                break
            except psycopg.Error:
                time.sleep(0.25)
        if not ready:
            pytest.skip("timescale container did not become ready")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def mosquitto_url():
    """A fresh anonymous pit-local MQTT broker in docker."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    port = _free_port()
    name = f"openlaps-test-mosquitto-{uuid.uuid4().hex[:8]}"
    # Written inside the container rather than bind-mounted from tmp_path: a
    # confined docker (the snap package, for one) cannot see /tmp and
    # silently substitutes an empty directory for the file, leaving a broker
    # that never starts and a test that skips for the wrong reason.
    config = "listener 1883\nallow_anonymous true\npersistence false\n"
    encoded = base64.b64encode(config.encode()).decode()
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:1883",
            "--entrypoint",
            "sh",
            _MOSQUITTO_IMAGE,
            "-c",
            f"echo {encoded} | base64 -d > /mosquitto/config/mosquitto.conf "
            "&& exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"cannot start mosquitto container: {run.stderr.strip()[:200]}")
    try:
        deadline = time.monotonic() + 20.0
        ready = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    ready = True
                    break
            except OSError:
                time.sleep(0.1)
        if not ready:
            pytest.skip("mosquitto container did not become ready")
        yield f"mqtt://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# --- the leafnode pair (P3.6) ------------------------------------------------

_LEAF_ALIAS = "vehicle-leaf"

# Short pings on both servers. Disconnecting a docker network removes the
# interface but leaves the established TCP socket half-open, so a leafnode
# only notices the peer is gone when its own keepalive times out — two
# minutes twice over, on the defaults. The deployed configs leave those
# defaults alone; this is a test-harness concession to not spending five
# minutes per dropout assertion.
_PING_CONF = """
ping_interval: "1s"
ping_max: 2
"""

_VEHICLE_CONF = f"""
server_name: vehicle
listen: 0.0.0.0:4222
http: 0.0.0.0:8222
jetstream {{ domain: veh, store_dir: /data }}
leafnodes {{ listen: 0.0.0.0:7422 }}
{_PING_CONF}
"""

# The remote names the vehicle's alias on the leafnode-only network. When
# that network is disconnected the name stops resolving, which is exactly
# the dropout being simulated.
_PIT_CONF = f"""
server_name: pit
listen: 0.0.0.0:4222
http: 0.0.0.0:8222
jetstream {{ domain: pit, store_dir: /data }}
leafnodes {{ remotes: [ {{ url: "nats://{_LEAF_ALIAS}:7422" }} ] }}
{_PING_CONF}
"""


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _leafz(port: int) -> int:
    """Established leafnode connections, or -1 while the server is unreachable."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/leafz", timeout=1.0) as response:
            # `leafs` is the array of connections; `leafnodes` is its length.
            return int(json.load(response).get("leafnodes", 0))
    except (urllib.error.URLError, OSError, ValueError):
        return -1


def _await_leafz(port: int, expected: int, *, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _leafz(port) == expected:
            return True
        time.sleep(0.2)
    return False


@dataclass
class LeafnodePair:
    """Two JetStream servers in distinct domains, joined by a severable leafnode."""

    vehicle_url: str
    pit_url: str
    _vehicle: str
    _pit_monitor_port: int
    _leaf_network: str

    def sever(self, *, timeout_s: float = 30.0) -> None:
        """Cut the link by removing the vehicle from the leafnode-only network.

        Deliberately *not* by disconnecting the network carrying the test
        client's published ports: `docker network disconnect` takes the port
        mapping with it, and the assertions would then run against a client
        that lost its own connection rather than against a severed link.
        """
        _docker("network", "disconnect", self._leaf_network, self._vehicle)
        assert _await_leafz(self._pit_monitor_port, 0, timeout_s=timeout_s), (
            "pit still reports a leafnode connection after severing"
        )

    def restore(self, *, timeout_s: float = 60.0) -> None:
        """Reattach, alias and all — the remote URL resolves it by name."""
        _docker("network", "connect", "--alias", _LEAF_ALIAS, self._leaf_network, self._vehicle)
        assert _await_leafz(self._pit_monitor_port, 1, timeout_s=timeout_s), (
            "leafnode did not re-establish after reconnecting the network"
        )


@pytest.fixture
def leafnode_pair():
    """A vehicle/pit `nats-server` pair wired exactly as `deploy/nats/*.conf` wires them.

    Domains `veh`/`pit`, the pit dialling the vehicle, and two docker
    networks: one carrying only the leafnode, one carrying the published
    ports the test client uses. Severing touches only the former.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    suffix = uuid.uuid4().hex[:8]
    client_net = f"openlaps-test-client-{suffix}"
    leaf_net = f"openlaps-test-leaf-{suffix}"
    vehicle = f"openlaps-test-veh-{suffix}"
    pit = f"openlaps-test-pit-{suffix}"
    ports = {name: _free_port() for name in ("veh", "veh_mon", "pit", "pit_mon")}

    def start(name: str, config: str, client_port: int, monitor_port: int) -> bool:
        # The config is written *inside* the container rather than
        # bind-mounted from tmp_path. A confined docker (the snap package,
        # for one) cannot see /tmp at all and silently substitutes an empty
        # directory for the file, which surfaces as "is a directory" from a
        # server that never starts. Nothing here needs a host file.
        encoded = base64.b64encode(config.encode()).decode()
        run = _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            # The client network is attached first, so the published ports
            # are mapped through it and survive the leafnode disconnect.
            client_net,
            # A store of known size; see LEAF_STORE_BYTES. Severing the link
            # disconnects a network rather than restarting the container, so
            # nothing here loses its stream data mid-test.
            "--tmpfs",
            f"/data:size={LEAF_STORE_BYTES}",
            "-p",
            f"127.0.0.1:{client_port}:4222",
            "-p",
            f"127.0.0.1:{monitor_port}:8222",
            "--entrypoint",
            "sh",
            _NATS_IMAGE,
            "-c",
            f"echo {encoded} | base64 -d > /nats.conf && exec nats-server -c /nats.conf",
        )
        return run.returncode == 0

    def running(name: str) -> bool:
        return _docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() == "true"

    try:
        for network in (client_net, leaf_net):
            if _docker("network", "create", network).returncode != 0:
                pytest.skip(f"cannot create docker network {network}")
        if not start(vehicle, _VEHICLE_CONF, ports["veh"], ports["veh_mon"]):
            pytest.skip("cannot start vehicle nats container")
        if not _await_leafz(ports["veh_mon"], 0):
            pytest.skip(f"vehicle nats did not start: {_docker('logs', vehicle).stderr[-300:]}")
        if _docker("network", "connect", "--alias", _LEAF_ALIAS, leaf_net, vehicle).returncode:
            pytest.skip("cannot attach the vehicle to the leafnode network")
        if not start(pit, _PIT_CONF, ports["pit"], ports["pit_mon"]):
            pytest.skip("cannot start pit nats container")
        if not running(pit):
            pytest.skip(f"pit nats did not start: {_docker('logs', pit).stderr[-300:]}")
        if _docker("network", "connect", leaf_net, pit).returncode != 0:
            pytest.skip("cannot attach the pit to the leafnode network")
        if not _await_leafz(ports["pit_mon"], 1):
            pytest.skip("leafnode did not establish between the test servers")
        yield LeafnodePair(
            vehicle_url=f"nats://127.0.0.1:{ports['veh']}",
            pit_url=f"nats://127.0.0.1:{ports['pit']}",
            _vehicle=vehicle,
            _pit_monitor_port=ports["pit_mon"],
            _leaf_network=leaf_net,
        )
    finally:
        for name in (pit, vehicle):
            _docker("rm", "-f", name)
        for network in (leaf_net, client_net):
            _docker("network", "rm", network)
