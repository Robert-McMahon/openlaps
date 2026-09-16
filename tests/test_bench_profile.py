"""The bench rig measures the example profile's channel mix, or nothing.

`tools/bench-hardware.yaml` exists so Phase 4 can drive the real agent from
synthetic sources (P4.1). Its whole value rests on one property: that the
channel set, the wire encodings and the registry it produces are the example
profile's, so a bandwidth number measured on the bench is a bandwidth number
about the car.

**Most of that property is now true by construction.** Until 2026-09-09 the
bench was `profiles/example-club-racer-bench/`, a byte-for-byte copy of the
example profile with three lines changed, and the bulk of this file was a
copy-policing exercise -- `filecmp` over eight shared files, and an assertion
that no ninth file had appeared. An overlay (ADR 0010) reads the same
`catalog.yaml` and the same DBCs off disk, so there is no copy to drift and
nothing to police. What remains is what the overlay is allowed to change, and
the two consequences that follow from it changing nothing else.
"""

from __future__ import annotations

from pathlib import Path

from conftest import EXAMPLE_PROFILE

from core.catalog import build_runtime_catalog
from core.config import load_profile

BENCH_HARDWARE = Path(__file__).parents[1] / "tools" / "bench-hardware.yaml"


def test_the_bench_overlay_keeps_the_vehicle_identity_and_the_catalog():
    bench = load_profile(EXAMPLE_PROFILE, BENCH_HARDWARE)
    example = load_profile(EXAMPLE_PROFILE)

    # Same car: changing the id would change every subject and every
    # pit-side setting along with it (docs/plan/PHASE4.md, P4.1).
    assert bench.vehicle.vehicle.id == example.vehicle.vehicle.id
    assert bench.catalog_hash == example.catalog_hash
    assert bench.hardware_target == "bench-rig"


def test_only_the_transports_and_the_receiver_s_absence_differ():
    bench = load_profile(EXAMPLE_PROFILE, BENCH_HARDWARE).vehicle
    example = load_profile(EXAMPLE_PROFILE).vehicle

    bus, example_bus = bench.buses[0], example.buses[0]
    # The bus *name* is what every catalog `from: "can0:..."` resolves
    # against; only the socketCAN device underneath it moves.
    assert bus.name == example_bus.name == "can0"
    assert bus.interface == "vcan0"
    assert bus.bitrate == example_bus.bitrate
    assert [(dbc.device, dbc.file) for dbc in bus.dbcs] == [
        (dbc.device, dbc.file) for dbc in example_bus.dbcs
    ]

    serial, example_serial = bench.serial[0], example.serial[0]
    assert serial.name == example_serial.name
    assert serial.decoder == example_serial.decoder
    assert serial.baud == example_serial.baud
    assert serial.port != example_serial.port

    # The driver stays attached: serial source refs resolve against the
    # driver name when one is present, so detaching it would invalidate every
    # `serial0:um980.RMC.*` entry -- and it is also what lets the collector
    # accept the pit's RTCM write-back, which is reverse-channel traffic the
    # bench is there to measure.
    assert serial.driver is not None
    assert serial.driver.name == example_serial.driver.name == "um980"
    assert serial.driver.config.configure_on_start is False

    # ...and everything else about that driver is the car's, unread rather
    # than deleted: `UM980Driver.configure` returns immediately when
    # configure_on_start is false, so the real receiver's settings stay
    # legible in vehicle.yaml without ever being sent to a pty.
    assert serial.driver.config.rate_hz == example_serial.driver.config.rate_hz
    assert serial.driver.config.sentences == example_serial.driver.config.sentences
    assert serial.driver.config.pps == example_serial.driver.config.pps
    assert serial.driver.config.timing_output == example_serial.driver.config.timing_output

    assert bench.host == example.host


def test_runtime_catalog_is_identical_apart_from_registry_bookkeeping(tmp_path: Path):
    """Same channels, same ids, same source refs, same wire encodings."""
    bench = build_runtime_catalog(
        load_profile(EXAMPLE_PROFILE, BENCH_HARDWARE),
        state_path=tmp_path / "bench.json",
        created_unix_ms=1,
    )
    example = build_runtime_catalog(
        load_profile(EXAMPLE_PROFILE), state_path=tmp_path / "example.json", created_unix_ms=1
    )

    assert bench.channel_ids == example.channel_ids
    assert bench.source_map == example.source_map
    assert bench.catalog_hash == example.catalog_hash
    assert bench.registry.registry_seq == example.registry.registry_seq == 1
    assert bench.registry.SerializeToString() == example.registry.SerializeToString()


def test_a_bench_run_no_longer_bumps_the_registry_generation(tmp_path: Path):
    """The footgun the old bench profile's README had to warn about, gone.

    The generation counter keys on the catalog content, and lives in a state
    file beside the profile unless OPENLAPS_STATE_DIR moves it. Two profile
    *directories* therefore meant two independent sequences: a pit that had
    already seen the car's generations rejected bench batches with
    `unknown_seq_batches` until it rescanned the catalog subject.

    One directory and an overlay means one sequence, because the overlay
    cannot reach the catalog. Alternating between a bench run and a real run
    against the same state file is now a no-op, which is the whole reason the
    host wiring was worth separating from the car.
    """
    state = tmp_path / "registry.json"

    first = build_runtime_catalog(load_profile(EXAMPLE_PROFILE), state_path=state)
    bench = build_runtime_catalog(load_profile(EXAMPLE_PROFILE, BENCH_HARDWARE), state_path=state)
    back = build_runtime_catalog(load_profile(EXAMPLE_PROFILE), state_path=state)

    assert first.registry.registry_seq == bench.registry.registry_seq == back.registry.registry_seq
