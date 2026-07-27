"""Strict profile configuration loading tests."""

from pathlib import Path

import pytest

from core.config import ConfigError, load_profile

EXAMPLE_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer"


def _write_minimal_profile(
    root: Path,
    *,
    channel_name: str = "car.rpm",
    channel_body: str = '{ from: "can0:ecu.ENGINE.RPM", units: "rpm" }',
    catalog_extra: str = "",
) -> Path:
    (root / "dbcs").mkdir(parents=True)
    (root / "dbcs" / "ecu.dbc").write_text('VERSION ""\n', encoding="utf-8")
    (root / "vehicle.yaml").write_text(
        """vehicle:
  id: test-car
buses:
  - name: can0
    interface: can0
    bitrate: 1000000
    dbcs:
      - device: ecu
        file: dbcs/ecu.dbc
serial: []
host:
  enabled: true
  interval: 5s
""",
        encoding="utf-8",
    )
    (root / "catalog.yaml").write_text(
        f"channels:\n  {channel_name}: {channel_body}\n{catalog_extra}apps: {{}}\n",
        encoding="utf-8",
    )
    return root


def test_example_profile_loads_end_to_end():
    profile = load_profile(EXAMPLE_PROFILE)

    assert profile.vehicle.vehicle.id == "example-club-racer"
    assert len(profile.vehicle.buses) == 1
    assert len(profile.vehicle.buses[0].dbcs) == 4
    assert profile.vehicle.host.interval_ns == 5_000_000_000
    assert len(profile.catalog.channels) >= 100
    assert profile.catalog.channels["position.fix_quality"].type == "string"


def test_unknown_catalog_key_reports_file_key_and_reason(tmp_path: Path):
    profile_dir = _write_minimal_profile(
        tmp_path,
        channel_body='{ from: "can0:ecu.ENGINE.RPM", typo: true }',
    )

    with pytest.raises(ConfigError) as exc_info:
        load_profile(profile_dir)

    message = str(exc_info.value)
    assert "catalog.yaml" in message
    assert "channels.car.rpm.typo" in message
    assert "Extra inputs are not permitted" in message


@pytest.mark.parametrize("name", ["engine.rpm", "lap.number", "timing.delta"])
def test_invalid_or_reserved_channel_namespace_is_rejected(tmp_path: Path, name: str):
    profile_dir = _write_minimal_profile(tmp_path, channel_name=name)

    with pytest.raises(ConfigError, match=r"catalog.yaml.*channels.*namespace"):
        load_profile(profile_dir)


def test_duplicate_channel_name_is_rejected_instead_of_silently_overwritten(tmp_path: Path):
    profile_dir = _write_minimal_profile(
        tmp_path,
        catalog_extra='  car.rpm: { from: "can0:ecu.ENGINE.RPM2" }\n',
    )

    with pytest.raises(ConfigError, match=r"catalog.yaml.*duplicate key 'car.rpm'"):
        load_profile(profile_dir)


def test_unhashable_yaml_mapping_key_reports_file_key_and_reason(tmp_path: Path):
    profile_dir = _write_minimal_profile(tmp_path)
    (profile_dir / "catalog.yaml").write_text(
        """channels:
  ? [car.rpm]
  : {from: "can0:ecu.ENGINE.RPM"}
apps: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_profile(profile_dir)

    message = str(exc_info.value)
    assert "catalog.yaml" in message
    assert "YAML error" in message
    assert "mapping key" in message
    assert "unhashable" in message


def test_vehicle_invalid_utf8_reports_file_and_reason(tmp_path: Path):
    profile_dir = _write_minimal_profile(tmp_path)
    (profile_dir / "vehicle.yaml").write_bytes(b"vehicle:\n  id: \xff\n")

    with pytest.raises(ConfigError) as exc_info:
        load_profile(profile_dir)

    message = str(exc_info.value)
    assert "vehicle.yaml" in message
    assert "UTF-8" in message


def test_missing_dbc_file_reports_vehicle_file_and_key(tmp_path: Path):
    profile_dir = _write_minimal_profile(tmp_path)
    (profile_dir / "dbcs" / "ecu.dbc").unlink()

    with pytest.raises(ConfigError) as exc_info:
        load_profile(profile_dir)

    message = str(exc_info.value)
    assert "vehicle.yaml" in message
    assert "buses.0.dbcs.0.file" in message
    assert "does not exist" in message


def test_excessively_large_duration_reports_file_key_and_reason(tmp_path: Path):
    duration = f"1{'0' * 400}h"
    profile_dir = _write_minimal_profile(
        tmp_path,
        channel_body=(f'{{ from: "can0:ecu.ENGINE.RPM", rbe: {{min_interval: {duration}}} }}'),
    )

    with pytest.raises(ConfigError) as exc_info:
        load_profile(profile_dir)

    message = str(exc_info.value)
    assert "catalog.yaml" in message
    assert "channels.car.rpm.rbe" in message
    assert "duration is too large" in message


@pytest.mark.parametrize("dbc_file", ["{absolute}", "../outside.dbc"])
def test_dbc_path_must_be_relative_and_contained_by_profile(tmp_path: Path, dbc_file: str):
    profile_dir = _write_minimal_profile(tmp_path / "profile")
    outside = tmp_path / "outside.dbc"
    outside.write_text('VERSION ""\n', encoding="utf-8")
    configured_path = str(outside) if dbc_file == "{absolute}" else dbc_file
    vehicle_path = profile_dir / "vehicle.yaml"
    vehicle_path.write_text(
        vehicle_path.read_text(encoding="utf-8").replace("dbcs/ecu.dbc", configured_path),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"vehicle.yaml.*dbcs.0.file.*relative.*profile"):
        load_profile(profile_dir)


def test_unknown_source_device_is_rejected(tmp_path: Path):
    profile_dir = _write_minimal_profile(
        tmp_path,
        channel_body='{ from: "can0:missing.ENGINE.RPM" }',
    )

    with pytest.raises(ConfigError, match=r"catalog.yaml.*channels.car.rpm.from.*unknown"):
        load_profile(profile_dir)


def test_wire_encoding_lever_parses_and_validates(tmp_path: Path):
    profile_dir = _write_minimal_profile(
        tmp_path,
        channel_body=(
            '{ from: "can0:ecu.ENGINE.RPM", encode: {type: uint, scale: 0.25, offset: 100} }'
        ),
    )

    channel = load_profile(profile_dir).catalog.channels["car.rpm"]

    assert channel.encode is not None
    assert channel.encode.type == "uint"
    assert channel.encode.scale == 0.25
    assert channel.encode.offset == 100.0


def test_zero_scale_rejects_nonzero_offset(tmp_path: Path):
    profile_dir = _write_minimal_profile(
        tmp_path,
        channel_body=(
            '{ from: "can0:ecu.ENGINE.RPM", encode: {type: uint, scale: 0, offset: 100} }'
        ),
    )

    with pytest.raises(
        ConfigError,
        match=r"catalog.yaml.*channels.car.rpm.encode.*offset must be zero when scale is zero",
    ):
        load_profile(profile_dir)


@pytest.mark.parametrize(
    ("coefficient", "value"),
    [("scale", ".nan"), ("scale", ".inf"), ("offset", ".nan"), ("offset", "-.inf")],
)
def test_integer_encoding_coefficients_must_be_finite(tmp_path: Path, coefficient: str, value: str):
    profile_dir = _write_minimal_profile(
        tmp_path,
        channel_body=(
            f'{{ from: "can0:ecu.ENGINE.RPM", encode: {{type: uint, {coefficient}: {value}}} }}'
        ),
    )

    with pytest.raises(ConfigError, match=rf"catalog.yaml.*{coefficient}.*finite"):
        load_profile(profile_dir)
