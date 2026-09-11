"""P4.2's runbook is a measurement contract, and these are its load-bearing parts.

`docs/BENCH_RUNBOOK.md` is prose, so most of it cannot be tested and should
not be. What *can* drift silently is the handful of places where the
document names something the code also names -- a counter, a stream, a
durable, a path -- and the places where a warning in the document rests on
a fact elsewhere in the repository. Both fail quietly: a renamed counter
does not break the probe, it makes it fall back to `/proc/net/dev` and
report a coarser number that looks identical in a CSV; a runbook citing a
file that moved sends the next operator looking for it.

Three groups here:

* **The nft rulesets** (`deploy/nft/`), parsed and asserted structurally.
  Direction is host-relative and mirrored between the two ends, and getting
  it backwards would invert `docs/LINK_BUDGET.md` §8's reverse-channel
  result -- the one figure in that section nobody has ever measured. This
  is also where "counting only" is enforced: a bench ruleset that can take
  a verdict is a ruleset that can drop telemetry.
* **The facts two of the runbook's warnings rest on.** Both warnings become
  wrong -- and worse, misleading -- the moment the underlying fact changes.
* **The literals the runbook quotes**, against the code that defines them.

No hardware, no docker, no nft binary: everything here runs in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLE_PROFILE

from core.catalog import build_runtime_catalog
from core.config import load_profile

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import bench_gps  # noqa: E402
import link_probe  # noqa: E402

REPO = Path(__file__).parents[1]
RUNBOOK = REPO / "docs" / "BENCH_RUNBOOK.md"
NFT = {
    "vehicle": REPO / "deploy" / "nft" / "bench-vehicle.nft",
    "pit": REPO / "deploy" / "nft" / "bench-pit.nft",
}
BENCH_HARDWARE = REPO / "tools" / "bench-hardware.yaml"
AGENT_UNIT = REPO / "deploy" / "systemd" / "openlaps-agent.service"
MIGRATION = REPO / "src" / "pit" / "db" / "migrations" / "001_init.sql"

_COUNTER = re.compile(r"^counter\s+(\S+)\s*\{")
_CHAIN = re.compile(r"^chain\s+(\S+)\s*\{")
_TYPE = re.compile(r"^type\s+filter\s+hook\s+(\w+)\s+priority\s+\w+;\s*policy\s+(\w+);$")
_RULE = re.compile(r'^tcp\s+(dport|sport)\s+(\d+)\s+counter\s+name\s+"(\w+)"$')


def parse_nft(path: Path) -> dict:
    """A deliberately strict reader for the two bench rulesets.

    Strict because the assertion these tests want to make is not "the file
    contains the right rules" but "the file contains *nothing else*". A
    permissive parser that skipped lines it did not recognise would let a
    `drop` through, which is the one outcome that matters.
    """
    table: str | None = None
    counters: set[str] = set()
    chains: dict[str, dict] = {}
    current: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "}":
            current = None
            continue
        if line.startswith("table "):
            table = line.split()[2]
            continue
        if match := _COUNTER.match(line):
            counters.add(match.group(1))
            current = None
            continue
        if line.startswith("comment "):
            continue
        if match := _CHAIN.match(line):
            current = match.group(1)
            chains[current] = {"hook": None, "policy": None, "rules": []}
            continue
        assert current is not None, f"{path.name}: stray line outside a chain: {line!r}"
        if match := _TYPE.match(line):
            chains[current]["hook"] = match.group(1)
            chains[current]["policy"] = match.group(2)
            continue
        match = _RULE.match(line)
        assert match is not None, (
            f"{path.name}: {line!r} in chain {current} is not a counter rule. "
            "These rulesets count and do nothing else."
        )
        chains[current]["rules"].append((match.group(1), int(match.group(2)), match.group(3)))
    return {"table": table, "counters": counters, "chains": chains}


def leafnode_listen_port() -> int:
    """The port the vehicle's nats-server listens for the pit on."""
    config = (REPO / "deploy" / "nats" / "vehicle.conf").read_text()
    block = re.search(r"leafnodes\s*\{(.*?)\n\}", config, re.S)
    assert block, "deploy/nats/vehicle.conf has no leafnodes block"
    port = re.search(r"listen:\s*\S+:(\d+)", block.group(1))
    assert port, "the leafnodes block declares no listener"
    return int(port.group(1))


# --- the nft rulesets --------------------------------------------------------


@pytest.mark.parametrize("role", sorted(NFT))
def test_ruleset_defines_exactly_the_counters_link_probe_reads(role: str):
    """A renamed counter does not fail; it silently coarsens every run.

    `link_probe` falls back to `/proc/net/dev` when it finds neither name,
    which is every byte on the interface rather than the leafnode's. The
    row records `wire_source`, so the run is not *wrong* -- but nothing
    about it looks different until someone reads that column.
    """
    assert parse_nft(NFT[role])["counters"] == {
        link_probe.DEFAULT_NFT_COUNTER_OUT,
        link_probe.DEFAULT_NFT_COUNTER_IN,
    }


@pytest.mark.parametrize("role", sorted(NFT))
def test_ruleset_only_counts(role: str):
    """No verdict, anywhere. `parse_nft` rejects a non-counter rule outright."""
    chains = parse_nft(NFT[role])["chains"]
    assert chains, f"{role}: no chains"
    for name, chain in chains.items():
        assert chain["policy"] == "accept", f"{role}/{name} policies {chain['policy']}"
        assert chain["hook"] == name, f"{role}/{name} hooks {chain['hook']}"


@pytest.mark.parametrize("role", sorted(NFT))
def test_ruleset_covers_every_path_a_leafnode_packet_can_take(role: str):
    """input/output for a host nats-server, forward for a containerised one.

    A packet traverses exactly one of those paths, so covering all three
    double-counts nothing and means the ruleset does not have to know how
    this host happens to run nats today.
    """
    assert set(parse_nft(NFT[role])["chains"]) == {"input", "output", "forward"}


@pytest.mark.parametrize("role", sorted(NFT))
def test_ruleset_counts_only_the_leafnode_port(role: str):
    port = leafnode_listen_port()
    for chain in parse_nft(NFT[role])["chains"].values():
        assert {rule[1] for rule in chain["rules"]} == {port}


def _port_to_counter(role: str) -> dict[str, str]:
    """The `dport`/`sport` -> counter mapping, asserted consistent across hooks."""
    mapping: dict[str, str] = {}
    for chain in parse_nft(NFT[role])["chains"].values():
        for keyword, _, counter in chain["rules"]:
            assert mapping.setdefault(keyword, counter) == counter, (
                f"{role}: {keyword} maps to two different counters"
            )
    return mapping


def test_direction_mapping_is_mirrored_between_the_two_ends():
    """The pit dials and the vehicle listens, so the port roles are opposite.

    Both hosts count into the same two names -- `link_probe` resolves the
    link direction from `--role` -- which means the mirroring has to live
    in the rulesets. Inverting it would swap forward and reverse in every
    derived series, including the reverse ratio `LINK_BUDGET.md` §8 exists
    to establish, and the result would be plausible in both directions.
    """
    vehicle, pit = _port_to_counter("vehicle"), _port_to_counter("pit")

    # Vehicle: a packet *to* 7422 is arriving, a packet *from* it is leaving.
    assert vehicle == {
        "dport": link_probe.DEFAULT_NFT_COUNTER_IN,
        "sport": link_probe.DEFAULT_NFT_COUNTER_OUT,
    }
    # Pit: the exact opposite, because it is the end that dials.
    assert pit == {
        "dport": link_probe.DEFAULT_NFT_COUNTER_OUT,
        "sport": link_probe.DEFAULT_NFT_COUNTER_IN,
    }
    assert all(vehicle[keyword] != pit[keyword] for keyword in vehicle)


def test_both_ends_use_one_table_the_runbook_can_tear_down():
    tables = {role: parse_nft(path)["table"] for role, path in NFT.items()}
    assert len(set(tables.values())) == 1, tables
    table = next(iter(tables.values()))
    assert f"nft delete table inet {table}" in RUNBOOK.read_text()


# --- the facts two warnings rest on ------------------------------------------


@pytest.mark.parametrize("hardware", [None, BENCH_HARDWARE], ids=["car", "bench-rig"])
def test_clock_health_is_mapped_on_the_car_and_on_the_bench(hardware: Path | None, tmp_path: Path):
    """P4.8 makes chrony the sole authority and its state ordinary telemetry."""
    catalog = build_runtime_catalog(
        load_profile(EXAMPLE_PROFILE, hardware), state_path=tmp_path / "registry.json"
    )
    assert {
        "sys.host.clock_offset_s",
        "sys.host.clock_source",
        "sys.host.clock_stratum",
        "sys.host.clock_root_dispersion_s",
    } <= catalog.channel_ids.keys()


def test_the_systemd_unit_still_hides_the_bench_gps_symlink():
    """Why §2 warns about running the agent under systemd for a bench run.

    `PrivateTmp=true` gives the agent its own `/tmp`, so it never sees the
    symlink `bench_gps.py` publishes -- presenting as a collector that
    produces no `position.*`, which looks exactly like the void-fix problem
    the feeder exists to solve. The warning is only worth carrying while
    both halves hold.
    """
    assert "PrivateTmp=true" in AGENT_UNIT.read_text()
    assert bench_gps.DEFAULT_LINK.startswith("/tmp/")
    serial = load_profile(EXAMPLE_PROFILE, BENCH_HARDWARE).vehicle.serial
    assert [source.port for source in serial] == [bench_gps.DEFAULT_LINK]


@pytest.mark.parametrize(
    "table", ["samples", "laps", "lap_sectors", "ingest_cursor", "channel_registry", "channel_map"]
)
def test_clean_slate_targets_tables_that_exist(table: str):
    """§8 names these by hand in SQL, so a rename has to fail somewhere."""
    assert f"CREATE TABLE {table} (" in MIGRATION.read_text()
    assert table in RUNBOOK.read_text()


@pytest.mark.parametrize(
    ("fixture", "frames"),
    [("candump-sample.log", 45861), ("candump-imu-sample.log", 1255)],
)
def test_runbook_quotes_the_fixture_sizes_correctly(fixture: str, frames: int):
    """§6 tells the operator how long a `canplayer -l i` loop is."""
    path = REPO / "tests" / "fixtures" / "candump" / fixture
    assert sum(1 for _ in path.open()) == frames
    assert f"{frames:,}" in RUNBOOK.read_text()


# --- the literals the runbook quotes -----------------------------------------


def _quoted_literals() -> list[str]:
    return [
        link_probe.DEFAULT_NFT_COUNTER_OUT,
        link_probe.DEFAULT_NFT_COUNTER_IN,
        link_probe.DEFAULT_STREAM["vehicle"],
        link_probe.DEFAULT_STREAM["pit"],
        link_probe.DEFAULT_CONSUMER,
        bench_gps.DEFAULT_LINK,
        *(str(value) for value in link_probe.MODEL_KBIT_S.values()),
    ]


@pytest.mark.parametrize("literal", _quoted_literals())
def test_runbook_names_what_the_code_names(literal: str):
    """Every one of these is a value an operator types or reads back.

    A drift here is not a broken run -- it is a run that purges the wrong
    stream, resets the wrong durable, or reconciles against a model figure
    that moved.
    """
    assert literal in RUNBOOK.read_text()


def test_runbook_references_only_files_that_exist():
    """Prose rots. A path is the part of it that can be checked."""
    text = RUNBOOK.read_text()
    candidates = set(re.findall(r"(?:docs|deploy|tools|tests|profiles|src)/[\w./-]+", text))
    missing = sorted(
        path for path in candidates if "<" not in path and not (REPO / path.rstrip(".")).exists()
    )
    assert not missing, f"docs/BENCH_RUNBOOK.md points at paths that do not exist: {missing}"
