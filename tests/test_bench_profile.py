"""The bench profile measures the example profile's channel mix, or nothing.

`profiles/example-club-racer-bench` exists so Phase 4 can drive the real
agent from synthetic sources (P4.1). Its whole value rests on one property:
that the channel set, the wire encodings and the registry it produces are
the example profile's, so a bandwidth number measured on the bench is a
bandwidth number about the car. These tests are that property, mechanised
-- a stray edit to either catalog fails here rather than silently changing
what a bench run means.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest
from conftest import EXAMPLE_PROFILE

from core.catalog import build_runtime_catalog
from core.config import load_profile

BENCH_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer-bench"

# Everything except vehicle.yaml, which is the one file allowed to differ.
SHARED_FILES = (
    "catalog.yaml",
    "dbcs/fdi-imu.dbc",
    "dbcs/haltech-ecu.dbc",
    "dbcs/haltech-multiplexed.dbc",
    "dbcs/haltech-wideband.dbc",
    "dbcs/README.md",
    "tracks/Wanneroo.json",
    "tracks/Wanneroo.kml",
)


@pytest.mark.parametrize("relative", SHARED_FILES)
def test_shared_files_are_byte_identical_to_the_example_profile(relative: str):
    example, bench = EXAMPLE_PROFILE / relative, BENCH_PROFILE / relative
    assert bench.is_file(), f"{relative} is missing from the bench profile"
    assert filecmp.cmp(example, bench, shallow=False), (
        f"{relative} has drifted from the example profile's copy"
    )


def test_bench_profile_contains_nothing_else():
    """A file only the bench has is a difference nobody declared."""
    extra = {
        str(path.relative_to(BENCH_PROFILE))
        for path in BENCH_PROFILE.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    }
    assert extra == {*SHARED_FILES, "vehicle.yaml", "README.md"}


def test_bench_profile_parses_and_keeps_the_vehicle_identity():
    bench = load_profile(BENCH_PROFILE)
    example = load_profile(EXAMPLE_PROFILE)

    # Same car: changing the id would change every subject and every
    # pit-side setting along with it (docs/plan/PHASE4.md, P4.1).
    assert bench.vehicle.vehicle.id == example.vehicle.vehicle.id
    assert bench.catalog_hash == example.catalog_hash


def test_only_the_transports_differ():
    bench = load_profile(BENCH_PROFILE).vehicle
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
    assert serial.port != example_serial.port
    # The driver stays attached: serial source refs resolve against the
    # driver name when one is present, so dropping it would invalidate every
    # `serial0:um980.RMC.*` entry -- and it is also what lets the collector
    # accept the pit's RTCM write-back, which is reverse-channel traffic the
    # bench is there to measure.
    assert serial.driver is not None
    assert serial.driver.name == example_serial.driver.name == "um980"
    assert serial.driver.config.configure_on_start is False

    assert bench.host == example.host


def test_runtime_catalog_is_identical_apart_from_registry_bookkeeping(tmp_path: Path):
    """Same channels, same ids, same source refs, same wire encodings."""
    bench = build_runtime_catalog(
        load_profile(BENCH_PROFILE), state_path=tmp_path / "bench.json", created_unix_ms=1
    )
    example = build_runtime_catalog(
        load_profile(EXAMPLE_PROFILE), state_path=tmp_path / "example.json", created_unix_ms=1
    )

    assert bench.channel_ids == example.channel_ids
    assert bench.source_map == example.source_map
    assert bench.catalog_hash == example.catalog_hash
    # Registry generation is per-profile state, not content -- see this
    # profile's README on why the bench has its own generation sequence.
    assert bench.registry.registry_seq == example.registry.registry_seq == 1
    assert bench.registry.SerializeToString() == example.registry.SerializeToString()


def test_registry_generation_is_tracked_per_profile(tmp_path: Path):
    """The consequence the README warns about, asserted rather than assumed.

    Two profiles with identical catalogs still keep separate generation
    counters, because the counter is per state file. A pit that has already
    seen the example profile's generations will therefore reject bench
    batches until it rescans the catalog subject.
    """
    state = tmp_path / "bench.json"
    first = build_runtime_catalog(load_profile(BENCH_PROFILE), state_path=state)
    again = build_runtime_catalog(load_profile(BENCH_PROFILE), state_path=state)
    other = build_runtime_catalog(
        load_profile(BENCH_PROFILE), state_path=tmp_path / "elsewhere.json"
    )

    assert first.registry.registry_seq == again.registry.registry_seq
    assert other.registry.registry_seq == 1
