"""Tests for the lean collector-to-pipeline sample object."""

from dataclasses import fields

from core.samples import Sample


def test_sample_is_a_slotted_value_object():
    sample = Sample(
        source_ref="can0:haltech.ENGINE1.ENGINE_SPEED",
        t_mono_ns=123,
        t_wall_ms=456.75,
        value=7000.0,
    )

    assert [field.name for field in fields(sample)] == [
        "source_ref",
        "t_mono_ns",
        "t_wall_ms",
        "value",
    ]
    assert sample.source_ref == "can0:haltech.ENGINE1.ENGINE_SPEED"
    assert sample.t_mono_ns == 123
    assert sample.t_wall_ms == 456.75
    assert sample.value == 7000.0
    assert not hasattr(sample, "__dict__")
