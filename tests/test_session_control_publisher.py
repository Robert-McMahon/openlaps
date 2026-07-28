"""Latest-only JetStream session publisher tests."""

from __future__ import annotations

import asyncio
import json

import nats
from nats.js import api

from pit.session_control.publisher import LatestSessionPublisher, retry_backoff

VEHICLE = "test-car"


def test_publish_retry_backoff_grows_until_progress_resets_it():
    assert retry_backoff(0.5, made_progress=False) == 1.0
    assert retry_backoff(8.0, made_progress=False) == 15.0
    assert retry_backoff(15.0, made_progress=False) == 15.0
    assert retry_backoff(15.0, made_progress=True) == 0.5


def test_retries_collapse_to_the_latest_payload_before_connection():
    publisher = LatestSessionPublisher("nats://unavailable", VEHICLE, domain=None)

    publisher.submit({"session_id": "s-1", "driver": "Driver A"})
    publisher.submit({"session_id": "s-1", "driver": "Driver B"})

    assert json.loads(publisher.pending_payload()) == {
        "driver": "Driver B",
        "session_id": "s-1",
    }
    assert publisher.pending_count == 1


def test_publish_failure_retries_only_the_latest_payload(monkeypatch):
    published: list[bytes] = []

    class JetStream:
        def __init__(self, fail: bool, publisher: LatestSessionPublisher) -> None:
            self.fail = fail
            self.publisher = publisher

        async def publish(self, _subject: str, payload: bytes, timeout: float) -> None:
            assert timeout > 0
            if self.fail:
                self.publisher.submit({"session_id": "s-1", "driver": "Driver B"})
                raise OSError("leaf link unavailable")
            published.append(payload)

    class Client:
        is_connected = True

        def __init__(self, js: JetStream) -> None:
            self.js = js

        def jetstream(self, **_kwargs):
            return self.js

        async def drain(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def exercise() -> None:
        publisher = LatestSessionPublisher("nats://pit", VEHICLE, domain="veh")
        clients = [Client(JetStream(True, publisher)), Client(JetStream(False, publisher))]
        stop = asyncio.Event()

        async def connect(_stop):
            return clients.pop(0)

        async def no_delay(_stop, _delay):
            return None

        monkeypatch.setattr(publisher, "_connect", connect)
        monkeypatch.setattr("pit.session_control.publisher._sleep_unless", no_delay)
        publisher.submit({"session_id": "s-1", "driver": "Driver A"})
        task = asyncio.create_task(publisher.run(stop))
        await _wait_until(lambda: bool(published))
        stop.set()
        await task

        assert json.loads(published[0]) == {"driver": "Driver B", "session_id": "s-1"}
        assert publisher.errors == 1

    asyncio.run(exercise())


def test_last_session_payload_supersedes_prior_message(nats_url):
    async def exercise() -> None:
        client = await nats.connect(nats_url)
        js = client.jetstream()
        await js.add_stream(
            api.StreamConfig(
                name="CMD",
                subjects=[f"cmd.{VEHICLE}.>"],
                storage=api.StorageType.MEMORY,
                max_msgs_per_subject=1,
            )
        )
        publisher = LatestSessionPublisher(nats_url, VEHICLE, domain=None)
        stop = asyncio.Event()
        task = asyncio.create_task(publisher.run(stop))
        try:
            publisher.submit({"session_id": "s-1", "driver": "Driver A"})
            await _wait_until(lambda: publisher.published >= 1)
            publisher.submit({"session_id": "s-1", "driver": "Driver B"})
            await _wait_until(lambda: publisher.published >= 2)

            info = await js.stream_info("CMD")
            assert info.state.messages == 1
            subscription = await js.subscribe(
                f"cmd.{VEHICLE}.session",
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.LAST,
            )
            message = await subscription.next_msg(timeout=2)
            assert json.loads(message.data) == {
                "driver": "Driver B",
                "session_id": "s-1",
            }
        finally:
            stop.set()
            await task
            await client.close()

    asyncio.run(exercise())


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.01)
