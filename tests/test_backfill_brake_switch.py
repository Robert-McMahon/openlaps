"""The fusion that rebuilds brake switch history from the pin voltage and the ECU."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from backfill_brake_switch import fuse  # noqa: E402

T0 = datetime(2026, 11, 13, 10, 30, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _edges(rows):
    return [
        (round((time - T0).total_seconds(), 3), value)
        for (time, value), (_, before) in zip(rows[1:], rows, strict=False)
        if value != before
    ]


def test_the_pin_voltage_times_the_edges_and_a_lagging_ecu_does_not_move_them():
    voltage = [(_at(0), 0.007), (_at(3.68), 5.0), (_at(6.43), 0.18), (_at(6.54), 0.007)]
    ecu = [(_at(3.65), 0), (_at(3.88), 1), (_at(6.33), 1), (_at(6.56), 0)]

    fused = fuse(voltage, ecu, threshold=4.5)

    assert _edges(fused.rows) == [(3.68, 1), (6.43, 0)]
    assert (fused.applications, fused.applications_from_ecu) == (1, 0)
    assert (fused.releases, fused.releases_from_ecu) == (1, 0)


def test_an_application_whose_voltage_edge_was_lost_is_taken_from_the_ecu():
    # The 5 V sample never arrived: the pin record goes from released straight
    # to the decay on release, while the ECU shows the pedal held for 3 s.
    voltage = [(_at(0), 0.007), (_at(5.0), 0.007), (_at(8.36), 0.6), (_at(8.47), 0.008)]
    ecu = [(_at(4.9), 0), (_at(5.15), 1), (_at(5.64), 1), (_at(8.3), 1), (_at(8.55), 0)]

    fused = fuse(voltage, ecu, threshold=4.5)

    assert _edges(fused.rows) == [(5.15, 1), (8.36, 0)]
    assert (fused.applications, fused.applications_from_ecu) == (1, 1)
    assert fused.releases_from_ecu == 0


def test_a_release_whose_voltage_edge_was_lost_is_taken_from_the_ecu():
    voltage = [(_at(0), 0.007), (_at(2.0), 5.0), (_at(7.0), 0.007)]
    ecu = [(_at(2.2), 1), (_at(3.9), 1), (_at(4.2), 0), (_at(4.5), 0)]

    fused = fuse(voltage, ecu, threshold=4.5)

    assert _edges(fused.rows) == [(2.0, 1), (4.2, 0)]
    assert fused.releases_from_ecu == 1


def test_the_level_is_filled_while_it_holds_but_not_across_a_gap_in_the_record():
    voltage = [(_at(0), 0.007), (_at(1.0), 5.0), (_at(2.0), 5.0), (_at(60.0), 0.007)]

    rows = fuse(voltage, [], threshold=4.5).rows

    times = [round((time - T0).total_seconds(), 1) for time, _ in rows]
    assert times == [0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 60.0]
    assert [value for _, value in rows] == [0] * 5 + [1] * 6 + [0]
