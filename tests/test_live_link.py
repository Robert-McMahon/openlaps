"""The car-to-pit live link: wire format, the car's sampler, the pit's end."""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from urllib.parse import urlparse

import aiomqtt
import pytest

from agent.agent import AgentSettings
from agent.live import HELLO_TIMEOUT_S, LiveSampler, LiveSender, Subscribers
from core import live_wire
from core.live_wire import Selection, WireError
from core.pb import telemetry_pb2 as pb
from pit.live_decoder.config import load_live_config
from pit.live_decoder.demand import Demand, parse_subscribe_log
from pit.live_decoder.direct import (
    DirectLink,
    DirectSource,
    LiveMessage,
    parse_car_addrs,
    selection_for,
)
from pit.live_decoder.health import HealthState
from pit.live_decoder.resend import Resender
from pit.live_decoder.service import LiveDecoderSettings, LiveUpdate, MqttSink
from pit.registry_cache import MSG_TYPE_HEADER, MSG_TYPE_REGISTRY

SEQ = 7
RPM, COOLANT, GEAR, LAT = 1, 2, 3, 2_147_483_700


def _batch(epoch_ms: int, *samples: tuple[int, int, str, object], seq: int = SEQ) -> bytes:
    batch = pb.SampleBatch(registry_seq=seq, batch_epoch_unix_ms=epoch_ms, format_version=1)
    for channel_id, offset_us, kind, value in samples:
        sample = batch.samples.add(channel_id=channel_id, t_offset_us=offset_us)
        setattr(sample, kind, value)
    return batch.SerializeToString()


def _values(frames: list[bytes]) -> dict[int, object]:
    out: dict[int, object] = {}
    for frame in frames:
        batch = pb.SampleBatch.FromString(frame)
        assert batch.registry_seq == SEQ and batch.format_version == 1
        for sample in batch.samples:
            out[sample.channel_id] = getattr(sample, sample.WhichOneof("value"))
    return out


# -- wire format ---------------------------------------------------------------


@pytest.mark.parametrize("secret", [None, "pit-wall"])
def test_hello_and_frame_round_trip(secret: str | None) -> None:
    key = live_wire.derive_key(secret)
    selection = Selection({RPM: 100, COOLANT: 100, LAT: 0}, refresh_ms=1000)
    (datagram,) = live_wire.encode_hello(
        session=0xABCDEF, counter=3, registry_seq=SEQ, selection=selection, key=key
    )
    hello = live_wire.decode_hello(datagram, key)
    assert (hello.session, hello.counter, hello.registry_seq) == (0xABCDEF, 3, SEQ)
    assert (hello.part, hello.parts, hello.refresh_ms) == (0, 1, 1000)
    assert hello.intervals == selection.intervals
    assert hello.selection_version == selection.version

    frame = live_wire.encode_frame(11, 22, b"batch-bytes", key)
    assert live_wire.decode_frame(frame, key) == (11, 22, b"batch-bytes")
    assert len(frame) == len(b"batch-bytes") + live_wire.frame_overhead(key)


def test_a_signed_link_refuses_unsigned_tampered_and_mistyped_datagrams() -> None:
    key = live_wire.derive_key("pit-wall")
    frame = live_wire.encode_frame(1, 2, b"payload", key)
    with pytest.raises(WireError):
        live_wire.decode_frame(live_wire.encode_frame(1, 2, b"payload", None), key)
    with pytest.raises(WireError):
        live_wire.decode_frame(frame[:-1] + bytes([frame[-1] ^ 1]), key)
    with pytest.raises(WireError):
        live_wire.decode_frame(frame, live_wire.derive_key("someone-else"))
    with pytest.raises(WireError):
        live_wire.decode_hello(frame, key)
    with pytest.raises(WireError):
        live_wire.decode_frame(b"not ours at all", None)


def test_a_selection_too_big_for_one_datagram_is_sent_in_parts() -> None:
    selection = Selection({2_147_483_648 + n: 100 for n in range(400)}, refresh_ms=1000)
    datagrams = live_wire.encode_hello(
        session=1, counter=1, registry_seq=SEQ, selection=selection, key=None
    )
    assert len(datagrams) > 1
    assert all(len(datagram) <= live_wire.MAX_DATAGRAM for datagram in datagrams)
    merged: dict[int, int] = {}
    for datagram in datagrams:
        hello = live_wire.decode_hello(datagram, None)
        assert hello.parts == len(datagrams)
        merged.update(hello.intervals)
    assert merged == selection.intervals


# -- the car's sampler -----------------------------------------------------------


def _sampler() -> LiveSampler:
    sampler = LiveSampler(SEQ)
    sampler.select(Selection({RPM: 100, COOLANT: 100, GEAR: 0}, refresh_ms=1000))
    return sampler


def test_only_selected_channels_are_sent_and_only_their_newest_value() -> None:
    sampler = _sampler()
    sampler.observe(_batch(1_000, (RPM, 0, "u", 3000), (RPM, 9_000, "u", 3100), (99, 0, "u", 1)))
    assert _values(sampler.frames(0)) == {RPM: 3100}


def test_a_steady_value_is_sent_once_per_refresh_not_once_per_frame() -> None:
    """Rob's coolant example: constant for ten seconds must not cross at 10 Hz."""
    sampler = _sampler()
    sent = 0
    for tick in range(100):  # ten seconds of 10 Hz frames
        # The catalog's own filter still lets a heartbeat through; same value.
        sampler.observe(_batch(1_000 + tick * 100, (COOLANT, 0, "f", 88.5)))
        sent += COOLANT in _values(sampler.frames(tick * 100.0))
    assert 10 <= sent <= 11


def test_a_changing_value_is_sent_no_faster_than_its_interval() -> None:
    sampler = _sampler()
    sent = 0
    for tick in range(100):  # one second of 100 Hz changes, sampled every 10 ms
        sampler.observe(_batch(1_000 + tick * 10, (RPM, 0, "u", 3000 + tick)))
        sent += RPM in _values(sampler.frames(tick * 10.0))
    assert 10 <= sent <= 11


def test_a_change_goes_in_the_next_frame_and_carries_its_capture_time() -> None:
    sampler = _sampler()
    sampler.observe(_batch(5_000, (GEAR, 0, "i", 3)))
    sampler.frames(0)
    sampler.observe(_batch(5_040, (GEAR, 2_500, "i", 4)))
    (frame,) = sampler.frames(50.0)
    batch = pb.SampleBatch.FromString(frame)
    (sample,) = batch.samples
    assert sample.i == 4
    assert batch.batch_epoch_unix_ms * 1000 + sample.t_offset_us == 5_040_000 + 2_500


def test_a_value_stuck_at_nan_is_not_a_change_every_frame() -> None:
    sampler = _sampler()
    sent = 0
    for tick in range(20):
        sampler.observe(_batch(1_000 + tick * 100, (COOLANT, 0, "f", math.nan)))
        sent += COOLANT in _values(sampler.frames(tick * 100.0))
    assert sent <= 3


def test_a_new_selection_or_listener_gets_every_current_value_at_once() -> None:
    sampler = _sampler()
    sampler.observe(_batch(1_000, (RPM, 0, "u", 3000), (COOLANT, 0, "f", 88.5)))
    assert set(_values(sampler.frames(0))) == {RPM, COOLANT}
    assert sampler.frames(100.0) == []
    sampler.forget_sent()
    assert set(_values(sampler.frames(200.0))) == {RPM, COOLANT}


def test_a_wide_first_send_is_split_into_datagram_sized_batches() -> None:
    sampler = LiveSampler(SEQ, max_batch_bytes=400)
    sampler.select(Selection({n: 100 for n in range(1, 101)}, refresh_ms=1000))
    sampler.observe(_batch(1_000, *[(n, n, "d", n * 1.5) for n in range(1, 101)]))
    frames = sampler.frames(0)
    assert len(frames) > 1
    assert all(len(frame) <= 400 for frame in frames)
    assert _values(frames) == {n: n * 1.5 for n in range(1, 101)}


def test_values_captured_hours_apart_do_not_overflow_the_offset() -> None:
    sampler = _sampler()
    sampler.observe(_batch(1_000, (GEAR, 0, "i", 2)))
    sampler.observe(_batch(1_000 + 3 * 3600 * 1000, (RPM, 0, "u", 900)))
    frames = sampler.frames(0)
    assert len(frames) == 2
    assert _values(frames) == {GEAR: 2, RPM: 900}


def test_batches_of_another_registry_generation_are_ignored() -> None:
    sampler = _sampler()
    sampler.observe(_batch(1_000, (RPM, 0, "u", 3000), seq=SEQ + 1))
    assert sampler.frames(0) == []


# -- who the car answers ---------------------------------------------------------


def _hello(session: int, counter: int, selection: Selection, seq: int = SEQ) -> live_wire.Hello:
    (datagram,) = live_wire.encode_hello(
        session=session, counter=counter, registry_seq=seq, selection=selection, key=None
    )
    return live_wire.decode_hello(datagram, None)


def test_one_pit_is_served_on_every_path_it_says_hello_from_until_it_goes_quiet() -> None:
    selection = Selection({RPM: 100})
    subscribers = Subscribers(SEQ)
    tunnel, radio, stranger = ("100.1.1.1", 5000), ("192.168.1.9", 5001), ("10.0.0.5", 5002)

    assert subscribers.hello(_hello(1, 1, selection), tunnel, 0.0) == selection
    assert subscribers.hello(_hello(1, 1, selection), radio, 0.0) is None  # same selection
    assert subscribers.addrs == [tunnel, radio]

    # A second pit cannot take the link over, and a replayed HELLO is refused.
    assert subscribers.hello(_hello(2, 9, Selection({COOLANT: 100})), stranger, 1.0) is None
    assert subscribers.hello(_hello(1, 0, selection), stranger, 1.0) is None
    assert subscribers.addrs == [tunnel, radio]
    assert subscribers.rejected == 2

    # One path dies; the other keeps the session.
    subscribers.hello(_hello(1, 2, selection), radio, HELLO_TIMEOUT_S)
    subscribers.expire(HELLO_TIMEOUT_S + 1.0)
    assert subscribers.addrs == [radio]

    # Silence ends the session, and then another pit may have it.
    subscribers.expire(3 * HELLO_TIMEOUT_S)
    assert subscribers.addrs == []
    other = Selection({COOLANT: 100})
    assert subscribers.hello(_hello(2, 1, other), stranger, 3 * HELLO_TIMEOUT_S) == other


def test_a_hello_for_another_registry_generation_is_not_served() -> None:
    subscribers = Subscribers(SEQ)
    assert (
        subscribers.hello(_hello(1, 1, Selection({RPM: 100}), seq=SEQ - 1), ("h", 1), 0.0) is None
    )
    assert subscribers.addrs == []
    assert subscribers.registry_mismatches == 1


# -- the pit's end -----------------------------------------------------------------


def _registry() -> pb.ChannelRegistry:
    registry = pb.ChannelRegistry(registry_seq=SEQ, vehicle_id="car-1")
    for channel_id, name in ((RPM, "car.rpm"), (COOLANT, "car.coolant"), (GEAR, "lap.number")):
        registry.channels.add(id=channel_id, name=name)
    registry.channels.add(id=50, name="sys.unselected")
    return registry


def _live_config(tmp_path: Path, resend_s: float = 1):
    path = tmp_path / "live.yaml"
    path.write_text(
        f"defaults:\n  max_hz: 10\n  resend_s: {resend_s}\n"
        "channels:\n  - match: car.rpm\n    max_hz: 20\n  - match: car.*\n"
        "  - match: lap.*\n    max_hz: 0\n",
        encoding="utf-8",
    )
    return load_live_config(path)


def test_the_selection_is_the_live_view_resolved_against_the_registry(tmp_path: Path) -> None:
    selection = selection_for(_live_config(tmp_path), _registry())
    assert selection == Selection({RPM: 50, COOLANT: 100, GEAR: 0}, refresh_ms=1000)
    # With repeats turned off a lost frame must still heal eventually.
    assert selection_for(_live_config(tmp_path, resend_s=0), _registry()).refresh_ms == 5000


def test_car_addresses_parse_with_a_default_port() -> None:
    assert parse_car_addrs("openlaps-car, 192.168.12.222:7500,") == (
        ("openlaps-car", live_wire.DEFAULT_PORT),
        ("192.168.12.222", 7500),
    )
    with pytest.raises(ValueError, match="bad live car address"):
        parse_car_addrs("car:notaport")


def test_direct_source_needs_a_car_address(tmp_path: Path) -> None:
    env = {"OPENLAPS_LIVE_SOURCE": "direct", "OPENLAPS_NATS_URL": "nats://x"}
    with pytest.raises(ValueError, match="OPENLAPS_LIVE_CAR_ADDRS"):
        LiveDecoderSettings.from_env(env, config_path=tmp_path / "live.yaml")
    env["OPENLAPS_LIVE_CAR_ADDRS"] = "192.168.12.222"
    settings = LiveDecoderSettings.from_env(env, config_path=tmp_path / "live.yaml")
    assert settings.car_addrs == (("192.168.12.222", live_wire.DEFAULT_PORT),)
    assert settings.live_stream == "TELE_VEHICLE"


def test_the_agent_leaves_the_live_link_off_unless_given_a_port(tmp_path: Path) -> None:
    assert AgentSettings.from_env({"OPENLAPS_PROFILE": str(tmp_path)}).live_port == 0
    settings = AgentSettings.from_env(
        {"OPENLAPS_PROFILE": str(tmp_path), "OPENLAPS_LIVE_PORT": "7423", "OPENLAPS_LIVE_KEY": "k"}
    )
    assert (settings.live_port, settings.live_key) == (7423, "k")


def test_duplicate_overtaken_and_unsigned_frames_are_dropped_and_a_restart_is_not() -> None:
    health = HealthState()
    link = DirectLink((), "pit-wall", health)
    key = live_wire.derive_key("pit-wall")
    addr = ("192.168.12.222", 7423)

    link.datagram_received(live_wire.encode_frame(5, 1, b"a", key), addr)
    link.datagram_received(live_wire.encode_frame(5, 2, b"b", key), addr)
    link.datagram_received(live_wire.encode_frame(5, 2, b"b", key), addr)  # second path
    link.datagram_received(live_wire.encode_frame(5, 1, b"a", key), addr)  # overtaken
    link.datagram_received(live_wire.encode_frame(5, 3, b"c", None), addr)  # unsigned
    link.datagram_received(live_wire.encode_frame(6, 1, b"d", key), addr)  # agent restarted

    got = [link.frames.get_nowait().data for _ in range(link.frames.qsize())]
    assert got == [b"a", b"b", b"d"]
    assert (health.live_frames, health.live_frame_duplicates, health.live_frame_rejects) == (
        3,
        2,
        1,
    )


class _FakeSubscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[LiveMessage] = asyncio.Queue()

    async def next_msg(self, timeout: float) -> LiveMessage:  # noqa: ASYNC109
        return await asyncio.wait_for(self.queue.get(), timeout=timeout)


def test_the_sourced_copy_stands_in_only_while_the_car_sends_no_frames() -> None:
    async def scenario() -> None:
        health = HealthState()
        link = DirectLink((), None, health)
        copy = _FakeSubscription()
        source = DirectSource(link, copy, health, fallback_after_s=0.3)
        stop = asyncio.Event()
        pump = asyncio.create_task(source._pump(stop))  # noqa: SLF001
        try:
            # Frames are flowing: the copy's batches are ignored, registries not.
            link.datagram_received(live_wire.encode_frame(1, 1, b"frame-1", None), ("h", 1))
            copy.queue.put_nowait(LiveMessage(b"stale-batch", {MSG_TYPE_HEADER: "batch"}))
            copy.queue.put_nowait(LiveMessage(b"registry", {MSG_TYPE_HEADER: MSG_TYPE_REGISTRY}))
            assert (await source.next_msg(timeout=1)).data == b"frame-1"
            registry = await source.next_msg(timeout=1)
            assert (registry.data, registry.headers) == (
                b"registry",
                {MSG_TYPE_HEADER: MSG_TYPE_REGISTRY},
            )
            assert not health.live_fallback

            # The car goes quiet for longer than the limit: the copy takes over.
            await asyncio.sleep(0.4)
            copy.queue.put_nowait(LiveMessage(b"copy-batch", {MSG_TYPE_HEADER: "batch"}))
            assert (await source.next_msg(timeout=1)).data == b"copy-batch"
            assert health.live_fallback

            # Frames return: the copy is dropped again.
            link.datagram_received(live_wire.encode_frame(1, 2, b"frame-2", None), ("h", 1))
            copy.queue.put_nowait(LiveMessage(b"late-batch", {MSG_TYPE_HEADER: "batch"}))
            assert (await source.next_msg(timeout=1)).data == b"frame-2"
            with pytest.raises(TimeoutError):
                await source.next_msg(timeout=0.2)
            assert not health.live_fallback
        finally:
            stop.set()
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    asyncio.run(scenario())


# -- both ends, over a real socket ---------------------------------------------------


@pytest.mark.parametrize("secret", [None, "pit-wall"])
def test_the_pit_asks_and_the_car_streams_changes_over_udp(secret: str | None) -> None:
    sender = LiveSender(registry_seq=SEQ, port=0, secret=secret, tick_ms=20, bind="127.0.0.1")
    sender.start()

    async def scenario() -> dict[int, list[object]]:
        health = HealthState()
        link = DirectLink((("127.0.0.1", sender.port),), secret, health)
        await link.open()
        link.select(SEQ, Selection({RPM: 20, COOLANT: 20}, refresh_ms=200))
        stop = asyncio.Event()
        hello = asyncio.create_task(link.run(stop))
        seen: dict[int, list[object]] = {RPM: [], COOLANT: []}
        try:
            deadline = time.monotonic() + 5.0
            tick = 0
            while time.monotonic() < deadline and len(seen[RPM]) < 10:
                tick += 1
                sender.submit_many(
                    [_batch(1_000 + tick * 20, (RPM, 0, "u", 3000 + tick), (COOLANT, 0, "f", 90.0))]
                )
                try:
                    message = await link.frames.get() if not link.frames.empty() else None
                    if message is None:
                        await asyncio.sleep(0.02)
                        continue
                except asyncio.QueueEmpty:
                    continue
                for channel_id, value in _values([message.data]).items():
                    seen[channel_id].append(value)
        finally:
            stop.set()
            await asyncio.gather(hello, return_exceptions=True)
            link.close()
        assert health.live_frame_rejects == 0
        return seen

    try:
        seen = asyncio.run(scenario())
    finally:
        sender.stop()
    assert len(seen[RPM]) >= 10
    assert seen[RPM] == sorted(seen[RPM])  # newest value each time, never an older one
    # The steady channel came once, then only as a refresh.
    assert 1 <= len(seen[COOLANT]) < len(seen[RPM])
    assert set(seen[COOLANT]) == {90.0}


def test_a_car_with_a_key_does_not_answer_a_pit_without_one() -> None:
    sender = LiveSender(registry_seq=SEQ, port=0, secret="pit-wall", tick_ms=20, bind="127.0.0.1")
    sender.start()

    async def scenario() -> int:
        health = HealthState()
        link = DirectLink((("127.0.0.1", sender.port),), None, health)
        await link.open()
        link.select(SEQ, Selection({RPM: 20}))
        await link._hello()  # noqa: SLF001
        sender.submit_many([_batch(1_000, (RPM, 0, "u", 3000))])
        await asyncio.sleep(0.3)
        link.close()
        return link.frames.qsize()

    try:
        assert asyncio.run(scenario()) == 0
        assert sender.bad_datagrams >= 1
        assert sender.frames_sent == 0
    finally:
        sender.stop()


# -- only what somebody is watching ---------------------------------------------------


def _demand() -> Demand:
    demand = Demand("car-1")
    demand.arm()
    return demand


def test_a_channel_nobody_subscribes_to_goes_idle_and_a_subscription_wakes_it() -> None:
    demand = _demand()
    assert demand.wanted("car.rpm")
    demand.published("car.rpm", False, 0.0)
    assert demand.wanted("car.rpm")  # one verdict is not enough
    demand.published("car.rpm", False, 2.0)
    assert not demand.wanted("car.rpm")
    demand.published("car.coolant", True, 2.0)
    assert demand.wanted("car.coolant")
    before = demand.version

    demand.subscribed("openlaps/other-car/car.rpm", 3.0)  # somebody else's car
    assert not demand.wanted("car.rpm") and demand.version == before
    demand.subscribed("openlaps/car-1/car.rpm", 3.0)
    assert demand.wanted("car.rpm") and demand.version == before + 1


def test_wildcard_subscriptions_wake_every_channel_they_cover() -> None:
    demand = _demand()
    for channel in ("car.rpm", "car.coolant", "lap.number"):
        demand.published(channel, False, 0.0)
        demand.published(channel, False, 2.0)
    demand.subscribed("openlaps/car-1/+", 3.0)
    assert demand.idle == frozenset()


def test_a_verdict_that_crossed_a_subscription_does_not_undo_it() -> None:
    demand = _demand()
    demand.published("car.rpm", False, 0.0)
    demand.subscribed("openlaps/car-1/car.rpm", 1.0)
    demand.published("car.rpm", False, 1.1)
    demand.published("car.rpm", False, 1.2)
    assert demand.wanted("car.rpm")
    # Long after, with the subscriber gone again, it does go idle.
    demand.published("car.rpm", False, 10.0)
    demand.published("car.rpm", False, 12.0)
    assert not demand.wanted("car.rpm")


def test_nothing_is_pruned_until_the_broker_is_known_to_report_subscriptions() -> None:
    demand = Demand("car-1")  # never armed: an older broker config
    for now in (0.0, 2.0, 4.0):
        demand.published("car.rpm", False, now)
    assert demand.wanted("car.rpm")


def test_a_new_broker_connection_wants_everything_again() -> None:
    demand = _demand()
    demand.published("car.rpm", False, 0.0)
    demand.published("car.rpm", False, 2.0)
    before = demand.version
    demand.reset()
    assert demand.wanted("car.rpm") and not demand.armed and demand.version == before + 1


def test_the_selection_leaves_out_idle_channels(tmp_path: Path) -> None:
    demand = _demand()
    demand.published("car.coolant", False, 0.0)
    demand.published("car.coolant", False, 2.0)
    selection = selection_for(_live_config(tmp_path), _registry(), demand.wanted)
    assert selection.intervals == {RPM: 50, GEAR: 0}


def test_the_subscribe_log_line_is_parsed() -> None:
    line = b"1795322264: grafana-x 0 openlaps/christine/car.rpm"
    assert parse_subscribe_log(line) == ("grafana-x", "openlaps/christine/car.rpm")
    assert parse_subscribe_log(b"garbage") is None


def test_an_unwatched_channel_is_not_repeated_and_its_last_value_is_forgotten(
    tmp_path: Path,
) -> None:
    config = _live_config(tmp_path)
    resender = Resender()
    resender.note(LiveUpdate("car.rpm", 1.0, 3000), 0.0)
    resender.note(LiveUpdate("car.coolant", 1.0, 88.5), 0.0)
    due = resender.due(config, 2.0, 2.0, lambda channel: channel != "car.coolant")
    assert [update.channel for update in due] == ["car.rpm"]
    # Watched again later: the stale reading must not come back.
    assert [u.channel for u in resender.due(config, 4.0, 4.0, lambda channel: True)] == ["car.rpm"]


def test_narrowing_the_selection_does_not_resend_what_the_pit_already_has() -> None:
    sampler = _sampler()
    sampler.observe(_batch(1_000, (RPM, 0, "u", 3000), (COOLANT, 0, "f", 88.5)))
    assert set(_values(sampler.frames(0))) == {RPM, COOLANT}
    sampler.select(Selection({RPM: 100}, refresh_ms=1000))
    assert sampler.frames(100.0) == []
    # Widened again: only the channel that had been dropped is sent at once.
    sampler.select(Selection({RPM: 100, COOLANT: 100}, refresh_ms=1000))
    assert set(_values(sampler.frames(200.0))) == {COOLANT}


def test_a_car_with_a_listener_and_nothing_to_send_says_so() -> None:
    sender = LiveSender(registry_seq=SEQ, port=0, tick_ms=20, bind="127.0.0.1")
    sender.start()

    async def scenario() -> list[pb.SampleBatch]:
        link = DirectLink((("127.0.0.1", sender.port),), None, HealthState())
        await link.open()
        link.select(SEQ, Selection({}))  # no dashboard open
        stop = asyncio.Event()
        hello = asyncio.create_task(link.run(stop))
        try:
            first = await asyncio.wait_for(link.frames.get(), timeout=3)
            second = await asyncio.wait_for(link.frames.get(), timeout=3)
        finally:
            stop.set()
            await asyncio.gather(hello, return_exceptions=True)
            link.close()
        return [pb.SampleBatch.FromString(message.data) for message in (first, second)]

    try:
        batches = asyncio.run(scenario())
    finally:
        sender.stop()
    assert all(batch.registry_seq == SEQ and not batch.samples for batch in batches)


def test_the_sink_learns_from_a_real_broker_who_is_watching(
    mosquitto_logging_url: str, tmp_path: Path
) -> None:
    broker = urlparse(mosquitto_logging_url)
    settings = LiveDecoderSettings(
        config_path=tmp_path / "unused.yaml",
        nats_url="nats://unused",
        mqtt_host=broker.hostname,
        mqtt_port=broker.port,
    )

    async def scenario() -> None:
        demand = Demand("car-1")
        sink = MqttSink(settings, "car-1", HealthState(), demand)
        stop = asyncio.Event()
        run = asyncio.create_task(sink.run(stop))

        async def publish_until(condition, what: str) -> None:
            deadline = time.monotonic() + 15.0
            while not condition():
                assert time.monotonic() < deadline, what
                for channel in ("car.rpm", "car.coolant"):
                    sink.submit(LiveUpdate(channel, 1.0, 1))
                await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(sink.connected.wait(), timeout=10)
            async with aiomqtt.Client(broker.hostname, broker.port) as gauge:
                await gauge.subscribe("openlaps/car-1/car.rpm")
                await publish_until(lambda: demand.armed, "never armed")
                # Only the rpm gauge is open: coolant goes idle, rpm does not.
                await publish_until(lambda: not demand.wanted("car.coolant"), "coolant stayed")
                assert demand.wanted("car.rpm")
                # A coolant gauge opens: wanted again without waiting for a publish.
                await gauge.subscribe("openlaps/car-1/car.coolant")
                deadline = time.monotonic() + 5.0
                while not demand.wanted("car.coolant"):
                    assert time.monotonic() < deadline, "subscription did not wake coolant"
                    await asyncio.sleep(0.05)
            # The dashboard closed: both go idle.
            await publish_until(lambda: len(demand.idle) == 2, "channels stayed after close")
        finally:
            stop.set()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


def test_backup_addresses_are_asked_only_while_the_preferred_one_delivers_nothing() -> None:
    class _Transport:
        def __init__(self) -> None:
            self.sent: list[tuple[str, int]] = []

        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            self.sent.append(addr)

    async def scenario() -> None:
        tunnel, radio = ("127.0.0.1", 7423), ("127.0.0.2", 7423)
        link = DirectLink((tunnel, radio), None, HealthState())
        transport = _Transport()
        link.connection_made(transport)  # type: ignore[arg-type]
        link.select(SEQ, Selection({RPM: 100}))

        async def asked() -> set[tuple[str, int]]:
            transport.sent.clear()
            await link._hello()  # noqa: SLF001
            return set(transport.sent)

        # Nothing heard yet: every path is tried.
        assert await asked() == {tunnel, radio}
        # The tunnel delivers: the backup is left alone.
        link.datagram_received(live_wire.encode_frame(1, 1, b"a", None), tunnel)
        assert await asked() == {tunnel}
        # The backup gets a frame here first and the tunnel's copy is the
        # duplicate: the tunnel is still delivering, so still only it.
        link.datagram_received(live_wire.encode_frame(1, 2, b"b", None), radio)
        link.datagram_received(live_wire.encode_frame(1, 2, b"b", None), tunnel)
        assert await asked() == {tunnel}
        # The tunnel goes quiet: both again, until it is heard from.
        link._primary_heard -= 10  # noqa: SLF001
        link.datagram_received(live_wire.encode_frame(1, 3, b"c", None), radio)
        assert await asked() == {tunnel, radio}
        link.datagram_received(live_wire.encode_frame(1, 4, b"d", None), tunnel)
        assert await asked() == {tunnel}

    asyncio.run(scenario())
