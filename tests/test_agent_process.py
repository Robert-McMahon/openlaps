"""VehicleAgent wiring and failure-mode tests (no hardware, no NATS server).

Covers the failure-mode table rows that don't need a broker: local NATS
down at startup (capture continues, agent does not exit), absent CAN
interface and serial device (collectors retry, agent keeps running with
the collectors it has), and collector-thread supervision.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
import serial
from conftest import EXAMPLE_PROFILE

from agent.agent import AgentSettings, VehicleAgent, agent_derived_channels
from agent.pipeline import DERIVED_SOURCE_CLASS
from core.pb import telemetry_pb2 as pb


def _unused_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"nats://127.0.0.1:{sock.getsockname()[1]}"


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        profile_dir=EXAMPLE_PROFILE,
        nats_url=_unused_port_url(),
        tick_ms=10,
        state_dir=tmp_path / "state",
        health_interval_s=0.05,
    )


def _failing_bus_factory(config):
    raise OSError(f"no such interface {config.interface}")


def _failing_serial_factory(config):
    raise serial.SerialException(f"no such port {config.port}")


def test_agent_survives_nats_down_and_absent_devices(tmp_path: Path):
    agent = VehicleAgent(
        _settings(tmp_path),
        bus_factory=_failing_bus_factory,
        serial_factory=_failing_serial_factory,
    )
    agent.start()
    try:
        time.sleep(0.6)
        # Collectors are alive and retrying, not dead; NATS is not connected;
        # nothing has crashed the process.
        for supervised in agent._supervised:
            assert supervised.collector.is_running()
        assert not agent.publisher.connected
        # Health samples flow through the pipeline into buffered batches.
        assert agent.pipeline.flush is not None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and agent.publisher.publish_lag_ms() == 0.0:
            time.sleep(0.05)
        assert agent.publisher.publish_lag_ms() > 0.0, "batches should be buffered, not lost"
    finally:
        started = time.monotonic()
        agent.stop()
        assert time.monotonic() - started < 15.0
    # Buffered-but-unpublishable batches were shed visibly at shutdown, not
    # silently: the agent still exited cleanly.
    status = json.loads(agent._status_payload("stopped"))
    assert status["nats_connected"] is False


def test_registry_includes_sys_agent_and_derived_channels(tmp_path: Path):
    agent = VehicleAgent(_settings(tmp_path), bus_factory=_failing_bus_factory)
    names = {channel.name for channel in agent.catalog.registry.channels}
    assert {"lap.event", "lap.number", "timing.delta_best", "timing.distance"} <= names
    assert {
        "sys.agent.status",
        "sys.agent.unmapped_refs",
        "sys.agent.rbe_suppressed",
        "sys.agent.encode_failures",
        "sys.agent.publish_drops",
        "sys.agent.publish_lag_ms",
        "sys.agent.clock_offset_ms",
        "sys.agent.clock_source",
        "sys.agent.drops.can0",
        "sys.agent.drops.serial0",
        "sys.agent.drops.host",
    } <= names
    # The timing app is wired per apps.lap_timing in the example catalog.
    assert agent.timing_app is not None
    assert agent.timing_app.track_name == "Wanneroo"


def test_health_samples_land_on_sys_agent_channels(tmp_path: Path):
    agent = VehicleAgent(_settings(tmp_path), bus_factory=_failing_bus_factory)
    agent._emit_health_samples()
    samples = {s.source_ref: s.value for s in agent._agent_queue.drain()}
    assert samples["derived:sys.agent.clock_source"] == "system"
    assert samples["derived:sys.agent.publish_drops"] == 0
    assert samples["derived:sys.agent.encode_failures"] == 0
    status = json.loads(samples["derived:sys.agent.status"])
    assert status["state"] == "running"
    assert set(status["collectors"]) == {"can0", "serial0", "host"}


def test_encode_failures_reach_the_wire_as_their_own_channel(tmp_path: Path):
    # A mis-typed catalog channel discards whole tick windows across every
    # source class; the counter has to be visible off-box, not just in logs.
    agent = VehicleAgent(_settings(tmp_path), bus_factory=_failing_bus_factory)
    agent.pipeline.encode_failures = 2
    agent._emit_health_samples()
    for sample in agent._agent_queue.drain():
        agent.pipeline.ingest(DERIVED_SOURCE_CLASS, sample)
    batches = agent.pipeline.flush(agent.clock)

    names = {channel.id: channel.name for channel in agent.catalog.registry.channels}
    wire = {}
    for batch in batches:
        decoded = pb.SampleBatch()
        decoded.ParseFromString(batch.payload)
        for sample in decoded.samples:
            wire[names[sample.channel_id]] = sample
    # The health samples themselves encoded cleanly (the counter did not move).
    assert agent.pipeline.encode_failures == 2
    assert wire["sys.agent.encode_failures"].i == 2


class _StubCollector:
    def __init__(self) -> None:
        self.starts = 0
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.starts += 1


def test_supervisor_restarts_dead_collectors_with_backoff(tmp_path: Path):
    agent = VehicleAgent(_settings(tmp_path), bus_factory=_failing_bus_factory)
    stub = _StubCollector()
    supervised = agent._supervised[0]
    supervised.collector = stub

    agent._supervise_collectors()
    assert stub.starts == 1 and supervised.restarts == 1
    # Immediately after, backoff blocks a second restart attempt.
    agent._supervise_collectors()
    assert stub.starts == 1
    # Once the backoff window elapses it tries again.
    supervised.next_restart_mono = 0.0
    agent._supervise_collectors()
    assert stub.starts == 2 and supervised.restarts == 2
    # A recovered collector resets the backoff and is left alone.
    stub.running = True
    agent._supervise_collectors()
    assert stub.starts == 2
    assert supervised.backoff_s == pytest.approx(1.0)


def test_settings_from_env_parses_the_documented_variables(tmp_path: Path):
    env = {
        "OPENLAPS_PROFILE": str(EXAMPLE_PROFILE),
        "OPENLAPS_NATS_URL": "nats://10.0.0.5:4222",
        "OPENLAPS_VEHICLE_ID": "override-car",
        "OPENLAPS_TICK_MS": "15",
        "OPENLAPS_QUEUE_SIZE": "500",
        "OPENLAPS_PUBLISH_WINDOW": "64",
        "OPENLAPS_PUBLISH_BUFFER_BYTES": "1048576",
        "OPENLAPS_TELE_MAX_AGE_H": "48",
        "OPENLAPS_TELE_MAX_BYTES": "1000000",
        "OPENLAPS_REGISTRY_REPUBLISH_S": "60",
        "OPENLAPS_STATE_DIR": str(tmp_path / "state"),
    }
    settings = AgentSettings.from_env(env)
    assert settings.profile_dir == EXAMPLE_PROFILE
    assert settings.nats_url == "nats://10.0.0.5:4222"
    assert settings.vehicle_id == "override-car"
    assert settings.tick_ms == 15
    assert settings.queue_maxlen == 500
    assert settings.publish_window == 64
    assert settings.publish_buffer_bytes == 1_048_576
    assert settings.tele_max_age_s == 48 * 3600
    assert settings.tele_max_bytes == 1_000_000
    assert settings.registry_interval_s == 60.0
    assert settings.state_dir == tmp_path / "state"


def test_settings_require_a_profile():
    with pytest.raises(ValueError, match="OPENLAPS_PROFILE"):
        AgentSettings.from_env({})


def test_derived_channel_set_covers_the_spec_table():
    names = [channel.name for channel in agent_derived_channels(["can0", "serial9"])]
    assert "sys.agent.drops.can0" in names
    assert "sys.agent.drops.serial9" in names
    assert names.count("sys.agent.status") == 1
    status = next(c for c in agent_derived_channels(["can0"]) if c.name == "sys.agent.status")
    assert status.value_type == pb.STRING


def test_settings_from_env_defaults_apply_when_variables_are_unset():
    """Regression: slots dataclass defaults must come from the dataclass, not cls attrs."""
    settings = AgentSettings.from_env({"OPENLAPS_PROFILE": str(EXAMPLE_PROFILE)})
    assert settings.nats_url == "nats://127.0.0.1:4222"
    assert settings.nats_creds is None
    assert settings.vehicle_id is None
    assert settings.tick_ms == 20
    assert settings.queue_maxlen == 10_000
    assert settings.publish_window == 1_000
    assert settings.tele_max_age_s == 72 * 3600
    assert settings.registry_interval_s == 300.0
    assert settings.state_dir is None


def test_settings_from_env_treats_blank_values_as_unset():
    """example.env templates every variable blank; sourcing it must be harmless."""
    env = {
        "OPENLAPS_PROFILE": str(EXAMPLE_PROFILE),
        "OPENLAPS_NATS_URL": "",
        "OPENLAPS_NATS_CREDS": "",
        "OPENLAPS_VEHICLE_ID": "",
        "OPENLAPS_TICK_MS": "",
        "OPENLAPS_STATE_DIR": "",
    }
    settings = AgentSettings.from_env(env)
    assert settings.nats_url == "nats://127.0.0.1:4222"
    assert settings.tick_ms == 20
    assert settings.state_dir is None


def test_settings_from_env_reports_unparseable_values_precisely():
    env = {"OPENLAPS_PROFILE": str(EXAMPLE_PROFILE), "OPENLAPS_TICK_MS": "fast"}
    with pytest.raises(ValueError, match="OPENLAPS_TICK_MS='fast'"):
        AgentSettings.from_env(env)
