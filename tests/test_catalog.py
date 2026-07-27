"""Runtime catalog and protobuf registry construction tests."""

import fcntl
import hashlib
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

import core.catalog as catalog_module
from core.catalog import DERIVED_ID_START, build_runtime_catalog
from core.config import ConfigError, load_profile
from core.pb import telemetry_pb2 as pb

EXAMPLE_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer"


def _write_profile(root: Path, channel_line: str) -> Path:
    (root / "dbcs").mkdir(parents=True)
    (root / "dbcs" / "ecu.dbc").write_text('VERSION ""\n', encoding="utf-8")
    (root / "vehicle.yaml").write_text(
        """vehicle: {id: test-car}
buses:
  - name: can0
    interface: can0
    bitrate: 1000000
    dbcs: [{device: ecu, file: dbcs/ecu.dbc}]
serial: []
host: {enabled: false, interval: 5s}
""",
        encoding="utf-8",
    )
    (root / "catalog.yaml").write_text(
        f"channels:\n  {channel_line}\napps: {{}}\n",
        encoding="utf-8",
    )
    return root


def _update_state_in_child(state_path: str, result_queue: multiprocessing.Queue) -> None:
    result_queue.put(catalog_module._update_registry_state(Path(state_path), "new-hash"))


def test_example_catalog_builds_ordered_mapping_and_round_trip_registry(tmp_path: Path):
    profile = load_profile(EXAMPLE_PROFILE)
    runtime = build_runtime_catalog(
        profile,
        state_path=tmp_path / "registry-state.json",
        created_unix_ms=1_753_500_000_000,
    )

    assert len(runtime.source_map) >= 100
    channel_id, policy = runtime.source_map["can0:haltech.TEMPERATURE1.AIR_TEMPERATURE"]
    assert channel_id == 1
    assert policy.name == "car.air_temp"
    assert policy.value_type == pb.DOUBLE
    assert runtime.registry.channels[0].name == "car.air_temp"
    assert runtime.registry.registry_seq == 1
    assert runtime.registry.vehicle_id == "example-club-racer"
    assert runtime.registry.created_unix_ms == 1_753_500_000_000

    wire = runtime.registry.SerializeToString()
    decoded = pb.ChannelRegistry.FromString(wire)
    assert decoded == runtime.registry


def test_registry_uses_channel_type_and_encoding_policy(tmp_path: Path):
    profile_dir = _write_profile(
        tmp_path,
        (
            'car.rpm: {from: "can0:ecu.ENGINE.RPM", units: rpm, '
            "encode: {type: uint, scale: 0.25, offset: 100}}"
        ),
    )

    runtime = build_runtime_catalog(load_profile(profile_dir), created_unix_ms=1)
    channel = runtime.registry.channels[0]
    _, policy = runtime.source_map["can0:ecu.ENGINE.RPM"]

    assert channel.type == pb.UINT
    assert channel.scale == 0.25
    assert channel.offset == 100.0
    assert policy.value_type == pb.UINT
    assert policy.scale == 0.25
    assert policy.offset == 100.0


def test_derived_channels_have_ids_in_a_reserved_stable_range(tmp_path: Path):
    profile_dir = _write_profile(
        tmp_path,
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )

    runtime = build_runtime_catalog(load_profile(profile_dir), created_unix_ms=1)
    derived = [
        channel
        for channel in runtime.registry.channels
        if channel.name.startswith(("lap.", "timing."))
    ]

    assert derived
    assert all(channel.id >= DERIVED_ID_START for channel in derived)
    assert runtime.channel_ids["timing.delta_best"] >= DERIVED_ID_START


def test_registry_seq_is_stable_then_bumps_when_catalog_content_changes(tmp_path: Path):
    profile_dir = _write_profile(
        tmp_path,
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )
    profile = load_profile(profile_dir)

    first = build_runtime_catalog(profile, created_unix_ms=1)
    second = build_runtime_catalog(load_profile(profile_dir), created_unix_ms=2)
    assert first.registry.registry_seq == 1
    assert second.registry.registry_seq == 1

    catalog_path = profile_dir / "catalog.yaml"
    catalog_path.write_text(
        catalog_path.read_text(encoding="utf-8").replace(
            'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
            'car.engine_speed: {from: "can0:ecu.ENGINE.RPM"}',
        ),
        encoding="utf-8",
    )
    changed = build_runtime_catalog(load_profile(profile_dir), created_unix_ms=3)

    assert changed.registry.registry_seq == 2
    assert changed.registry.channels[0].id == 1
    assert changed.registry.channels[0].name == "car.engine_speed"


def test_runtime_hash_and_registry_use_loaded_catalog_snapshot(tmp_path: Path):
    profile_dir = _write_profile(
        tmp_path,
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )
    original_bytes = (profile_dir / "catalog.yaml").read_bytes()
    profile = load_profile(profile_dir)
    (profile_dir / "catalog.yaml").write_text(
        'channels:\n  car.speed: {from: "can0:ecu.ENGINE.RPM"}\napps: {}\n',
        encoding="utf-8",
    )

    runtime = build_runtime_catalog(profile, created_unix_ms=1)

    assert runtime.catalog_hash == hashlib.sha256(original_bytes).hexdigest()
    assert runtime.registry.channels[0].name == "car.rpm"


def test_registry_state_update_holds_process_lock(tmp_path: Path):
    state_path = tmp_path / "registry-state.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.touch()
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()

    with lock_path.open("r+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        process = context.Process(
            target=_update_state_in_child,
            args=(str(state_path), result_queue),
        )
        process.start()
        time.sleep(0.2)
        assert process.is_alive(), "registry update ignored the process lock"
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    process.join(timeout=5)
    assert process.exitcode == 0
    assert result_queue.get(timeout=1) == 1


def test_catalog_byte_read_failure_is_wrapped_with_path_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    profile_dir = _write_profile(
        tmp_path,
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )
    original_read_bytes = Path.read_bytes

    def fail_catalog_read(path: Path) -> bytes:
        if path.name == "catalog.yaml":
            raise OSError("catalog read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_catalog_read)

    with pytest.raises(ConfigError, match=r"catalog.yaml.*catalog read failure"):
        load_profile(profile_dir)


def test_registry_state_invalid_utf8_reports_path_and_reason(tmp_path: Path):
    profile_dir = _write_profile(
        tmp_path / "profile",
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )
    state_path = tmp_path / "registry-state.json"
    state_path.write_bytes(b"\xff")

    with pytest.raises(ConfigError, match=r"registry-state.json.*invalid registry state.*UTF-8"):
        build_runtime_catalog(profile_dir, state_path=state_path, created_unix_ms=1)


@pytest.mark.parametrize("registry_seq", [True, 1.0, "1", None])
def test_registry_state_rejects_non_integer_sequence_values(tmp_path: Path, registry_seq: object):
    profile_dir = _write_profile(
        tmp_path / "profile",
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )
    state_path = tmp_path / "registry-state.json"
    state_path.write_text(
        json.dumps({"catalog_hash": "old-hash", "registry_seq": registry_seq}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"registry-state.json.*registry_seq.*must be an integer"):
        build_runtime_catalog(profile_dir, state_path=state_path, created_unix_ms=1)


def test_registry_state_directory_creation_failure_is_wrapped(tmp_path: Path):
    profile_dir = _write_profile(
        tmp_path / "profile",
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    state_path = parent_file / "registry-state.json"

    with pytest.raises(ConfigError, match=r"registry-state.json.*unable.*File exists"):
        build_runtime_catalog(profile_dir, state_path=state_path, created_unix_ms=1)


def test_registry_state_cleanup_does_not_mask_primary_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    profile_dir = _write_profile(
        tmp_path / "profile",
        'car.rpm: {from: "can0:ecu.ENGINE.RPM"}',
    )

    def fail_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        raise OSError("primary replace failure")

    def fail_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        raise OSError("cleanup failure")

    monkeypatch.setattr(catalog_module.os, "replace", fail_replace)
    monkeypatch.setattr(catalog_module.Path, "unlink", fail_cleanup)

    with pytest.raises(ConfigError, match=r"registry-state.json.*primary replace failure"):
        build_runtime_catalog(profile_dir, created_unix_ms=1)
