"""deploy/provision_pit_streams.py: the pit's age cap is never the shorter one.

nats-server resumes a source from the newest sourced message the pit stream
still holds. An empty pit stream therefore restarts from the vehicle's
oldest message, and a pit cap shorter than the vehicle's turns a pit outage
into a re-ingest of data Timescale already archived, as duplicate rows: the
re-sourced messages get new pit sequence numbers above the ingest cursor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "deploy"))

import provision_pit_streams as provision  # noqa: E402

H = 3600.0


def test_the_default_pit_cap_is_not_shorter_than_the_vehicle_default():
    assert provision.DEFAULT_MAX_AGE_H >= 72
    assert provision.pit_stream_config().max_age == provision.DEFAULT_MAX_AGE_H * H


@pytest.mark.parametrize(
    ("pit", "vehicle"),
    [(72 * H, 72 * H), (96 * H, 72 * H), (0, 72 * H), (0, 0)],
)
def test_a_pit_cap_at_least_the_vehicles_raises_no_warning(pit, vehicle):
    assert provision.retention_shortfall(pit, vehicle) is None


@pytest.mark.parametrize(("pit", "vehicle"), [(24 * H, 72 * H), (72 * H, 0)])
def test_a_shorter_pit_cap_explains_the_duplicate_rows_it_would_cause(pit, vehicle):
    message = provision.retention_shortfall(pit, vehicle)
    assert message is not None
    assert "duplicate rows" in message
    assert "OPENLAPS_PIT_STREAM_MAX_AGE_H" in message
    assert "unlimited" in message or "72 h" in message


def test_an_unreachable_vehicle_makes_the_check_advisory():
    class Unreachable:
        def jetstream(self, **_):
            raise ConnectionError("leafnode down")

    import asyncio

    assert asyncio.run(provision.source_max_age_s(Unreachable(), "TELE", "veh")) is None
