"""P3.3 live-decoder unit tests: strict config, limiting, reload, and MQTT wire shape."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.batcher import Batcher
from core.catalog import ChannelPolicy
from core.pb import telemetry_pb2 as pb
from core.samples import Sample
from pit.live_decoder.config import ConfigError, LiveConfig, load_live_config
from pit.live_decoder.health import HealthState, serve_health
from pit.live_decoder.limiter import ConflatingLimiter
from pit.live_decoder.reload import ConfigReloader
from pit.live_decoder.service import (
    LiveDecoder,
    LiveDecoderSettings,
    LiveUpdate,
    MqttSink,
    mqtt_message,
)
from pit.registry_cache import MSG_TYPE_HEADER, MSG_TYPE_REGISTRY, DecodedSample


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _config(tmp_path: Path, *, total_max_hz: int = 500) -> LiveConfig:
    return load_live_config(
        _write(
            tmp_path / "live.yaml",
            f"""vehicle: car-1
defaults:
  max_hz: 10
  total_max_hz: {total_max_hz}
channels:
  - match: car.rpm
    max_hz: 20
  - match: car.*
  - match: lap.*
    max_hz: 0
""",
        )
    )


def _sample(name: str, value: object, capture_ms: float = 1_780_000_000_000.0) -> DecodedSample:
    return DecodedSample(capture_ms, pb.Channel(id=1, name=name), value)


def test_config_uses_first_matching_rule_and_inherits_default(tmp_path):
    config = _config(tmp_path)

    assert config.rule_for("car.rpm").max_hz == 20
    assert config.rule_for("car.speed").max_hz is None
    assert config.max_hz_for("car.speed") == 10
    assert config.max_hz_for("lap.event") == 0
    assert config.rule_for("position.lat") is None


def test_config_rejects_unknown_keys_and_invalid_rates(tmp_path):
    path = _write(
        tmp_path / "bad.yaml",
        """vehicle: car-1
defaults: {max_hz: 10, total_max_hz: 500, typo: true}
channels: [{match: 'car.*'}]
""",
    )
    with pytest.raises(ConfigError, match=r"defaults\.typo"):
        load_live_config(path)

    path.write_text(
        """vehicle: car-1
defaults: {max_hz: -1, total_max_hz: 500}
channels: [{match: 'car.*'}]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="greater than or equal to 0"):
        load_live_config(path)


def test_config_rejects_duplicate_yaml_keys(tmp_path):
    path = _write(
        tmp_path / "duplicate.yaml",
        """vehicle: car-1
vehicle: car-2
defaults: {max_hz: 10, total_max_hz: 500}
channels: [{match: 'car.*'}]
""",
    )
    with pytest.raises(ConfigError, match="duplicate key 'vehicle'"):
        load_live_config(path)


def test_unmatched_rules_are_reported_against_registry(tmp_path):
    config = load_live_config(
        _write(
            tmp_path / "unmatched.yaml",
            """vehicle: car-1
defaults: {max_hz: 10, total_max_hz: 500}
channels:
  - {match: car.rpm}
  - {match: car.typo}
  - {match: 'lap.*'}
""",
        )
    )
    registry = pb.ChannelRegistry(registry_seq=1)
    registry.channels.add(id=1, name="car.rpm")
    registry.channels.add(id=2, name="lap.event")

    assert config.unmatched_rules(registry) == ["car.typo"]


def test_limiter_conflates_to_the_newest_value_at_ten_hz(tmp_path):
    config = _config(tmp_path)
    limiter = ConflatingLimiter(config)
    published = []
    for index in range(100):
        now = index / 100.0
        candidates = limiter.offer(_sample("car.speed", index), now)
        candidates.extend(limiter.drain(now))
        published.extend(limiter.admit(candidates, now))

    assert len(published) == 10
    assert [item.value for item in published] == list(range(0, 100, 10))
    assert limiter.admit(limiter.drain(1.0), 1.0)[0].value == 99
    assert limiter.suppressed["car.speed"] == 89


def test_limiter_conflates_multiple_same_batch_candidates_to_latest(tmp_path):
    limiter = ConflatingLimiter(_config(tmp_path))
    candidates = limiter.offer(_sample("car.speed", 10), 0.0)
    candidates.extend(limiter.offer(_sample("car.speed", 11), 0.0))

    candidates = limiter.conflate_candidates(candidates)
    assert [update.value for update in candidates] == [11]
    assert limiter.suppressed["car.speed"] == 1


def test_unmatched_channels_are_never_published(tmp_path):
    limiter = ConflatingLimiter(_config(tmp_path))
    assert limiter.offer(_sample("position.lat", -31.0), 0.0) == []
    assert limiter.drain(1.0) == []


def test_zero_max_hz_passes_every_sample(tmp_path):
    limiter = ConflatingLimiter(_config(tmp_path))
    output = []
    for index in range(20):
        now = index / 1000.0
        output.extend(limiter.admit(limiter.offer(_sample("lap.event", str(index)), now), now))
    assert [item.value for item in output] == [str(index) for index in range(20)]
    assert not any(item.conflate for item in output)


def test_aggregate_budget_sheds_least_recently_published_candidates(tmp_path):
    config = load_live_config(
        _write(
            tmp_path / "aggregate.yaml",
            """vehicle: car-1
defaults: {max_hz: 1, total_max_hz: 1}
channels:
  - {match: 'car.*'}
""",
        )
    )
    limiter = ConflatingLimiter(config)

    initial = limiter.offer(_sample("car.old", 1), 0.0)
    assert [item.channel for item in limiter.admit(initial, 0.0)] == ["car.old"]
    assert limiter.offer(_sample("car.old", 2), 0.1) == []
    # Both candidates are due together. car.old was published more recently
    # than car.new (never), so the least-recent candidate is shed.
    candidates = limiter.offer(_sample("car.new", 3), 0.1)
    candidates.extend(limiter.drain(1.0))

    ready = limiter.admit(candidates, 1.0)
    assert [item.channel for item in ready] == ["car.old"]
    assert limiter.aggregate_sheds == 1
    assert limiter.suppressed["car.new"] == 1


def test_aggregate_budget_ranks_all_same_batch_candidates_by_recency(tmp_path):
    config = load_live_config(
        _write(
            tmp_path / "fairness.yaml",
            """vehicle: car-1
defaults: {max_hz: 0, total_max_hz: 1}
channels: [{match: 'car.*'}]
""",
        )
    )
    limiter = ConflatingLimiter(config)
    limiter.admit(limiter.offer(_sample("car.old", 1), 0.0), 0.0)

    candidates = limiter.offer(_sample("car.new", 2), 1.0)
    candidates.extend(limiter.offer(_sample("car.old", 3), 1.0))
    ready = limiter.admit(candidates, 1.0)

    assert [item.channel for item in ready] == ["car.old"]
    assert limiter.suppressed["car.new"] == 1


def test_fractional_aggregate_budget_carries_capacity_across_seconds(tmp_path):
    config = load_live_config(
        _write(
            tmp_path / "fractional.yaml",
            """vehicle: car-1
defaults: {max_hz: 0, total_max_hz: 0.5}
channels: [{match: 'lap.*'}]
""",
        )
    )
    limiter = ConflatingLimiter(config)

    first = limiter.offer(_sample("lap.event", "first"), 0.0)
    assert len(limiter.admit(first, 0.0)) == 1
    too_soon = limiter.offer(_sample("lap.event", "too-soon"), 1.0)
    assert limiter.admit(too_soon, 1.0) == []
    ready = limiter.offer(_sample("lap.event", "ready"), 2.0)
    output = limiter.admit(ready, 2.0)
    assert [item.value for item in output] == ["ready"]


def test_malformed_reload_preserves_previous_config(tmp_path, caplog):
    path = tmp_path / "live.yaml"
    _write(
        path,
        """vehicle: car-1
defaults: {max_hz: 10, total_max_hz: 500}
channels: [{match: 'car.*'}]
""",
    )
    reloader = ConfigReloader(path, poll_interval_s=5.0)
    original = reloader.config

    path.write_text("vehicle: [not valid for this model]\n", encoding="utf-8")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    with caplog.at_level(logging.ERROR):
        assert reloader.poll(5.0) is None
    assert reloader.config is original
    assert "previous config stays in force" in caplog.text


def test_sighup_request_forces_reload_before_mtime_poll(tmp_path):
    path = tmp_path / "live.yaml"
    _write(
        path,
        """vehicle: car-1
defaults: {max_hz: 10, total_max_hz: 500}
channels: [{match: 'car.*'}]
""",
    )
    reloader = ConfigReloader(path, poll_interval_s=60.0)
    path.write_text(
        """vehicle: car-1
defaults: {max_hz: 2, total_max_hz: 500}
channels: [{match: 'car.*'}]
""",
        encoding="utf-8",
    )

    reloader.request_reload()
    loaded = reloader.poll(0.1)
    assert loaded is not None
    assert loaded.defaults.max_hz == 2


def test_topic_and_payload_are_compact_json():
    topic, payload = mqtt_message("car-1", LiveUpdate("car.rpm", 1234.25, 4500.0))
    assert topic == "openlaps/car-1/car.rpm"
    assert json.loads(payload) == {"time": 1234.25, "value": 4500.0}
    assert b" " not in payload


def test_non_finite_values_are_not_encoded_as_invalid_json():
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="JSON"):
            mqtt_message("car-1", LiveUpdate("car.rpm", 1234.25, value))


def test_health_reports_per_channel_counts_and_reload_state(tmp_path):
    health = HealthState()
    health.note_publish("car.rpm")
    health.note_suppressed("car.rpm", 3)
    health.aggregate_sheds = 2
    health.config_mtime_ns = 123
    health.unmatched_rules = ["car.typo"]
    health.roll(now=health._started + 1.0)

    snapshot = health.snapshot(now=health._started + 2.0)
    assert snapshot["publish_rate"] == 1.0
    assert snapshot["channels"]["car.rpm"] == {"published": 1, "suppressed": 3}
    assert snapshot["aggregate_sheds"] == 2
    assert snapshot["config_mtime_ns"] == 123
    assert snapshot["unmatched_rules"] == ["car.typo"]


def test_health_endpoint_serves_snapshot():
    health = HealthState()
    health.note_publish("car.rpm")
    server = serve_health(health, 0, host="127.0.0.1")
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback test server
            f"http://127.0.0.1:{server.server_port}/health", timeout=2
        ) as response:
            payload = json.load(response)
        assert response.status == 200
        assert payload["channels"]["car.rpm"]["published"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_settings_validate_deploy_wiring(tmp_path):
    path = tmp_path / "live.yaml"
    settings = LiveDecoderSettings.from_env(
        {
            "OPENLAPS_NATS_URL": "nats://pit:4222",
            "OPENLAPS_MQTT_HOST": "mosquitto",
            "OPENLAPS_MQTT_PORT": "1884",
            "OPENLAPS_LIVE_HEALTH_PORT": "8082",
            "OPENLAPS_LIVE_CONFIG_POLL_S": "2.5",
        },
        config_path=path,
    )
    assert settings.config_path == path
    assert (settings.nats_url, settings.mqtt_host, settings.mqtt_port) == (
        "nats://pit:4222",
        "mosquitto",
        1884,
    )
    assert settings.config_poll_s == 2.5

    with pytest.raises(ValueError, match="QUEUE_SIZE"):
        LiveDecoderSettings.from_env({"OPENLAPS_LIVE_MQTT_QUEUE_SIZE": "0"}, config_path=path)


def test_settings_name_the_pit_sourced_stream(tmp_path):
    """The sourced stream declares no subjects, so it must be named outright."""
    path = tmp_path / "live.yaml"
    assert LiveDecoderSettings.from_env({}, config_path=path).stream == "TELE_VEHICLE"
    assert (
        LiveDecoderSettings.from_env(
            {"OPENLAPS_LIVE_STREAM": "TELE_SPARE"}, config_path=path
        ).stream
        == "TELE_SPARE"
    )
    with pytest.raises(ValueError, match="OPENLAPS_LIVE_STREAM"):
        LiveDecoderSettings.from_env({"OPENLAPS_LIVE_STREAM": "  "}, config_path=path)


def test_mqtt_down_drops_without_queueing_or_blocking(tmp_path):
    health = HealthState()
    sink = MqttSink(
        LiveDecoderSettings(config_path=tmp_path / "unused", nats_url="nats://unused"),
        "car-1",
        health,
    )
    sink.submit(LiveUpdate("car.rpm", 1.0, 4500.0))
    assert health.mqtt_drops == 1
    assert sink.pending_count == 0


def test_slow_mqtt_handoff_conflates_queued_gauge_updates(tmp_path):
    health = HealthState()
    sink = MqttSink(
        LiveDecoderSettings(config_path=tmp_path / "unused", nats_url="nats://unused"),
        "car-1",
        health,
    )
    sink.connected.set()
    sink.submit(LiveUpdate("car.rpm", 1.0, 4500.0))
    sink.submit(LiveUpdate("car.rpm", 2.0, 4600.0))

    assert sink.pending_count == 1
    assert sink._limited["car.rpm"].value == 4600.0
    assert sink.suppressed["car.rpm"] == 1


def test_mqtt_handoff_preserves_unlimited_event_updates(tmp_path):
    health = HealthState()
    sink = MqttSink(
        LiveDecoderSettings(config_path=tmp_path / "unused", nats_url="nats://unused"),
        "car-1",
        health,
    )
    sink.connected.set()
    sink.submit(LiveUpdate("lap.event", 1.0, "first", conflate=False))
    sink.submit(LiveUpdate("lap.event", 2.0, "second", conflate=False))

    assert sink.pending_count == 2
    assert [update.value for update in sink._unlimited] == ["first", "second"]


def test_initial_nats_failure_observes_stop_and_uses_outer_backoff(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "live.yaml",
        """vehicle: car-1
defaults: {max_hz: 10, total_max_hz: 500}
channels: [{match: 'car.*'}]
""",
    )
    decoder = LiveDecoder(
        LiveDecoderSettings(config_path=path, nats_url="nats://unavailable", health_port=0)
    )
    stop = asyncio.Event()

    class FailingNats:
        def __init__(self):
            self.options = {}

        async def connect(self, **options):
            assert options["max_reconnect_attempts"] == 1
            stop.set()
            raise OSError("unavailable")

    monkeypatch.setattr("pit.live_decoder.service.NatsClient", FailingNats)
    assert asyncio.run(decoder._connect_nats(stop)) is None


def test_service_recovers_registry_decodes_scaled_value_and_filters(tmp_path):
    path = _write(
        tmp_path / "live.yaml",
        """vehicle: car-1
defaults: {max_hz: 0, total_max_hz: 0}
channels: [{match: car.coolant_temp}]
""",
    )
    decoder = LiveDecoder(
        LiveDecoderSettings(config_path=path, nats_url="nats://unused", health_port=0)
    )
    sent = []
    decoder.sink = SimpleNamespace(submit=sent.append)

    registry = pb.ChannelRegistry(registry_seq=7, vehicle_id="car-1")
    registry.channels.add(
        id=3,
        name="car.coolant_temp",
        units="K",
        type=pb.UINT,
        scale=0.1,
    )
    decoder._handle_message(
        SimpleNamespace(
            headers={MSG_TYPE_HEADER: MSG_TYPE_REGISTRY},
            data=registry.SerializeToString(),
        ),
        0.0,
    )

    batcher = Batcher(
        7,
        {3: ChannelPolicy("car.coolant_temp", pb.UINT, None, scale=0.1)},
        tick_ms=20,
    )
    batcher.add("can0", 3, Sample("can0:test", 0, 1234.0, 355.0))
    payload = batcher.tick(1234, 0)["can0"]
    decoder._handle_message(SimpleNamespace(headers={}, data=payload), 0.0)

    assert len(sent) == 1
    assert sent[0].channel == "car.coolant_temp"
    assert sent[0].value == pytest.approx(355.0)
    assert sent[0].capture_unix_ms == 1234.0


def test_unknown_format_version_is_logged_once_and_never_published(tmp_path, caplog):
    path = _write(
        tmp_path / "live.yaml",
        """vehicle: car-1
defaults: {max_hz: 0, total_max_hz: 0}
channels: [{match: 'car.*'}]
""",
    )
    decoder = LiveDecoder(
        LiveDecoderSettings(config_path=path, nats_url="nats://unused", health_port=0)
    )
    sent = []
    decoder.sink = SimpleNamespace(submit=sent.append)
    registry = pb.ChannelRegistry(registry_seq=1, vehicle_id="car-1")
    registry.channels.add(id=1, name="car.rpm", type=pb.DOUBLE)
    decoder._add_registry(registry.SerializeToString())
    payload = pb.SampleBatch(registry_seq=1, format_version=99).SerializeToString()

    with caplog.at_level(logging.ERROR):
        decoder._handle_message(SimpleNamespace(headers={}, data=payload), 0.0)
        decoder._handle_message(SimpleNamespace(headers={}, data=payload), 0.1)

    assert sent == []
    assert decoder.health.bad_version_batches == 2
    assert caplog.text.count("format_version=99") == 1


def test_malformed_protobuf_is_logged_and_dropped_without_stopping_service(tmp_path, caplog):
    path = _write(
        tmp_path / "live.yaml",
        """vehicle: car-1
defaults: {max_hz: 0, total_max_hz: 0}
channels: [{match: 'car.*'}]
""",
    )
    decoder = LiveDecoder(
        LiveDecoderSettings(config_path=path, nats_url="nats://unused", health_port=0)
    )
    sent = []
    decoder.sink = SimpleNamespace(submit=sent.append)

    with caplog.at_level(logging.WARNING):
        decoder._handle_message(SimpleNamespace(headers={}, data=b"\x80"), 0.0)
        decoder._handle_message(
            SimpleNamespace(
                headers={MSG_TYPE_HEADER: MSG_TYPE_REGISTRY},
                data=b"\x80",
            ),
            0.1,
        )

    assert sent == []
    assert decoder.health.malformed_payloads == 2
    assert caplog.text.count("malformed protobuf") == 2


def _minimal_config(tmp_path: Path, vehicle: str) -> Path:
    return _write(
        tmp_path / "live.yaml",
        f"vehicle: {vehicle}\nchannels:\n  - match: car.rpm\n",
    )


def test_a_vehicle_id_the_yaml_disagrees_with_is_fatal_at_startup(tmp_path):
    # The failure this replaces was silent by construction: the service came
    # up, connected, subscribed to `tele.<yaml-vehicle>.>` on a stream that
    # only ever carried `tele.<env-vehicle>.>`, and reported healthy zeroes
    # for two 35-minute bench runs. Refusing to start is the only outcome
    # that cannot be mistaken for a car sitting in the paddock.
    settings = LiveDecoderSettings(
        config_path=_minimal_config(tmp_path, "example-club-racer"),
        nats_url="nats://pit:4222",
        vehicle_id="example-club-racer-parity",
    )
    with pytest.raises(ValueError) as excinfo:
        LiveDecoder(settings)
    message = str(excinfo.value)
    assert "example-club-racer-parity" in message
    assert "tele.example-club-racer.>" in message


def test_an_agreeing_vehicle_id_starts_and_an_absent_one_defers_to_the_yaml(tmp_path):
    config_path = _minimal_config(tmp_path, "car-7")
    agreeing = LiveDecoderSettings(
        config_path=config_path, nats_url="nats://pit:4222", vehicle_id="car-7"
    )
    assert LiveDecoder(agreeing).subject_filter == "tele.car-7.>"
    # The YAML stays the source of truth; the env var is only a cross-check,
    # so a deployment that does not set one is unaffected.
    unset = LiveDecoderSettings(config_path=config_path, nats_url="nats://pit:4222")
    assert LiveDecoder(unset).subject_filter == "tele.car-7.>"


def test_settings_carry_the_pit_wide_vehicle_id(tmp_path):
    settings = LiveDecoderSettings.from_env(
        {"OPENLAPS_VEHICLE_ID": "car-7"}, config_path=tmp_path / "live.yaml"
    )
    assert settings.vehicle_id == "car-7"
    blank = LiveDecoderSettings.from_env(
        {"OPENLAPS_VEHICLE_ID": "  "}, config_path=tmp_path / "live.yaml"
    )
    assert blank.vehicle_id is None


def test_health_reports_the_subject_it_decodes(tmp_path):
    settings = LiveDecoderSettings(
        config_path=_minimal_config(tmp_path, "car-7"),
        nats_url="nats://pit:4222",
        stream="TELE_VEHICLE",
    )
    snapshot = LiveDecoder(settings).health.snapshot()
    assert snapshot["stream"] == "TELE_VEHICLE"
    assert snapshot["subject_filter"] == "tele.car-7.>"


def test_the_shipped_config_names_the_example_profiles_vehicle():
    """`deploy/pit-config/live-decoder.yaml` must match the profile it decodes.

    The service refuses to start on a vehicle-id mismatch, so a wrong id in
    the shipped file is a pit whose gauges are down from the first boot.
    This drifted once already: a parity run's throwaway vehicle id
    (`example-club-racer-parity`) was committed in a conflict resolution,
    and every fresh checkout inherited a live-decoder that decoded nothing.
    """
    from conftest import EXAMPLE_PROFILE

    from core.config import load_profile

    shipped = load_live_config(
        Path(__file__).parents[1] / "deploy" / "pit-config" / "live-decoder.yaml"
    )
    assert shipped.vehicle == load_profile(EXAMPLE_PROFILE).vehicle.vehicle.id
