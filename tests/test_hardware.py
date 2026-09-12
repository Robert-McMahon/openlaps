"""The host-hardware overlay, and every target directory that ships one.

Two halves. The first exercises `core.hardware` against synthetic profiles:
what an overlay may change, what it must refuse, and that a changed port
leaves the catalog alone. The second walks `deploy/targets/` and holds each
shipped board to the contract `deploy/targets/README.md` states -- which is
what makes "adding a target is a directory, not a code change" true rather
than merely intended (ADR 0010).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.config import ConfigError, load_profile
from core.hardware import apply_hardware, load_hardware

ROOT = Path(__file__).parents[1]
EXAMPLE_PROFILE = ROOT / "profiles" / "example-club-racer"
TARGETS = ROOT / "deploy" / "targets"

TARGET_DIRS = sorted(path for path in TARGETS.iterdir() if path.is_dir())
TARGET_IDS = [path.name for path in TARGET_DIRS]


def _write_overlay(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# -- the overlay itself ------------------------------------------------------


def test_an_overlay_substitutes_interface_and_port(tmp_path):
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        """target: bench-box
buses:
  can0: { interface: vcan0 }
serial:
  serial0: { port: /dev/ttyS4 }
""",
    )

    profile = load_profile(EXAMPLE_PROFILE, overlay)

    assert profile.hardware_target == "bench-box"
    assert profile.vehicle.buses[0].interface == "vcan0"
    assert profile.vehicle.serial[0].port == "/dev/ttyS4"


def test_the_profile_is_unchanged_where_the_overlay_is_silent(tmp_path):
    """An overlay setting one field must not reset the others to defaults."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nserial:\n  serial0: { port: /dev/ttyS4 }\n",
    )

    plain = load_profile(EXAMPLE_PROFILE)
    overlaid = load_profile(EXAMPLE_PROFILE, overlay)

    assert overlaid.vehicle.buses == plain.vehicle.buses
    assert overlaid.vehicle.serial[0].baud == plain.vehicle.serial[0].baud
    assert overlaid.vehicle.serial[0].driver == plain.vehicle.serial[0].driver
    assert overlaid.vehicle.host == plain.vehicle.host


def test_remapping_a_port_leaves_the_catalog_identical(tmp_path):
    """The whole point: host wiring must not be able to move a channel."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        """target: bench-box
buses:
  can0: { interface: vcan0 }
serial:
  serial0: { port: /dev/ttyS4, baud: 230400 }
""",
    )

    plain = load_profile(EXAMPLE_PROFILE)
    overlaid = load_profile(EXAMPLE_PROFILE, overlay)

    assert overlaid.catalog == plain.catalog
    assert overlaid.catalog_hash == plain.catalog_hash


def test_no_overlay_leaves_the_profile_as_written():
    profile = load_profile(EXAMPLE_PROFILE)

    assert profile.hardware_target is None
    assert profile.vehicle.serial[0].port == "/dev/ttyUSB0"


def test_an_unknown_transport_is_an_error_not_an_ignored_key(tmp_path):
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nserial:\n  serial1: { port: /dev/ttyS4 }\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_profile(EXAMPLE_PROFILE, overlay)

    message = str(excinfo.value)
    assert "serial1" in message
    assert "serial0" in message


def test_an_unknown_bus_names_the_buses_the_profile_defines(tmp_path):
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nbuses:\n  can1: { interface: can1 }\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_profile(EXAMPLE_PROFILE, overlay)

    assert "can1" in str(excinfo.value)
    assert "can0" in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        "target: bench-box\nbuses:\n  can0: { bitrate: 0 }\n",
        "target: bench-box\nserial:\n  serial0: { port: '' }\n",
        "target: bench-box\nserial:\n  serial0: { baud: -1 }\n",
    ],
    ids=["zero-bitrate", "blank-port", "negative-baud"],
)
def test_an_overlay_is_held_to_the_profile_s_own_constraints(tmp_path, body):
    overlay = _write_overlay(tmp_path / "hardware.yaml", body)

    with pytest.raises(ConfigError):
        load_profile(EXAMPLE_PROFILE, overlay)


def test_a_misspelled_key_is_refused(tmp_path):
    """`extra=forbid`: a hardware file must not silently do nothing."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nserial:\n  serial0: { device: /dev/ttyS4 }\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_profile(EXAMPLE_PROFILE, overlay)

    assert "device" in str(excinfo.value)


def test_a_rig_can_say_the_receiver_is_absent(tmp_path):
    """The bench rig's third delta: a pty has no receiver to configure."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        """target: bench-box
serial:
  serial0:
    port: /tmp/openlaps-bench-gps
    driver: { configure_on_start: false }
""",
    )

    driver = load_profile(EXAMPLE_PROFILE, overlay).vehicle.serial[0].driver

    assert driver is not None
    assert driver.config.configure_on_start is False


def test_the_rest_of_the_driver_stays_the_car_s(tmp_path):
    """Only `configure_on_start` moves; the receiver's settings are the car's."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nserial:\n  serial0: { driver: { configure_on_start: false } }\n",
    )

    overlaid = load_profile(EXAMPLE_PROFILE, overlay).vehicle.serial[0].driver
    plain = load_profile(EXAMPLE_PROFILE).vehicle.serial[0].driver

    assert overlaid.name == plain.name
    assert overlaid.config.rate_hz == plain.config.rate_hz
    assert overlaid.config.sentences == plain.config.sentences
    assert overlaid.config.pps == plain.config.pps
    assert overlaid.config.timing_output == plain.config.timing_output


def test_the_driver_block_cannot_carry_the_receiver_s_own_settings(tmp_path):
    """`extra=forbid` keeps this from becoming a second place to set rate_hz."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nserial:\n  serial0: { driver: { rate_hz: 10 } }\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_hardware(overlay)

    assert "rate_hz" in str(excinfo.value)


def test_configuring_a_driver_the_profile_never_attached_is_an_error(tmp_path):
    profile = tmp_path / "profile"
    (profile / "dbcs").mkdir(parents=True)
    (profile / "dbcs" / "ecu.dbc").write_text('VERSION ""\n', encoding="utf-8")
    (profile / "vehicle.yaml").write_text(
        """vehicle:
  id: driverless
buses: []
serial:
  - name: serial0
    port: /dev/ttyUSB0
    baud: 115200
    decoder: nmea
host:
  enabled: true
  interval: 5s
""",
        encoding="utf-8",
    )
    (profile / "catalog.yaml").write_text(
        'channels:\n  position.speed: { from: "serial0:nmea.RMC.speed" }\napps: {}\n',
        encoding="utf-8",
    )
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nserial:\n  serial0: { driver: { configure_on_start: false } }\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_profile(profile, overlay)

    assert "no driver" in str(excinfo.value)


def test_an_overlay_names_the_board_s_temperature_sensors(tmp_path):
    """The third board-owned field: which sensor is behind `host:temp.cpu`."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: rk-box\nhost:\n  temperatures: { cpu: soc_thermal.0 }\n",
    )

    profile = load_profile(EXAMPLE_PROFILE, overlay)

    assert profile.vehicle.host.temperatures == {"cpu": "soc_thermal.0"}


def test_the_overlay_s_temperatures_replace_the_profile_s_not_merge(tmp_path):
    """A board's sensor set is a whole; the X4's `board` must not linger."""
    plain = load_profile(EXAMPLE_PROFILE).vehicle.host.temperatures
    assert "board" in plain, "the example profile is expected to map a board sensor"
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: rk-box\nhost:\n  temperatures: { cpu: soc_thermal.0 }\n",
    )

    overlaid = load_profile(EXAMPLE_PROFILE, overlay).vehicle.host.temperatures

    assert overlaid == {"cpu": "soc_thermal.0"}


def test_an_empty_temperature_mapping_means_none_and_no_block_means_the_profile_s(tmp_path):
    plain = load_profile(EXAMPLE_PROFILE).vehicle.host.temperatures
    none = _write_overlay(tmp_path / "none.yaml", "target: rk-box\nhost:\n  temperatures: {}\n")
    silent = _write_overlay(tmp_path / "silent.yaml", "target: rk-box\n")

    assert load_profile(EXAMPLE_PROFILE, none).vehicle.host.temperatures == {}
    assert load_profile(EXAMPLE_PROFILE, silent).vehicle.host.temperatures == plain


def test_temperatures_leave_the_rest_of_the_host_block_alone(tmp_path):
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: rk-box\nhost:\n  temperatures: { cpu: soc_thermal.0 }\n",
    )

    overlaid = load_profile(EXAMPLE_PROFILE, overlay).vehicle.host
    plain = load_profile(EXAMPLE_PROFILE).vehicle.host

    assert overlaid.enabled == plain.enabled
    assert overlaid.interval_ns == plain.interval_ns


@pytest.mark.parametrize(
    "body",
    [
        "target: rk-box\nhost:\n  temperatures: { CPU: soc_thermal.0 }\n",
        "target: rk-box\nhost:\n  temperatures: { cpu: soc_thermal }\n",
        "target: rk-box\nhost:\n  temperatures: { cpu: 'soc thermal.0' }\n",
    ],
)
def test_a_malformed_sensor_name_is_refused_at_load(tmp_path, body):
    """`<chip>.<label>`, lowercase, exactly as the collector names them."""
    overlay = _write_overlay(tmp_path / "hardware.yaml", body)

    with pytest.raises(ConfigError):
        load_profile(EXAMPLE_PROFILE, overlay)


def test_the_host_block_cannot_carry_the_collector_s_own_settings(tmp_path):
    """`enabled` and `interval` are the car's choice, not the board's."""
    overlay = _write_overlay(tmp_path / "hardware.yaml", "target: rk-box\nhost:\n  interval: 1s\n")

    with pytest.raises(ConfigError) as excinfo:
        load_hardware(overlay)

    assert "interval" in str(excinfo.value)


def test_link_options_do_not_leak_into_the_profile(tmp_path):
    """`link:` is an argument to `ip`, not a field of BusConfig."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        """target: bench-box
buses:
  can0:
    interface: can0
    link: { fd: true, dbitrate: 2000000 }
""",
    )

    profile = load_profile(EXAMPLE_PROFILE, overlay)

    assert not hasattr(profile.vehicle.buses[0], "fd")
    assert load_hardware(overlay).link("can0").dbitrate == 2000000


def test_a_bus_with_no_link_block_gets_classic_defaults(tmp_path):
    overlay = _write_overlay(
        tmp_path / "hardware.yaml", "target: bench-box\nbuses:\n  can0: { interface: can0 }\n"
    )

    link = load_hardware(overlay).link("can0")

    assert link.fd is False
    assert link.dbitrate is None


def test_a_data_bitrate_without_fd_is_refused(tmp_path):
    """The kernel calls it `Operation not supported`; say so at load instead."""
    overlay = _write_overlay(
        tmp_path / "hardware.yaml",
        "target: bench-box\nbuses:\n  can0: { link: { dbitrate: 2000000 } }\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_hardware(overlay)

    assert "fd" in str(excinfo.value)


def test_a_target_name_is_required(tmp_path):
    overlay = _write_overlay(tmp_path / "hardware.yaml", "buses:\n  can0: { interface: can0 }\n")

    with pytest.raises(ConfigError):
        load_hardware(overlay)


def test_apply_hardware_reports_the_file_it_was_given(tmp_path):
    """Errors must name the hardware file, not the profile it was applied to."""
    overlay = _write_overlay(
        tmp_path / "luckfox.yaml", "target: t\nbuses:\n  nope: { interface: can9 }\n"
    )
    vehicle = load_profile(EXAMPLE_PROFILE).vehicle

    with pytest.raises(ConfigError) as excinfo:
        apply_hardware(vehicle, load_hardware(overlay), path=overlay)

    assert "luckfox.yaml" in str(excinfo.value)


# -- the shipped targets -----------------------------------------------------


def test_targets_are_shipped():
    """Guards the parametrisation below: an empty sweep proves nothing.

    A containment check rather than an equality one, deliberately. Adding a
    board is a directory and no code (ADR 0010), and a test that had to be
    edited to accept a third target would quietly make that untrue.
    """
    assert {"radxa-x4", "luckfox-omni3576"} <= set(TARGET_IDS)


@pytest.mark.parametrize("target", TARGET_DIRS, ids=TARGET_IDS)
def test_every_target_applies_cleanly_to_the_example_profile(target):
    """A target that names a transport nobody has fails here, not on the car."""
    profile = load_profile(EXAMPLE_PROFILE, target / "hardware.yaml")

    assert profile.hardware_target == target.name


@pytest.mark.parametrize("target", TARGET_DIRS, ids=TARGET_IDS)
def test_every_target_has_the_files_the_readme_promises(target):
    for name in ("README.md", "target.env", "hardware.yaml", "go2rtc.yaml"):
        assert (target / name).is_file(), f"{target.name} is missing {name}"


@pytest.mark.parametrize("target", TARGET_DIRS, ids=TARGET_IDS)
def test_a_target_env_selects_its_own_directory(target):
    env = _read_env(target / "target.env")

    assert env["OPENLAPS_TARGET"] == target.name


@pytest.mark.parametrize("target", TARGET_DIRS, ids=TARGET_IDS)
def test_the_mapped_device_is_the_port_the_agent_opens(target):
    """The one duplication ADR 0010 accepts, held together here.

    compose maps a device node and cannot read YAML to find out which; the
    agent opens a port and does not read `.env`. They have to agree, and now
    they have to agree in CI rather than at the first failed open on the car.
    """
    env = _read_env(target / "target.env")
    overlay = load_hardware(target / "hardware.yaml")

    ports = {name: source.port for name, source in overlay.serial.items() if source.port}
    assert env["OPENLAPS_SERIAL_DEVICE"] in ports.values(), (
        f"{target.name}: OPENLAPS_SERIAL_DEVICE={env['OPENLAPS_SERIAL_DEVICE']} "
        f"is not any of hardware.yaml's ports {sorted(ports.values())}"
    )


@pytest.mark.parametrize("target", TARGET_DIRS, ids=TARGET_IDS)
def test_every_target_pins_the_three_ports_the_firewall_counts(target):
    """deploy/nft/bench-*.nft counts 1984/8554/8555 and cannot ask go2rtc."""
    config = yaml.safe_load((target / "go2rtc.yaml").read_text(encoding="utf-8"))

    assert config["api"]["listen"] == ":1984"
    assert config["rtsp"]["listen"] == ":8554"
    assert config["webrtc"]["listen"] == ":8555"


@pytest.mark.parametrize("target", TARGET_DIRS, ids=TARGET_IDS)
def test_every_target_serves_the_stream_the_dashboard_defaults_to(target):
    """`car_h264` is the `video` dashboard's default and plays anywhere.

    `car` is the H.265 encode and is target-dependent -- the Luckfox's VEPU
    will not give MPP an HEVC context -- so it is deliberately not required.
    """
    config = yaml.safe_load((target / "go2rtc.yaml").read_text(encoding="utf-8"))

    assert "car_h264" in config["streams"]


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            env[key.strip()] = value.split("#")[0].strip()
    return env


# -- the deploy wiring that has to agree with all of the above ---------------


def test_compose_maps_the_serial_device_at_the_same_path_both_sides():
    """The overlay can only name the port if the container agrees on it.

    The old arrangement rewrote the host device to /dev/ttyUSB0 inside the
    container so the profile did not have to encode the host's wiring. ADR
    0010 does that job properly, and the rewrite would now defeat it.
    """
    compose = (ROOT / "deploy" / "vehicle-compose.yaml").read_text(encoding="utf-8")

    assert (
        "- ${OPENLAPS_SERIAL_DEVICE:-/dev/ttyACM0}:${OPENLAPS_SERIAL_DEVICE:-/dev/ttyACM0}"
        in compose
    )
    assert ":/dev/ttyUSB0" not in compose


def test_compose_derives_both_target_files_from_one_variable():
    compose = (ROOT / "deploy" / "vehicle-compose.yaml").read_text(encoding="utf-8")

    assert "OPENLAPS_HARDWARE: /app/targets/${OPENLAPS_TARGET:-radxa-x4}/hardware.yaml" in compose
    assert "./targets/${OPENLAPS_TARGET:-radxa-x4}/go2rtc.yaml:/config/go2rtc.yaml:ro" in compose
    assert "- ./targets:/app/targets:ro" in compose


def test_the_agent_unit_does_not_try_to_configure_an_interface_it_cannot():
    """An ExecStartPre here runs as User=openlaps and is refused every time."""
    unit = (ROOT / "deploy" / "systemd" / "openlaps-agent.service").read_text(encoding="utf-8")

    assert "ip link set" not in unit
    assert "openlaps-can.service" in unit


def test_the_can_unit_runs_the_tool_as_root_before_the_agent():
    unit = (ROOT / "deploy" / "systemd" / "openlaps-can.service").read_text(encoding="utf-8")

    assert "User=root" in unit
    assert "Before=openlaps-agent.service" in unit
    assert "tools/can_up.py" in unit
