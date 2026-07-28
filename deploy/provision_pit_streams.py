#!/usr/bin/env python3
"""Create (or converge) the pit's sourced ``TELE_VEHICLE`` stream.

Run once per pit bring-up, after both nats-servers are up and the leafnode
is connected. Idempotent: it mirrors the
``add_stream``-then-``update_stream``-on-``BadRequestError`` convergence
pattern ``src/agent/publisher.py::_ensure_streams`` already uses for TELE
and CMD, so re-running it after an edit is the normal way to change the
stream's limits.

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
from nats.js.errors import BadRequestError

logger = logging.getLogger("provision-pit-streams")

DEFAULT_SERVER = os.environ.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222")
DEFAULT_STREAM = os.environ.get("OPENLAPS_PIT_STREAM", "TELE_VEHICLE")
DEFAULT_SOURCE_STREAM = os.environ.get("OPENLAPS_VEHICLE_STREAM", "TELE")
DEFAULT_DOMAIN = os.environ.get("OPENLAPS_VEHICLE_JS_DOMAIN", "veh")

# The pit stream is a buffer in front of Timescale, not an archive -- ADR
# 0003 makes the database the archive. It has to hold everything ingest
# might miss while it is down (a service restart, a schema migration, a
# machine reboot), and no more. A day covers all of those with room to
# spare; the size cap is the backstop for a runaway producer and should be
# set from the pit's actual free disk.
DEFAULT_MAX_AGE_H = float(os.environ.get("OPENLAPS_PIT_STREAM_MAX_AGE_H", "24"))
DEFAULT_MAX_BYTES = int(os.environ.get("OPENLAPS_PIT_STREAM_MAX_BYTES", str(32 * 1024**3)))


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
    try:
        info = await js.add_stream(config)
        logger.info("created stream %s sourcing %s", config.name, config.sources[0].name)
        return info
    except BadRequestError:
        # Exists with a different configuration: converge it, exactly as the
        # agent does for TELE/CMD.
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
    finally:
        await client.close()
    print(
        f"{info.config.name}: sourcing {config.sources[0].name} via "
        f"$JS.{args.domain}.API, {info.state.messages} messages, "
        f"{info.state.bytes} bytes",
        file=sys.stderr,
    )
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
