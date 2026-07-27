"""Bounded drop-oldest queue tests."""

import pytest

from agent.queues import SampleQueue
from core.samples import Sample


def _sample(index: int) -> Sample:
    return Sample("can0:ecu.M.S", index, float(index), index)


def test_drain_returns_samples_in_arrival_order_and_empties():
    queue = SampleQueue("can0", maxlen=10)
    for index in range(3):
        queue.put(_sample(index))
    assert [sample.value for sample in queue.drain()] == [0, 1, 2]
    assert queue.drain() == []
    assert queue.dropped == 0


def test_overflow_sheds_oldest_and_counts():
    queue = SampleQueue("can0", maxlen=3)
    for index in range(5):
        queue.put(_sample(index))
    assert queue.dropped == 2
    assert [sample.value for sample in queue.drain()] == [2, 3, 4]
    # Counter is cumulative across drains.
    for index in range(4):
        queue.put(_sample(index))
    assert queue.dropped == 3


def test_source_class_is_carried_for_the_pipeline():
    assert SampleQueue("serial0").source_class == "serial0"


def test_zero_capacity_rejected():
    with pytest.raises(ValueError):
        SampleQueue("can0", maxlen=0)
