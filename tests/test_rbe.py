"""Tests for the pure per-channel report-by-exception filter."""

import pytest

from core.config import RbeConfig
from core.rbe import RbeFilter
from core.samples import Sample, SampleValue


def _sample(value: SampleValue, t_mono_ns: int) -> Sample:
    return Sample("can0:ecu.ENGINE.RPM", t_mono_ns, t_mono_ns / 1_000_000, value)


def test_deadband_suppresses_until_value_moves_beyond_last_sent_value():
    rbe = RbeFilter()
    policy = RbeConfig(deadband=1.0)

    assert rbe.accept(7, _sample(10.0, 0), policy)
    assert not rbe.accept(7, _sample(10.5, 1_000), policy)
    assert not rbe.accept(7, _sample(11.0, 2_000), policy)
    assert rbe.accept(7, _sample(11.1, 3_000), policy)


@pytest.mark.parametrize("nonnumeric", [True, "10.0"])
def test_deadband_rejects_first_nonnumeric_sample_without_poisoning_state(
    nonnumeric: SampleValue,
):
    rbe = RbeFilter()
    policy = RbeConfig(deadband=1.0)

    with pytest.raises(TypeError, match="deadband requires numeric sample values"):
        rbe.accept(7, _sample(nonnumeric, 0), policy)

    assert rbe.accept(7, _sample(10.0, 1_000), policy)


def test_min_interval_caps_rate_from_last_sent_sample():
    rbe = RbeFilter()
    policy = RbeConfig(min_interval="10ms")

    assert rbe.accept(7, _sample(10.0, 1_000_000), policy)
    assert not rbe.accept(7, _sample(20.0, 10_999_999), policy)
    assert rbe.accept(7, _sample(30.0, 11_000_000), policy)


def test_max_interval_forces_heartbeat_before_other_suppression_rules():
    rbe = RbeFilter()
    policy = RbeConfig(deadband=1.0, min_interval="10ms", max_interval="10ms")

    assert rbe.accept(7, _sample(10.0, 1_000_000), policy)
    assert rbe.accept(7, _sample(10.0, 11_000_000), policy)


def test_max_interval_accepts_before_deadband_validates_current_value():
    rbe = RbeFilter()
    policy = RbeConfig(deadband=1.0, max_interval="10ms")

    assert rbe.accept(7, _sample(10.0, 0), policy)
    assert rbe.accept(7, _sample("heartbeat", 10_000_000), policy)


def test_min_interval_suppresses_before_deadband_validates_current_value():
    rbe = RbeFilter()
    policy = RbeConfig(deadband=1.0, min_interval="10ms")

    assert rbe.accept(7, _sample(10.0, 0), policy)
    assert not rbe.accept(7, _sample("rate-limited", 9_999_999), policy)


def test_no_policy_samples_pass_through_without_stateful_suppression():
    rbe = RbeFilter()

    assert rbe.accept(7, _sample(10.0, 1_000_000), None)
    assert rbe.accept(7, _sample(10.0, 1_000_000), None)
    assert rbe.suppressed_count == 0


def test_suppressed_count_accumulates_across_channels_and_rules():
    rbe = RbeFilter()
    deadband = RbeConfig(deadband=1.0)
    rate_cap = RbeConfig(min_interval="10ms")

    assert rbe.accept(7, _sample(10.0, 0), deadband)
    assert not rbe.accept(7, _sample(10.5, 20_000_000), deadband)
    assert rbe.accept(8, _sample(20.0, 0), rate_cap)
    assert not rbe.accept(8, _sample(30.0, 1_000_000), rate_cap)
    assert rbe.suppressed_count == 2
