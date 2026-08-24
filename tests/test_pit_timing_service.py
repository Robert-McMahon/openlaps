"""P6.6 timing extrapolator deploy config and service wire contracts."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.batcher import Batcher
from core.catalog import ChannelPolicy
from core.pb import telemetry_pb2 as pb
from core.samples import Sample
from pit.registry_cache import MSG_TYPE_HEADER, MSG_TYPE_REGISTRY
from pit.timing_extrapolator.config import ConfigError, load_config
from pit.timing_extrapolator.health import HealthState, serve_health
from pit.timing_extrapolator.service import TimingService, TimingServiceSettings, mqtt_message

REPO = Path(__file__).parents[1]


def event(kind: str, at: float, **values: object) -> str:
    payload = {
        "type": kind,
        "time": at,
        "lap_number": 1,
        "sector": 0,
        "split_time": 0.0,
        "lap_time": 0.0,
    }
    payload.update(values)
    return json.dumps(payload)


def test_shipped_config_is_strict_and_names_only_pit_owned_outputs(tmp_path):
    config = load_config(REPO / "deploy" / "pit-config" / "timing-extrapolator.yaml")

    assert config.vehicle == "example-club-racer"
    assert config.publish_hz == 10
    assert config.output_channels == (
        "timing.lap_elapsed_pit",
        "timing.sector_elapsed_pit",
    )

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("vehicle: car-1\npublish_hz: 10\ntypo: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="typo"):
        load_config(invalid)


def test_mqtt_payload_marks_pit_values_and_extrapolation_status():
    topic, payload = mqtt_message(
        "car-1", "timing.lap_elapsed_pit", 1_780_000_000.25, 42.5, "extrapolating", None
    )

    assert topic == "openlaps/car-1/timing.lap_elapsed_pit"
    assert json.loads(payload) == {
        "time": 1_780_000_000_250,
        "value": 42.5,
        "status": "extrapolating",
        "source": "pit",
    }


def test_service_decodes_authoritative_inputs_and_emits_only_pit_channels(tmp_path):
    config_path = tmp_path / "timing.yaml"
    config_path.write_text("vehicle: car-1\npublish_hz: 10\n", encoding="utf-8")
    service = TimingService(
        TimingServiceSettings(config_path=config_path, nats_url="nats://unused", health_port=0)
    )
    sent = []
    service.publish = sent.append

    registry = pb.ChannelRegistry(registry_seq=7, vehicle_id="car-1")
    channels = {
        1: ("sys.host.clock_offset_s", pb.DOUBLE),
        2: ("sys.host.clock_stratum", pb.INT64),
        3: ("sys.host.clock_source", pb.STRING),
        4: ("lap.event", pb.STRING),
    }
    for channel_id, (name, kind) in channels.items():
        registry.channels.add(id=channel_id, name=name, type=kind)
    service._handle_message(
        SimpleNamespace(
            headers={MSG_TYPE_HEADER: MSG_TYPE_REGISTRY}, data=registry.SerializeToString()
        )
    )

    policies = {channel_id: ChannelPolicy(name, kind, None) for channel_id, (name, kind) in channels.items()}
    batcher = Batcher(7, policies, tick_ms=20)
    values = [0.001, 1, "GPS", event("lap_completed", 1_000.0, lap_time=92.5)]
    for channel_id, value in enumerate(values, start=1):
        batcher.add("derived", channel_id, Sample("test", 0, 1_000_000.0, value))
    payload = batcher.tick(1_000_000, 0)["derived"]
    service._handle_message(SimpleNamespace(headers={}, data=payload))
    service._publish_tick(1_010.0)

    assert sent
    assert {item[0] for item in sent} <= set(service.config.output_channels)
    assert sent[-1][0] == "timing.sector_elapsed_pit"
    assert sent[-2][0] == "timing.lap_elapsed_pit"


def test_settings_name_the_sourced_stream_and_dedicated_health_port(tmp_path):
    settings = TimingServiceSettings.from_env(
        {
            "OPENLAPS_NATS_URL": "nats://pit:4222",
            "OPENLAPS_TIMING_STREAM": "TELE_SPARE",
            "OPENLAPS_TIMING_HEALTH_PORT": "8094",
            "OPENLAPS_MQTT_HOST": "mosquitto",
        },
        config_path=tmp_path / "timing.yaml",
    )

    assert settings.stream == "TELE_SPARE"
    assert settings.health_port == 8094
    assert settings.mqtt_host == "mosquitto"


def test_health_endpoint_exposes_gate_and_runaway_visibility():
    health = HealthState("TELE_VEHICLE", "tele.car-1.>")
    health.status["timing.lap_elapsed_pit"] = "degraded"
    health.reason["timing.lap_elapsed_pit"] = "runaway"
    health.published["timing.lap_elapsed_pit"] = 3
    server = serve_health(health, 0, host="127.0.0.1")
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback test server
            f"http://127.0.0.1:{server.server_port}/health", timeout=2
        ) as response:
            payload = json.load(response)
    finally:
        server.shutdown()
        server.server_close()

    assert payload["stream"] == "TELE_VEHICLE"
    assert payload["channels"]["timing.lap_elapsed_pit"]["status"] == "degraded"
    assert payload["channels"]["timing.lap_elapsed_pit"]["reason"] == "runaway"


def test_entrypoint_compose_env_and_health_probe_wire_the_separate_service():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    compose = (REPO / "deploy" / "pit-compose.yaml").read_text(encoding="utf-8")
    env = (REPO / "example.env").read_text(encoding="utf-8")

    assert 'openlaps-timing-extrapolator = "pit.timing_extrapolator.__main__:main"' in pyproject
    assert "timing-extrapolator:" in compose
    assert 'command: ["openlaps-timing-extrapolator"]' in compose
    assert '"8084:8084"' in compose
    assert "OPENLAPS_TIMING_STREAM" in env
    assert "OPENLAPS_TIMING_CONFIG" in env
    assert "OPENLAPS_TIMING_HEALTH_PORT=8084" in env
