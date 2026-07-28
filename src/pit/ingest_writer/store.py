"""Every statement the ingest-writer runs against TimescaleDB.

The whole write path is one transaction per flush: `COPY` the sample rows
into a session-lifetime `TEMP` table, `INSERT ... SELECT` them into the
`samples` hypertable, upsert whatever lap rows the same batches carried, and
advance `ingest_cursor` — all or nothing. The consumer is acked only after
that transaction commits, which is what makes redelivery a no-op rather than
a duplicate (see `writer.py`'s module docstring).

The temp table is created once per connection with `ON COMMIT DELETE ROWS`
rather than per flush with `ON COMMIT DROP`: at the default 200 ms flush
interval a per-flush `CREATE TEMP TABLE` would churn ~430,000 catalog rows a
day for no benefit.

One connection, no pool: flushes are serialised by construction (a single
writer task), and a pool would only add ways for two flushes to interleave.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import psycopg

from core.pb import telemetry_pb2 as pb
from pit.ingest_writer.laps import LapRow, PitStatusUpdate

logger = logging.getLogger(__name__)

# (time, channel_key, value, value_text)
SampleRow = tuple[datetime, int, float | None, str | None]

_FLUSH_TABLE = "_sample_flush"

_CREATE_FLUSH_TABLE = f"""
CREATE TEMP TABLE IF NOT EXISTS {_FLUSH_TABLE} (
    time        TIMESTAMPTZ      NOT NULL,
    channel_key BIGINT           NOT NULL,
    value       DOUBLE PRECISION NULL,
    value_text  TEXT             NULL
) ON COMMIT DELETE ROWS
"""

_INSERT_SAMPLES = f"""
INSERT INTO samples (time, channel_key, value, value_text)
SELECT time, channel_key, value, value_text FROM {_FLUSH_TABLE}
"""

_UPSERT_CURSOR = """
INSERT INTO ingest_cursor (consumer, stream, stream_seq, updated)
VALUES (%s, %s, %s, now())
ON CONFLICT (consumer) DO UPDATE
SET stream = EXCLUDED.stream, stream_seq = EXCLUDED.stream_seq, updated = EXCLUDED.updated
WHERE ingest_cursor.stream_seq < EXCLUDED.stream_seq
"""

_UPSERT_LAP = """
INSERT INTO laps (vehicle_id, session_id, stint_id, track_name, lap_number, crossed_at,
                  lap_time_s, valid, pit_status, direction)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (vehicle_id, crossed_at) DO UPDATE
SET session_id = EXCLUDED.session_id,
    stint_id   = EXCLUDED.stint_id,
    track_name = EXCLUDED.track_name,
    lap_number = EXCLUDED.lap_number,
    lap_time_s = EXCLUDED.lap_time_s,
    valid      = EXCLUDED.valid,
    pit_status = EXCLUDED.pit_status,
    direction  = EXCLUDED.direction
RETURNING lap_id
"""

_UPSERT_SECTOR = """
INSERT INTO lap_sectors (lap_id, sector, split_time_s, crossed_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (lap_id, sector) DO UPDATE
SET split_time_s = EXCLUDED.split_time_s, crossed_at = EXCLUDED.crossed_at
"""

# A pit entry/exit happens during a lap that has not been written yet, so it
# lands on the most recently completed lap at that instant.
_UPDATE_PIT_STATUS = """
UPDATE laps SET pit_status = %s
WHERE lap_id = (
    SELECT lap_id FROM laps
    WHERE vehicle_id = %s AND crossed_at <= %s
    ORDER BY crossed_at DESC LIMIT 1
)
"""


class TimescaleStore:
    """The ingest-writer's database connection and its statements."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None
        self._sessions_seen: set[str] = set()
        self._stints: dict[tuple[str, int], int] = {}
        self.connects = 0

    @property
    def connected(self) -> bool:
        """Whether a usable connection is currently held."""
        return self._conn is not None and not self._conn.closed

    async def connect(self) -> None:
        """Open the connection and prepare the per-connection temp table."""
        await self.close()
        conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=False)
        async with conn.transaction():
            await conn.execute(_CREATE_FLUSH_TABLE)
        self._conn = conn
        self.connects += 1
        # Session/stint ids are per-database, not per-connection, but a
        # reconnect is the natural point to re-check rows that may have been
        # written by session-control while this writer was disconnected.
        self._sessions_seen.clear()
        self._stints.clear()

    async def close(self) -> None:
        """Close the connection if one is open; never raises."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - closing a dead connection
                pass

    async def read_cursor(self, consumer: str) -> int:
        """The highest stream sequence whose rows are committed (0 if none)."""
        conn = self._require()
        async with conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT stream_seq FROM ingest_cursor WHERE consumer = %s", (consumer,)
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def upsert_registry(
        self, vehicle_id: str, registry: pb.ChannelRegistry
    ) -> dict[int, int]:
        """Record one registry generation; returns `wire_id -> channel_key`.

        Idempotent: replaying the same generation resolves to the same
        `channel_key` values, which is the point — a channel's identity
        outlives every generation it appears in (`docs/PIT_SCHEMA.md`).
        """
        conn = self._require()
        keys: dict[int, int] = {}
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO channel_registry (vehicle_id, registry_seq, created) "
                "VALUES (%s, %s, %s) ON CONFLICT (vehicle_id, registry_seq) DO NOTHING",
                (
                    vehicle_id,
                    registry.registry_seq,
                    _epoch_ms_to_datetime(registry.created_unix_ms),
                ),
            )
            for channel in registry.channels:
                row = await (
                    await conn.execute(
                        "INSERT INTO channels (vehicle_id, name, units, value_type) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (vehicle_id, name) DO UPDATE "
                        "SET units = EXCLUDED.units, value_type = EXCLUDED.value_type "
                        "RETURNING channel_key",
                        (vehicle_id, channel.name, channel.units, channel.type),
                    )
                ).fetchone()
                channel_key = int(row[0])
                keys[channel.id] = channel_key
                await conn.execute(
                    "INSERT INTO channel_map (vehicle_id, registry_seq, wire_id, channel_key, "
                    'source_ref, units, value_type, scale, "offset") '
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (vehicle_id, registry_seq, wire_id) DO UPDATE "
                    "SET channel_key = EXCLUDED.channel_key, source_ref = EXCLUDED.source_ref, "
                    "units = EXCLUDED.units, value_type = EXCLUDED.value_type, "
                    'scale = EXCLUDED.scale, "offset" = EXCLUDED."offset"',
                    (
                        vehicle_id,
                        registry.registry_seq,
                        channel.id,
                        channel_key,
                        channel.source_ref,
                        channel.units,
                        channel.type,
                        channel.scale,
                        channel.offset,
                    ),
                )
        return keys

    async def flush(
        self,
        *,
        rows: Sequence[SampleRow],
        laps: Sequence[LapRow],
        pit_updates: Sequence[PitStatusUpdate],
        consumer: str,
        stream: str,
        stream_seq: int,
    ) -> None:
        """Write one flush's worth of work and advance the cursor, atomically."""
        conn = self._require()
        async with conn.transaction():
            if rows:
                async with conn.cursor() as cur:
                    async with cur.copy(
                        f"COPY {_FLUSH_TABLE} (time, channel_key, value, value_text) FROM STDIN"
                    ) as copy:
                        for row in rows:
                            await copy.write_row(row)
                    await cur.execute(_INSERT_SAMPLES)
            for lap in laps:
                await self._write_lap(conn, lap)
            for update in pit_updates:
                await conn.execute(
                    _UPDATE_PIT_STATUS, (update.pit_status, update.vehicle_id, update.at)
                )
            if stream_seq > 0:
                await conn.execute(_UPSERT_CURSOR, (consumer, stream, stream_seq))

    async def _write_lap(self, conn: psycopg.AsyncConnection, lap: LapRow) -> None:
        session_id, stint_id = await self._resolve_session(conn, lap)
        row = await (
            await conn.execute(
                _UPSERT_LAP,
                (
                    lap.vehicle_id,
                    session_id,
                    stint_id,
                    lap.track_name,
                    lap.lap_number,
                    lap.crossed_at,
                    lap.lap_time_s,
                    lap.valid,
                    lap.pit_status,
                    lap.direction,
                ),
            )
        ).fetchone()
        lap_id = int(row[0])
        for sector in lap.sectors:
            await conn.execute(
                _UPSERT_SECTOR, (lap_id, sector.sector, sector.split_time_s, sector.crossed_at)
            )

    async def _resolve_session(
        self, conn: psycopg.AsyncConnection, lap: LapRow
    ) -> tuple[str | None, int | None]:
        """Resolve the lap's session/stint FKs, or NULL where they don't exist.

        `sessions` and `stints` are owned by session-control, which may not
        have written its row yet (its own DB write can be queued behind an
        outage). A lap with a stamped session we cannot see is written with
        NULL FKs rather than failing the whole flush on a foreign key — a lap
        with no session is still a lap.
        """
        if lap.session_id is None:
            return None, None
        if lap.session_id not in self._sessions_seen:
            found = await (
                await conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = %s", (lap.session_id,)
                )
            ).fetchone()
            if not found:
                return None, None
            self._sessions_seen.add(lap.session_id)
        if lap.stint_number is None:
            return lap.session_id, None
        cached = self._stints.get((lap.session_id, lap.stint_number))
        if cached is not None:
            return lap.session_id, cached
        row = await (
            await conn.execute(
                "SELECT stint_id FROM stints WHERE session_id = %s AND stint_number = %s",
                (lap.session_id, lap.stint_number),
            )
        ).fetchone()
        if not row:
            return lap.session_id, None
        stint_id = int(row[0])
        self._stints[(lap.session_id, lap.stint_number)] = stint_id
        return lap.session_id, stint_id

    def _require(self) -> psycopg.AsyncConnection:
        conn = self._conn
        if conn is None or conn.closed:
            raise psycopg.OperationalError("ingest-writer is not connected to the database")
        return conn


def _epoch_ms_to_datetime(unix_ms: int) -> datetime | None:
    """A registry's `created_unix_ms`, or None when the producer left it unset."""
    if not unix_ms:
        return None
    return datetime.fromtimestamp(unix_ms / 1000.0, tz=UTC)
