"""The strategy service: per-lap evaluation of the race for this car (P7.9).

`fuel.json` computed laps remaining in a panel expression: no rolling
window, no in/out-lap exclusion, no bounds, no driver-time regulation and
no knowledge of when the race ends. This service puts that arithmetic in
code that runs whether or not anyone is looking, writes each evaluation to
``strategy_state`` for the dashboard, publishes the radio-worthy numbers
live under ``strategy.*`` for the pit wall, and raises its warnings as
``watch_findings`` rows so they alert through the same chain as every
threshold.

It runs per lap and reads views, which is why it is not the watch service
(locked decision 5): a poll of the database every couple of seconds notices
a new lap crossing, a plan revision, a session ending or an open pit stop,
and each of those triggers an evaluation; a timer guarantees one every
``max_interval_s`` regardless, because driver time runs on the clock and
not on the lap counter. Reads and writes are synchronous psycopg on a
worker thread; the MQTT side is aiomqtt on the event loop, as in the
timing extrapolator.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import aiomqtt

from pit.db.dsn import dsn_from_env
from pit.strategy.database import Probe, StrategyDatabase
from pit.strategy.health import HealthState, serve_health
from pit.strategy.model import StrategyPolicy, StrategyState, evaluate, idle_state
from pit.strategy.publisher import messages, mqtt_message

logger = logging.getLogger(__name__)

_REPORT_INTERVAL_S = 60.0
_DB_RETRY_S = 5.0


@dataclass(frozen=True, slots=True)
class StrategySettings:
    """Deploy-time wiring plus the policy tunables; see ``example.env``."""

    dsn: str
    vehicle_id: str
    policy: StrategyPolicy = StrategyPolicy()
    poll_s: float = 2.0
    pit_interval_s: float = 5.0
    max_interval_s: float = 30.0
    health_port: int = 8088
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StrategySettings:
        """Build settings from the environment; a bad value is a startup error."""
        env = os.environ if env is None else env
        vehicle = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle:
            raise ValueError("OPENLAPS_VEHICLE_ID must name the car the pit watches")

        def number(name: str, default: str, *, positive: bool = True) -> float:
            value = float(env.get(name, default).strip() or default)
            if positive and value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        policy = StrategyPolicy(
            burn_window_laps=int(number("OPENLAPS_STRATEGY_BURN_WINDOW_LAPS", "5")),
            burn_sigma=number("OPENLAPS_STRATEGY_BURN_SIGMA", "2"),
            outlier_fraction=number("OPENLAPS_STRATEGY_OUTLIER_FRACTION", "0.2"),
            driver_margin_s=number("OPENLAPS_STRATEGY_DRIVER_MARGIN_S", "600"),
            plan_drift_laps=int(number("OPENLAPS_STRATEGY_PLAN_DRIFT_LAPS", "3", positive=False)),
            short_fill_fraction=number("OPENLAPS_STRATEGY_SHORT_FILL_FRACTION", "0.1"),
            window_warn_laps=int(number("OPENLAPS_STRATEGY_WINDOW_WARN_LAPS", "2", positive=False)),
        )
        mqtt_port = int(number("OPENLAPS_MQTT_PORT", "1883"))
        health_port = int(number("OPENLAPS_STRATEGY_HEALTH_PORT", "8088", positive=False))
        if not 1 <= mqtt_port <= 65535:
            raise ValueError("OPENLAPS_MQTT_PORT must be between 1 and 65535")
        if not 0 <= health_port <= 65535:
            raise ValueError("OPENLAPS_STRATEGY_HEALTH_PORT must be between 0 and 65535")
        return cls(
            dsn=dsn_from_env(env),
            vehicle_id=vehicle,
            policy=policy,
            poll_s=number("OPENLAPS_STRATEGY_POLL_S", "2"),
            pit_interval_s=number("OPENLAPS_STRATEGY_PIT_INTERVAL_S", "5"),
            max_interval_s=number("OPENLAPS_STRATEGY_MAX_INTERVAL_S", "30"),
            health_port=health_port,
            mqtt_host=env.get("OPENLAPS_MQTT_HOST", "127.0.0.1").strip() or "127.0.0.1",
            mqtt_port=mqtt_port,
            mqtt_username=env.get("OPENLAPS_MQTT_USERNAME", "").strip() or None,
            mqtt_password=env.get("OPENLAPS_MQTT_PASSWORD", "") or None,
        )


class StrategyService:
    """Polls the read surface, evaluates on change, writes and publishes."""

    def __init__(
        self,
        settings: StrategySettings,
        *,
        database: StrategyDatabase | None = None,
        clock=None,
    ) -> None:
        """Build the service; ``database`` and ``clock`` are injectable for tests."""
        self.settings = settings
        self.health = HealthState()
        self._db = (
            StrategyDatabase(settings.dsn, settings.vehicle_id) if database is None else database
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_probe: Probe | None = None
        self._last_evaluated_mono: float | None = None
        self._last_report = 0.0
        self._outbox: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=64)
        self._adopted = False

    # -- one step, synchronous: runs on a worker thread -----------------------------

    def step(self) -> StrategyState | None:
        """Probe, decide, evaluate; returns the state written, or None."""
        if not self._adopted:
            adopted = self._db.adopt_open_findings()
            if adopted:
                logger.info("strategy: adopted %d open finding(s) from a previous run", adopted)
            self._adopted = True
        probe = self._db.probe()
        trigger = self._trigger(probe)
        if trigger is None:
            return None
        now = self._clock()
        if probe.session_id is None or probe.session_started is None:
            state = idle_state(now, self.settings.vehicle_id)
        else:
            inputs = self._db.read_inputs(probe.session_id, probe.session_started, now)
            state = evaluate(inputs, self.settings.policy, trigger)
        self._db.write(state)
        self._last_probe = probe
        self._last_evaluated_mono = time.monotonic()
        self.health.observe_evaluation(state.session_id, trigger, len(self._db.open_findings))
        for finding in state.findings:
            logger.info(
                "strategy: %s %s: %s",
                finding.severity,
                finding.monitor,
                finding.summary.get("message"),
            )
        return state

    def _trigger(self, probe: Probe) -> str | None:
        previous = self._last_probe
        if previous is None:
            return "start"
        if probe.session_id != previous.session_id:
            return "idle" if probe.session_id is None else "start"
        if probe.session_id is None:
            return None
        if probe.last_crossed_at != previous.last_crossed_at:
            return "lap"
        if probe.plan_revision != previous.plan_revision:
            return "plan"
        since = time.monotonic() - (self._last_evaluated_mono or 0.0)
        if probe.in_pits and since >= self.settings.pit_interval_s:
            return "pit"
        if since >= self.settings.max_interval_s:
            return "timer"
        return None

    # -- the loops ----------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Evaluate until ``stop`` is set, with the MQTT publisher alongside."""
        logger.info(
            "strategy: starting for %s, polling every %.3gs",
            self.settings.vehicle_id,
            self.settings.poll_s,
        )
        mqtt_task = asyncio.create_task(self._mqtt_loop(stop))
        try:
            while not stop.is_set():
                delay = self.settings.poll_s
                try:
                    state = await asyncio.to_thread(self.step)
                except Exception as exc:  # noqa: BLE001 - every database failure: count, redial later
                    self.health.observe_db_error(exc)
                    logger.warning("strategy: evaluation failed: %s", exc)
                    delay = _DB_RETRY_S
                else:
                    if state is not None:
                        self._publish(state)
                self._report()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            mqtt_task.cancel()
            await asyncio.gather(mqtt_task, return_exceptions=True)
            await asyncio.to_thread(self._db.close)
            logger.info("strategy: stopped")

    def _publish(self, state: StrategyState) -> None:
        for channel, document in messages(state):
            item = mqtt_message(self.settings.vehicle_id, channel, document)
            if self._outbox.full():
                # Newer numbers supersede older ones; drop the oldest.
                self._outbox.get_nowait()
            self._outbox.put_nowait(item)

    async def _mqtt_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with aiomqtt.Client(
                    self.settings.mqtt_host,
                    self.settings.mqtt_port,
                    username=self.settings.mqtt_username,
                    password=self.settings.mqtt_password,
                ) as client:
                    self.health.mqtt_connected = True
                    while not stop.is_set():
                        try:
                            topic, payload = await asyncio.wait_for(
                                self._outbox.get(), timeout=0.25
                            )
                        except TimeoutError:
                            continue
                        await client.publish(topic, payload, qos=0, retain=True)
                        self.health.mqtt_published += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - broker outages retry forever
                self.health.mqtt_connected = False
                self.health.mqtt_reconnects += 1
                logger.warning("strategy: MQTT unavailable: %s", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass
        self.health.mqtt_connected = False

    def _report(self) -> None:
        now = time.monotonic()
        if now - self._last_report < _REPORT_INTERVAL_S:
            return
        self._last_report = now
        logger.info("strategy: %s", self.health.log_line())


async def run_service(settings: StrategySettings, stop: asyncio.Event) -> None:
    """Run one strategy service with its health endpoint until ``stop`` is set."""
    service = StrategyService(settings)
    server = serve_health(service.health, settings.health_port)
    try:
        await service.run(stop)
    finally:
        server.shutdown()
        server.server_close()
