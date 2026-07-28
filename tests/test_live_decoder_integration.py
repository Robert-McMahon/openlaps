"""Live-decoder integration against real NATS JetStream and Mosquitto."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

import aiomqtt
import nats
import pytest
from nats.js import api

from core.batcher import Batcher
from core.catalog import ChannelPolicy
from core.pb import telemetry_pb2 as pb
from core.samples import Sample
from pit.live_decoder.service import LiveDecoder, LiveDecoderSettings
from pit.registry_cache import MSG_TYPE_BATCH, MSG_TYPE_HEADER, MSG_TYPE_REGISTRY

VEHICLE = "integration-car"


async def _exercise(nats_url: str, mosquitto_url: str, config_path: Path) -> None:
    nc = await nats.connect(nats_url)
    js = nc.jetstream()
    await js.add_stream(api.StreamConfig(name="TELE", subjects=["tele.>"]))

    registry = pb.ChannelRegistry(registry_seq=4, vehicle_id=VEHICLE)
    registry.channels.add(
        id=1,
        name="car.coolant_temp",
        units="K",
        type=pb.UINT,
        scale=0.1,
    )
    registry.channels.add(id=2, name="car.unselected", type=pb.DOUBLE)
    await js.publish(
        f"tele.{VEHICLE}.catalog",
        registry.SerializeToString(),
        headers={MSG_TYPE_HEADER: MSG_TYPE_REGISTRY},
    )

    mqtt = urlparse(mosquitto_url)
    settings = LiveDecoderSettings(
        config_path=config_path,
        nats_url=nats_url,
        stream="TELE",
        mqtt_host=mqtt.hostname or "127.0.0.1",
        mqtt_port=mqtt.port or 1883,
        health_port=0,
        config_poll_s=0.05,
    )
    decoder = LiveDecoder(settings)
    stop = asyncio.Event()

    policies = {
        1: ChannelPolicy("car.coolant_temp", pb.UINT, None, scale=0.1),
        2: ChannelPolicy("car.unselected", pb.DOUBLE, None),
    }
    try:
        async with aiomqtt.Client(settings.mqtt_host, settings.mqtt_port) as subscriber:
            await subscriber.subscribe(f"openlaps/{VEHICLE}/#")
            task = asyncio.create_task(decoder.run(stop))
            await asyncio.wait_for(decoder.ready.wait(), timeout=10.0)
            await asyncio.wait_for(decoder.sink.connected.wait(), timeout=10.0)

            for index in range(10):
                mono_ns = index * 20_000_000
                epoch_ms = 1_780_000_000_000 + index * 20
                batcher = Batcher(4, policies, tick_ms=20)
                batcher.add(
                    "can0",
                    1,
                    Sample("can0:coolant", mono_ns, float(epoch_ms), 350.0 + index),
                )
                batcher.add(
                    "can0",
                    2,
                    Sample("can0:hidden", mono_ns, float(epoch_ms), float(index)),
                )
                await js.publish(
                    f"tele.{VEHICLE}.can0",
                    batcher.tick(epoch_ms, mono_ns)["can0"],
                    headers={MSG_TYPE_HEADER: MSG_TYPE_BATCH},
                )

            first = await asyncio.wait_for(anext(subscriber.messages), timeout=5.0)
            second = await asyncio.wait_for(anext(subscriber.messages), timeout=5.0)
            received = [first, second]
            assert {str(message.topic) for message in received} == {
                f"openlaps/{VEHICLE}/car.coolant_temp"
            }
            payloads = [json.loads(bytes(message.payload)) for message in received]
            assert payloads[0]["value"] == pytest.approx(350.0)
            assert payloads[-1]["value"] == pytest.approx(359.0)
            assert payloads[-1]["time"] == 1_780_000_000_180

            # 10 source values arriving together at a 2 Hz ceiling conflate to
            # the first and newest values, never a queue of stale intermediates.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(subscriber.messages), timeout=0.3)
            assert decoder.limiter.suppressed["car.coolant_temp"] == 8
            assert "car.unselected" not in decoder.limiter.published
    finally:
        stop.set()
        if "task" in locals():
            await asyncio.wait_for(task, timeout=10.0)
        await nc.close()


def test_real_batches_reach_mqtt_scaled_selected_and_rate_limited(
    nats_url: str,
    mosquitto_url: str,
    tmp_path: Path,
):
    config = tmp_path / "live-decoder.yaml"
    config.write_text(
        f"""vehicle: {VEHICLE}
defaults:
  max_hz: 10
  total_max_hz: 500
channels:
  - match: car.coolant_temp
    max_hz: 2
""",
        encoding="utf-8",
    )
    asyncio.run(_exercise(nats_url, mosquitto_url, config))
