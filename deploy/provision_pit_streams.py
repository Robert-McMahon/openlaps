#!/usr/bin/env python3
"""Create (or converge) the pit's sourced ``TELE_VEHICLE`` stream.

Run once per pit bring-up, after both nats-servers are up and the leafnode
is connected. Idempotent: it looks the stream up and either creates it or
converges it onto the desired config -- the same end state
``src/agent/publisher.py::_ensure_streams`` reaches for TELE and CMD, so
re-running it after an edit is the normal way to change the stream's
limits. See ``ensure_pit_stream`` for why the lookup comes first.

Two properties of the resulting stream are load-bearing, and both are
measurements rather than preferences:

**It declares no subjects.** A pit stream configured with both ``sources``
and ``subjects: [tele.<vehicle>.>]`` was measured growing by *twenty*
messages for ten published once to the vehicle. The leafnode propagates the
subject to the pit server as ordinary core NATS, so such a stream captures
each message directly *and* again through sourcing -- exactly 2x. Subjects
are preserved through sourcing, so pit consumers still filter on
``tele.<vehicle>.>``; they simply must do it against a named stream.

**Consumers must therefore name it.** ``nats-py`` resolves a stream from a
subject via ``$JS.API.STREAM.NAMES`` with a subject filter, and the server
matches that filter against a stream's *declared* subjects. A subject-less
stream matches nothing, so a bare ``js.subscribe("tele.x.>")`` raises
``NotFoundError`` against the pit. Every pit service passes ``stream=``
from configuration (``OPENLAPS_INGEST_STREAM``, ``OPENLAPS_LIVE_STREAM``,
``OPENLAPS_NTRIP_STREAM``) for this reason.

The equivalent `nats` CLI invocation is in deploy/README.md, for a pit
without this repository checked out.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import nats
from nats.js import JetStreamContext, api
from nats.js.errors import BadRequestError, NotFoundError

logger = logging.getLogger("provision-pit-streams")

DEFAULT_SERVER = os.environ.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222")
DEFAULT_STREAM = os.environ.get("OPENLAPS_PIT_STREAM", "TELE_VEHICLE")
DEFAULT_SOURCE_STREAM = os.environ.get("OPENLAPS_VEHICLE_STREAM", "TELE")
DEFAULT_DOMAIN = os.environ.get("OPENLAPS_VEHICLE_JS_DOMAIN", "veh")

# The pit stream is a buffer in front of Timescale, not an archive -- ADR
# 0003 makes the database the archive. Even so its age cap must be at least
# the vehicle's (OPENLAPS_TELE_MAX_AGE_H, 72 h), and the reason is how
# nats-server resumes a source. It keeps no separate cursor: on restart it
# finds where it was by scanning the pit stream for the newest message
# carrying a `Nats-Stream-Source` header, and an *empty* pit stream resumes
# from the vehicle's oldest message. With a shorter pit cap, a pit outage
# longer than that cap but shorter than the vehicle's empties the pit
# stream and re-pulls the vehicle's whole window -- including the part the
# pit had already archived. Those messages arrive under new pit sequence
# numbers, above the ingest cursor that is the writer's only idempotency
# (`src/pit/ingest_writer/writer.py`), and `samples` has no unique key, so
# they land as duplicate rows. Matching the vehicle closes both cases: an
# outage shorter than the cap leaves messages to resume from, and one
# longer than the vehicle's window means everything it still holds is new.
# The size cap remains the backstop for a runaway producer and should be
# set from the pit's actual free disk.
DEFAULT_MAX_AGE_H = float(os.environ.get("OPENLAPS_PIT_STREAM_MAX_AGE_H", "72"))
DEFAULT_MAX_BYTES = int(os.environ.get("OPENLAPS_PIT_STREAM_MAX_BYTES", str(32 * 1024**3)))


def retention_shortfall(pit_max_age_s: float, vehicle_max_age_s: float) -> str | None:
    """Why the pit's age cap is unsafe against the vehicle's, or None if it is not.

    A `max_age` of 0 means unlimited on both sides.
    """
    pit_unlimited = pit_max_age_s <= 0
    vehicle_unlimited = vehicle_max_age_s <= 0
    if pit_unlimited or (not vehicle_unlimited and pit_max_age_s >= vehicle_max_age_s):
        return None
    vehicle = "unlimited" if vehicle_unlimited else f"{vehicle_max_age_s / 3600:g} h"
    return (
        f"pit stream max_age {pit_max_age_s / 3600:g} h is shorter than the vehicle "
        f"stream's {vehicle}: a pit outage longer than the pit cap empties the pit "
        "stream, and the source then resumes from the vehicle's oldest message, "
        "re-ingesting data Timescale already holds as duplicate rows. Set "
        "OPENLAPS_PIT_STREAM_MAX_AGE_H to at least OPENLAPS_TELE_MAX_AGE_H."
    )


async def source_max_age_s(client, source_stream: str, domain: str) -> float | None:
    """The vehicle stream's age cap in seconds, read across the leafnode.

    None when the vehicle is not reachable right now (leafnode down, stream
    not yet created): the check is advisory and must not block bring-up.
    """
    try:
        info = await client.jetstream(domain=domain).stream_info(source_stream)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot tell"
        logger.info("could not read %s over $JS.%s.API: %s", source_stream, domain, exc)
        return None
    return float(info.config.max_age or 0)


def pit_stream_config(
    *,
    name: str = DEFAULT_STREAM,
    source_stream: str = DEFAULT_SOURCE_STREAM,
    domain: str = DEFAULT_DOMAIN,
    max_age_s: float = DEFAULT_MAX_AGE_H * 3600.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> api.StreamConfig:
    """The sourced-only stream config. Note the absent ``subjects``."""
    return api.StreamConfig(
        name=name,
        sources=[
            api.StreamSource(
                name=source_stream,
                # Cross-domain addressing: the vehicle's JetStream API is
                # reachable from the pit as "$JS.<domain>.API" over the
                # leafnode. This string must match the vehicle server's
                # `jetstream { domain: ... }` in deploy/nats/vehicle.conf.
                external=api.ExternalStream(api=f"$JS.{domain}.API"),
            )
        ],
        storage=api.StorageType.FILE,
        retention=api.RetentionPolicy.LIMITS,
        max_age=max_age_s,
        max_bytes=max_bytes,
        num_replicas=1,
    )


async def ensure_pit_stream(
    js: JetStreamContext, config: api.StreamConfig | None = None
) -> api.StreamInfo:
    """Create the stream, or converge an existing one onto ``config``."""
    config = pit_stream_config() if config is None else config
    # Look before adding, rather than adding and converging on the error --
    # the same ordering, for the same reason, as
    # `src/agent/publisher.py::_ensure_streams`. nats-server costs an `add`
    # of a stream that already exists as a *new* reservation of its
    # max_bytes, checked before it deduplicates by name, so re-adding
    # TELE_VEHICLE asks for a second 32 GiB on top of the one it already
    # holds and the 40 GiB `max_file_store` in deploy/nats/pit.conf answers
    # 10047 "insufficient storage resources". That is a 500, not the
    # BadRequestError the converge path catches, so it escaped and failed
    # every bring-up after the first (the store outlives the container, and
    # the reservation with it). An `update` is costed as a delta against the
    # existing reservation, so it stays free no matter how tight the
    # headroom.
    try:
        await js.stream_info(config.name)
    except NotFoundError:
        try:
            info = await js.add_stream(config)
        except BadRequestError:
            # Raced with another provisioner between the two calls.
            info = await js.update_stream(config)
        else:
            logger.info("created stream %s sourcing %s", config.name, config.sources[0].name)
            return info
    else:
        info = await js.update_stream(config)
    logger.info("converged existing stream %s", config.name)
    return info


async def run(args: argparse.Namespace) -> int:
    config = pit_stream_config(
        name=args.stream,
        source_stream=args.source_stream,
        domain=args.domain,
        max_age_s=args.max_age_h * 3600.0,
        max_bytes=args.max_bytes,
    )
    client = await nats.connect(args.server, user_credentials=args.creds or None)
    try:
        info = await ensure_pit_stream(client.jetstream(), config)
        vehicle_max_age = await source_max_age_s(client, args.source_stream, args.domain)
    finally:
        await client.close()
    print(
        f"{info.config.name}: sourcing {config.sources[0].name} via "
        f"$JS.{args.domain}.API, {info.state.messages} messages, "
        f"{info.state.bytes} bytes",
        file=sys.stderr,
    )
    if vehicle_max_age is not None:
        shortfall = retention_shortfall(float(info.config.max_age or 0), vehicle_max_age)
        if shortfall:
            # Advisory: bring-up continues, but every run says so until fixed.
            print(f"WARNING: {shortfall}", file=sys.stderr)
    if info.config.subjects:
        # Refuse to leave a double-capturing stream in place silently.
        print(
            f"WARNING: {info.config.name} declares subjects {info.config.subjects}; "
            "a sourced stream that also claims tele.> captures every message twice. "
            "Delete the stream and re-run.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--server", default=DEFAULT_SERVER, help="pit NATS URL (default: %(default)s)"
    )
    parser.add_argument("--creds", default=os.environ.get("OPENLAPS_NATS_CREDS") or None)
    parser.add_argument(
        "--stream", default=DEFAULT_STREAM, help="pit stream name (default: %(default)s)"
    )
    parser.add_argument(
        "--source-stream",
        default=DEFAULT_SOURCE_STREAM,
        help="vehicle stream to source (default: %(default)s)",
    )
    parser.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help="vehicle JetStream domain (default: %(default)s)",
    )
    parser.add_argument("--max-age-h", type=float, default=DEFAULT_MAX_AGE_H)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
