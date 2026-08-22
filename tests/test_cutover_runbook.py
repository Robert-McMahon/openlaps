"""P5.7's runbook is the go/no-go record, and these are its load-bearing parts.

`docs/CUTOVER_RUNBOOK.md` is prose, so most of it cannot be tested and
should not be. What *can* drift silently is the same set of things
`tests/test_bench_runbook.py` guards in its document: the places where the
runbook names something the code also names, and the places where a claim
in the document rests on a fact elsewhere in the repository.

Three groups here:

* **Gate provenance.** Every go/no-go gate cites a document or manifest,
  and a gate marked NOT RUN is only honest while the artefact that would
  close it does not exist. The moment `docs/bench/dropout.md` or
  `docs/bench/range.md` lands, the runbook's gate table is stale and these
  tests say so — which is the point: the runbook must be re-decided, not
  quietly outlived.
* **The honest statements.** "No tested rollback" is a phrase the brief
  requires verbatim, and the data-safety claim rests on the vehicle
  stream's retention limits, quoted as numbers an operator will read.
* **The literals an operator types or reads back** — ports, stream names,
  datasource uids, the dashboard, health counter names — against the code
  and config that define them. A drifted counter name here does not break
  anything; it sends a tired person grepping JSON for a key that no longer
  exists.

No hardware, no docker: everything here runs in CI.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLE_PROFILE

from agent import publisher
from core.catalog import build_runtime_catalog
from core.config import load_profile
from pit.ingest_writer.health import HealthState as IngestHealth
from pit.live_decoder.health import HealthState as LiveHealth
from pit.ntrip_client.health import HealthState as NtripHealth

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import link_probe  # noqa: E402

REPO = Path(__file__).parents[1]
RUNBOOK = REPO / "docs" / "CUTOVER_RUNBOOK.md"
PIT_COMPOSE = REPO / "deploy" / "pit-compose.yaml"
DATASOURCES = REPO / "deploy" / "pit-config" / "grafana" / "provisioning" / "datasources"
CAR_DASHBOARD = REPO / "deploy" / "pit-config" / "grafana" / "dashboards" / "car.json"


# --- gate provenance ---------------------------------------------------------


@pytest.mark.parametrize(
    "artefact",
    [
        "docs/bench/timing-parity.md",
        "docs/bench/timing-parity.manifest.json",
        "docs/bench/2026-08-21-steady-state/summary.md",
    ],
)
def test_closed_gates_cite_artefacts_that_exist(artefact: str):
    """A gate's provenance is a path; a moved artefact is an unclosed gate."""
    assert (REPO / artefact).exists()
    assert artefact in RUNBOOK.read_text()


@pytest.mark.parametrize("artefact", ["docs/bench/dropout.md", "docs/bench/range.md"])
def test_open_gates_are_still_open(artefact: str):
    """The runbook marks P4.4 and P4.5 NOT RUN because these do not exist.

    When one of them lands this test fails on purpose: the gate table, the
    caveats it restates, and probably the go decision itself need
    re-deciding, and a silent stale PASS/NOT-RUN table is exactly the drift
    this file exists to catch.
    """
    assert not (REPO / artefact).exists(), (
        f"{artefact} now exists -- the runbook's gate table marks it NOT RUN "
        "and is stale. Update docs/CUTOVER_RUNBOOK.md, then this test."
    )
    assert artefact.rsplit("/", 1)[1] in RUNBOOK.read_text()


# --- the honest statements ---------------------------------------------------


def test_states_plainly_there_is_no_tested_rollback():
    """The brief requires the words, not a paraphrase an optimist can misread."""
    assert "no tested rollback" in RUNBOOK.read_text()


def test_data_safety_claim_quotes_the_real_retention_limits():
    """ "The car keeps recording" is bounded by TELE's caps, quoted as numbers."""
    assert publisher.DEFAULT_TELE_MAX_BYTES == 8 * 1024**3
    assert publisher.DEFAULT_TELE_MAX_AGE_S == 72 * 3600
    assert "8 GiB / 72 h" in RUNBOOK.read_text()


def test_the_silent_thirty_spot_check_targets_a_real_channel(tmp_path: Path):
    """§5's SQL names `car.boost_button`; a catalog rename would orphan it."""
    catalog = build_runtime_catalog(
        load_profile(EXAMPLE_PROFILE), state_path=tmp_path / "registry.json"
    )
    assert "car.boost_button" in catalog.channel_ids
    assert "car.boost_button" in RUNBOOK.read_text()


# --- the literals an operator types or reads back ----------------------------


@pytest.mark.parametrize(
    "literal",
    [
        link_probe.DEFAULT_STREAM["vehicle"],  # TELE
        link_probe.DEFAULT_STREAM["pit"],  # TELE_VEHICLE
        "CMD",
        "openlaps-agent",
    ],
)
def test_runbook_names_what_the_code_names(literal: str):
    assert literal in RUNBOOK.read_text()


@pytest.mark.parametrize("port", [8080, 8081, 8082, 8083, 3000])
def test_health_and_ui_ports_match_the_compose_file(port: int):
    """The runbook sends the operator to `:PORT/...`; compose publishes them."""
    assert f'"{port}:{port}"' in PIT_COMPOSE.read_text()
    assert f":{port}" in RUNBOOK.read_text()


@pytest.mark.parametrize("uid", ["timescale", "mqtt-live"])
def test_datasource_uids_match_provisioning(uid: str):
    assert f"uid: {uid}" in (DATASOURCES / f"{uid}.yaml").read_text()
    assert uid in RUNBOOK.read_text()


def test_the_dashboard_the_operator_opens_exists_under_that_name():
    dashboard = json.loads(CAR_DASHBOARD.read_text())
    text = RUNBOOK.read_text()
    assert f"`{dashboard['uid']}`" in text
    assert dashboard["title"] in text


@pytest.mark.parametrize(
    ("state", "counters"),
    [
        (
            IngestHealth,
            [
                "unknown_seq_batches",
                "bad_version_batches",
                "dropped_flushes",
                "samples_dropped",
                "db_errors",
                "lag_ms",
                "wall_lag_ms",
                "agent_status_age_s",
                "stalled",
                "nats_reconnects",
            ],
        ),
        (LiveHealth, ["mqtt_drops", "aggregate_sheds", "unmatched_rules"]),
        (NtripHealth, ["reconnects"]),
    ],
    ids=["ingest-writer", "live-decoder", "ntrip-client"],
)
def test_health_counters_the_runbook_reads_exist(state: type, counters: list[str]):
    """Each counter the operator is told to read is a key `/health` serves."""
    snapshot = state().snapshot()
    text = RUNBOOK.read_text()
    for counter in counters:
        assert counter in snapshot, f"{state.__module__} no longer serves {counter}"
        assert counter in text


def test_per_channel_suppression_is_still_reported_that_way():
    """§5 step 2 reads `suppressed` per channel; it lives under `channels`."""
    live = LiveHealth()
    live.note_suppressed("car.rpm")
    assert live.snapshot()["channels"]["car.rpm"]["suppressed"] == 1
    assert "suppressed" in RUNBOOK.read_text()


def test_runbook_references_only_files_that_exist():
    """Prose rots. A path is the part of it that can be checked.

    The two open-gate artefacts are cited precisely *because* they do not
    exist; `test_open_gates_are_still_open` owns them.
    """
    deliberately_absent = {"docs/bench/dropout.md", "docs/bench/range.md"}
    text = RUNBOOK.read_text()
    candidates = (
        set(re.findall(r"(?:docs|deploy|tools|tests|profiles|src|firmware)/[\w./-]+", text))
        - deliberately_absent
    )
    missing = sorted(
        path for path in candidates if "<" not in path and not (REPO / path.rstrip(".")).exists()
    )
    assert not missing, f"docs/CUTOVER_RUNBOOK.md points at paths that do not exist: {missing}"
