"""Caller-driven protobuf batching for post-RBE telemetry samples."""

from collections import defaultdict

from core.catalog import ChannelPolicy
from core.pb import telemetry_pb2 as pb
from core.samples import Sample, SampleValue


class Batcher:
    """Accumulate mapped samples and serialize non-empty source-class ticks."""

    def __init__(
        self,
        registry_seq: int,
        policies_by_id: dict[int, ChannelPolicy],
        tick_ms: int = 20,
    ) -> None:
        if not 10 <= tick_ms <= 50:
            raise ValueError("tick_ms must be between 10 and 50 ms")
        self._registry_seq = registry_seq
        self._policies_by_id = policies_by_id
        self._tick_us = tick_ms * 1_000
        self._tick_ns = tick_ms * 1_000_000
        self._pending: dict[str, list[tuple[int, Sample]]] = defaultdict(list)

    def add(self, source_class: str, channel_id: int, sample: Sample) -> None:
        """Add one mapped, post-RBE sample to its source-class tick."""
        if channel_id not in self._policies_by_id:
            raise KeyError(f"unknown channel_id {channel_id}")
        self._pending[source_class].append((channel_id, sample))

    def tick(self, batch_epoch_unix_ms: int, batch_epoch_mono_ns: int) -> dict[str, bytes]:
        """Consume one tick, returning all batches or discarding it on failure."""
        pending = self._pending
        self._pending = defaultdict(list)
        emitted: dict[str, bytes] = {}
        for source_class, samples in pending.items():
            batch = pb.SampleBatch(
                registry_seq=self._registry_seq,
                batch_epoch_unix_ms=batch_epoch_unix_ms,
                batch_epoch_mono_ns=batch_epoch_mono_ns,
                format_version=1,
            )
            for channel_id, sample in samples:
                offset_ns = sample.t_mono_ns - batch_epoch_mono_ns
                offset_us = offset_ns // 1_000
                if not 0 <= offset_ns < self._tick_ns:
                    raise ValueError(
                        f"sample timestamp offset {offset_us} us falls outside "
                        f"tick window [0, {self._tick_us}) us"
                    )
                wire_sample = batch.samples.add(channel_id=channel_id, t_offset_us=offset_us)
                policy = self._policies_by_id[channel_id]
                _encode_value(wire_sample, sample.value, policy)
            emitted[source_class] = batch.SerializeToString()

        return emitted


def _encode_value(wire_sample: pb.Sample, value: SampleValue, policy: ChannelPolicy) -> None:
    if policy.value_type in {pb.DOUBLE, pb.FLOAT}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{policy.name} requires a numeric value")
        if policy.value_type == pb.DOUBLE:
            wire_sample.d = float(value)
        else:
            wire_sample.f = float(value)
    elif policy.value_type in {pb.INT64, pb.UINT}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{policy.name} requires a numeric value")
        if policy.scale != 0:
            wire_value = round((value - policy.offset) / policy.scale)
        elif isinstance(value, int):
            wire_value = value
        else:
            raise TypeError(f"unscaled {policy.name} requires an integer value")
        if policy.value_type == pb.INT64:
            wire_sample.i = wire_value
        else:
            if wire_value < 0:
                raise ValueError(f"{policy.name} encoded to a negative UINT value")
            wire_sample.u = wire_value
    elif policy.value_type == pb.BOOL:
        if not isinstance(value, bool):
            raise TypeError(f"{policy.name} requires a bool value")
        wire_sample.b = value
    elif policy.value_type == pb.STRING:
        if not isinstance(value, str):
            raise TypeError(f"{policy.name} requires a string value")
        wire_sample.s = value
    else:
        raise ValueError(f"unsupported value type {policy.value_type}")
