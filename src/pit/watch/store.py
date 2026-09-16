"""Owner-role writer: scores, finding transitions and checkpoints commit together."""

from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from pit.pit_monitor.store import PitMetricStore


def stamp(at: float) -> datetime:
    return datetime.fromtimestamp(at, UTC)


class WatchStore(PitMetricStore):
    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=5,
                options="-c statement_timeout=5000",
            )
        return self._conn

    def session(self, vehicle: str, at: float) -> str | None:
        row = (
            self._connection()
            .execute(
                "SELECT session_id FROM sessions WHERE vehicle_id=%s AND status='active' "
                "AND started <= %s ORDER BY started DESC LIMIT 1",
                (vehicle, stamp(at)),
            )
            .fetchone()
        )
        return row[0] if row else None

    def load(self, vehicle: str, session: str) -> dict:
        rows = (
            self._connection()
            .execute(
                "SELECT monitor, model FROM watch_baselines "
                "WHERE vehicle_id=%s AND session_id=%s AND stint_number=0",
                (vehicle, session),
            )
            .fetchall()
        )
        return dict(rows)

    def write_tick(self, vehicle: str, session: str | None, rows: dict, models: dict) -> None:
        try:
            with self._connection().transaction():
                for name, row in rows.items():
                    self._connection().execute(
                        "INSERT INTO watch_scores VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            stamp(row["time"]),
                            vehicle,
                            name,
                            row["score"],
                            row["residual"],
                            row["expected"],
                            row["observed"],
                            row["baseline_status"],
                        ),
                    )
                    finding = row["finding"]
                    if finding:
                        self._connection().execute(
                            "INSERT INTO watch_findings VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON CONFLICT (finding_id) DO UPDATE SET "
                            "closed_at=EXCLUDED.closed_at, peak_score=EXCLUDED.peak_score, "
                            "summary=EXCLUDED.summary",
                            (
                                finding["finding_id"],
                                vehicle,
                                name,
                                stamp(finding["opened_at"]),
                                stamp(finding["closed_at"]) if finding["closed_at"] else None,
                                finding["severity"],
                                finding["peak_score"],
                                Jsonb(finding["summary"]),
                            ),
                        )
                    if session:
                        self._connection().execute(
                            "INSERT INTO watch_baselines VALUES (%s,%s,%s,0,%s,%s) "
                            "ON CONFLICT (vehicle_id,monitor,session_id,stint_number) "
                            "DO UPDATE SET model=EXCLUDED.model",
                            (vehicle, name, session, stamp(row["time"]), Jsonb(models[name])),
                        )
        except Exception:
            self.close()
            raise

    def close_previous(self, vehicle: str, monitors: list[str], at: float, keep: list[str]) -> None:
        """End only this service's findings when the active session changes."""
        with self._connection().transaction():
            self._connection().execute(
                "UPDATE watch_findings SET closed_at=%s, "
                'summary=summary || \'{"closed_reason":"session_changed"}\'::jsonb '
                "WHERE vehicle_id=%s AND monitor=ANY(%s) AND closed_at IS NULL "
                "AND NOT (finding_id::text=ANY(%s))",
                (stamp(at), vehicle, monitors, keep),
            )
