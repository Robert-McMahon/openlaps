"""Envelope firing, clean replay, gaps, RBE equivalence and durable restart."""

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
import yaml

from core.pb import telemetry_pb2 as pb
from pit.db.migrate import apply_migrations
from pit.watch.config import WatchConfig, load_config
from pit.watch.engine import WatchEngine
from pit.watch.replay import ScaleFault
from pit.watch.service import Settings, WatchService
from pit.watch.store import WatchStore

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "profiles/example-club-racer"


def config():
    return WatchConfig.model_validate(
        dict(
            baseline_rpm_min=1200,
            monitors={
                "oil": dict(
                    target="car.oil_pressure",
                    conditioned_on=[
                        dict(channel="car.rpm", bins=250),
                        dict(channel="car.oil_temp", bins=10),
                    ],
                    baseline_seconds=5,
                    min_bin_samples=3,
                    score_window="3s",
                    min_scale=1,
                    severity="critical",
                )
            },
        )
    )


def feed(engine, t, oil=100, rpm=2000, pit="track", refresh=True):
    if refresh:
        engine.observe("lap.event", json.dumps(dict(pit_status=pit)), t - 0.01)
        for channel, value, unit in [
            ("car.oil_pressure", oil, "kPa"),
            ("car.rpm", rpm, "RPM"),
            ("car.oil_temp", 373, "K"),
        ]:
            engine.observe(channel, value, t - 0.01, unit)
    return engine.tick(t)["oil"]


def trained():
    e = WatchEngine(config())
    for t in range(5):
        assert feed(e, t)["score"] is None
    return e


def test_clean_fault_clear_and_summary():
    e = trained()
    for t in range(5, 30):
        row = feed(e, t)
        assert row["score"] == 0 and row["finding"] is None
    fault = ScaleFault(after_s=30)
    for t in range(30, 40):
        row = feed(e, t, fault.apply("car.oil_pressure", 100, t))
    finding = row["finding"]
    assert finding["severity"] == "critical"
    assert finding["summary"]["expected"] == 100
    assert finding["summary"]["observed"] == 70
    assert finding["summary"]["bins"][1]["unit"] == "K"
    assert finding["summary"]["bins"][1]["lower"] == 370
    closed = feed(e, 40)["finding"]
    assert closed["finding_id"] == finding["finding_id"]
    assert closed["closed_at"] == 40


def test_unknown_bin_stale_and_gates_never_report_zero_or_clear():
    e = trained()
    for t in range(5, 15):
        row = feed(e, t, 70)
    finding_id = row["finding"]["finding_id"]
    assert feed(e, 15, 70, rpm=3000)["baseline_status"] == "insufficient_bin"
    assert feed(e, 16, 70, pit="pit")["score"] is None
    assert feed(e, 17, 70, rpm=0)["score"] is None
    assert feed(e, 40, refresh=False)["score"] is None
    assert e.monitors["oil"].finding["finding_id"] == finding_id


def test_time_weighting_matches_rbe_and_sample_bursts():
    frequent, sparse = trained(), trained()
    for t in range(5, 30):
        for fraction in [0.1, 0.2, 0.3, 0.4]:
            frequent.observe("car.oil_pressure", 70, t - 1 + fraction, "kPa")
        a = feed(frequent, t, 70)
        b = feed(sparse, t, 70, refresh=(t % 5 == 0))
        assert a["score"] == pytest.approx(b["score"])


def test_checkpoint_restores_learning_and_frozen_finding():
    e = trained()
    for t in range(5, 15):
        feed(e, t, 70)
    model = json.loads(json.dumps(e.monitors["oil"].snapshot()))
    other = WatchEngine(config())
    other.monitors["oil"].restore(model)
    row = feed(other, 15, 70)
    assert row["expected"] == 100
    assert row["finding"]["finding_id"] == e.monitors["oil"].finding["finding_id"]
    assert other.monitors["oil"].learned == 5
    changed = config()
    changed.monitors["oil"].conditioned_on[0].bins = 100
    with pytest.raises(ValueError, match="configuration changed"):
        WatchEngine(changed).monitors["oil"].restore(model)


def test_engine_off_missing_track_and_changed_units_do_not_learn():
    e = WatchEngine(config())
    for t in range(10):
        feed(e, t, rpm=0)
    assert e.monitors["oil"].learned == 0
    e.pit_status = None
    assert feed(e, 11, refresh=False)["score"] is None
    e = trained()
    e.observe("car.oil_temp", 100, 5.1, "C")
    assert e.tick(6)["oil"]["baseline_status"] == "unit_mismatch"


def replay_all(fault=None):
    cfg = load_config(PROFILE / "watch.yaml")
    e = WatchEngine(cfg)
    catalog = yaml.safe_load((PROFILE / "catalog.yaml").read_text())["channels"]
    values = {c: 10.0 for c in e.input_channels if c != "lap.event"}
    values.update(
        {
            "car.rpm": 2000,
            "car.oil_pressure": 100,
            "car.oil_temp": 373,
            "car.battery_v": 14,
            "car.coolant_temp": 363,
            "car.ambient_air_temp": 293,
        }
    )
    findings = set()
    for t in range(300):
        e.observe("lap.event", {"pit_status": "track"}, t - 0.01)
        for c, value in values.items():
            e.observe(
                c,
                fault.apply(c, value, t) if fault else value,
                t - 0.01,
                catalog[c].get("units", ""),
            )
        rows = e.tick(t)
        findings.update(n for n, r in rows.items() if r["finding"])
    return e, rows, findings


def test_all_nine_profile_monitors_learn_clean_and_only_oil_fault_fires():
    e, rows, findings = replay_all()
    assert len(e.monitors) == 9
    assert all(m.frozen for m in e.monitors.values())
    assert all(r["baseline_status"] == "ready" for r in rows.values())
    assert not findings
    e, rows, findings = replay_all(ScaleFault())
    assert findings == {"oil_pressure_envelope"}


def test_registry_decode_and_out_of_order_samples(tmp_path, monkeypatch):
    service = WatchService(Settings(PROFILE / "watch.yaml", "test", "unused"))
    monkeypatch.setattr("pit.watch.service.time.time", lambda: 100)
    registry = pb.ChannelRegistry(registry_seq=1)
    registry.channels.add(id=1, name="car.oil_pressure", units="kPa", scale=0.1)
    service.handle(
        SimpleNamespace(
            headers={"Openlaps-Msg-Type": "registry"}, data=registry.SerializeToString()
        )
    )
    batch = pb.SampleBatch(registry_seq=1, batch_epoch_unix_ms=100000)
    batch.samples.add(channel_id=1, d=1000)
    service.handle(SimpleNamespace(headers={}, data=batch.SerializeToString()))
    assert service.pending[0][2].value == 100
    service.engine.observe("car.oil_pressure", 100, 99, "kPa")
    service.engine.tick(100)
    service.engine.observe("car.oil_pressure", 1, 95, "kPa")
    assert service.engine.readings["car.oil_pressure"].value == 100


def test_store_migration_grants_and_restart(timescale_dsn):
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO sessions(session_id,vehicle_id,session_type,started,status) "
            "VALUES ('s','v','test',to_timestamp(0),'active')"
        )
    store = WatchStore(timescale_dsn)
    e = trained()
    for t in range(5, 15):
        row = feed(e, t, 70)
        store.write_tick("v", "s", {"oil": row}, {"oil": e.monitors["oil"].snapshot()})
    store.close()
    store = WatchStore(timescale_dsn)
    other = WatchEngine(config())
    other.monitors["oil"].restore(store.load("v", "s")["oil"])
    assert other.monitors["oil"].frozen
    assert feed(other, 15, 70)["finding"]["finding_id"] == row["finding"]["finding_id"]
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        conn.execute("SET ROLE grafana_ro")
        assert conn.execute("SELECT count(*) FROM v_watch_findings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM v_watch_scores").fetchone()[0] == 10
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM watch_baselines")
    store.close()


def test_failed_database_tick_rolls_back_model(monkeypatch):
    service = WatchService(Settings(PROFILE / "watch.yaml", "v", "unused"))
    service.config = config()
    service.engine = trained()
    service.session, service.initialized = "s", True
    monkeypatch.setattr(service, "_context", lambda at: None)
    before = copy.deepcopy(service.engine.monitors["oil"].snapshot())

    def fail(*args):
        raise RuntimeError("database down")

    monkeypatch.setattr(service.store, "write_tick", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(service.tick(6))
    assert service.engine.monitors["oil"].snapshot() == before


def test_real_engine_start_fixture_cannot_learn_an_on_track_baseline(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import replay
    from _bench import load_catalog

    from collectors.clock import MonotonicWallClock
    from pit.registry_cache import RegistryCache

    profile, catalog, _ = load_catalog(PROFILE, vehicle=None, state_dir=str(tmp_path))
    frames = replay.load_candump(ROOT / "tests/fixtures/candump/candump-sample.log")
    batches = replay.replay_cycle(
        profile, catalog, MonotonicWallClock(), candump_frames=frames, nmea_lines=[], gps_rows=[]
    )
    cache = RegistryCache()
    # RuntimeCatalog exposes the same registry that the real publisher sends.
    cache.add_registry(catalog.registry)
    e = WatchEngine(load_config(PROFILE / "watch.yaml"))
    count = 0
    for batch in batches:
        decoded = cache.decode(batch.payload)
        for sample in decoded.samples:
            e.observe(
                sample.channel.name,
                sample.value,
                sample.capture_unix_ms / 1000,
                sample.channel.units,
            )
            count += 1
        if decoded.samples:
            e.tick(int(decoded.samples[-1].capture_unix_ms / 1000))
    assert count > 10000
    assert all(m.learned == 0 and m.finding is None for m in e.monitors.values())


def test_live_service_nats_database_mqtt_and_health(
    nats_url, timescale_dsn, mosquitto_url, tmp_path
):
    import time
    from urllib.parse import urlparse
    from urllib.request import urlopen

    import aiomqtt
    import nats

    from pit.watch.health import serve_health

    cfg = config()
    cfg.monitors["oil"].baseline_seconds = 3
    cfg.monitors["oil"].score_window = 1
    (tmp_path / "watch.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    (tmp_path / "catalog.yaml").write_text((PROFILE / "catalog.yaml").read_text())
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO sessions(session_id,vehicle_id,session_type,started,status) "
            "VALUES ('live','v','test',now()-interval '1 hour','active')"
        )
    mqtt = urlparse(mosquitto_url)
    settings = Settings(
        tmp_path / "watch.yaml",
        "v",
        timescale_dsn,
        nats_url=nats_url,
        stream="TELE",
        mqtt_host=mqtt.hostname,
        mqtt_port=mqtt.port,
        health_port=0,
    )

    async def exercise():
        client = await nats.connect(nats_url)
        js = client.jetstream()
        await js.add_stream(name="TELE", subjects=["tele.v.>"])
        registry = pb.ChannelRegistry(registry_seq=1)
        for i, (name, unit) in enumerate(
            [
                ("car.rpm", "RPM"),
                ("car.oil_temp", "K"),
                ("car.oil_pressure", "kPa"),
                ("lap.event", ""),
            ],
            1,
        ):
            registry.channels.add(id=i, name=name, units=unit)
        await js.publish(
            "tele.v.catalog",
            registry.SerializeToString(),
            headers={"Openlaps-Msg-Type": "registry"},
        )
        service = WatchService(settings)
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop))
        seen = []
        async with aiomqtt.Client(mqtt.hostname, mqtt.port) as subscriber:
            await subscriber.subscribe("openlaps/v/watch.oil.score")

            async def consume():
                async for message in subscriber.messages:
                    seen.append(json.loads(message.payload))

            receiver = asyncio.create_task(consume())
            try:
                # Real capture-time input and service timer, including the reorder delay.
                for second in range(12):
                    batch = pb.SampleBatch(
                        registry_seq=1, batch_epoch_unix_ms=int(time.time() * 1000)
                    )
                    for i, value in [(1, 2000), (2, 373), (3, 100 if second < 6 else 70)]:
                        batch.samples.add(channel_id=i, d=value)
                    batch.samples.add(channel_id=4, s=json.dumps({"pit_status": "track"}))
                    await js.publish("tele.v.can", batch.SerializeToString())
                    await asyncio.sleep(1)
                assert any(r["value"] is not None and r["value"] > 0.8 for r in seen)
                assert service.health.database_ok
                # HTTP adapter is the one bound by run(); port 0 chooses a free port.
                server = serve_health(service.health, 0, "127.0.0.1")
                try:
                    response = await asyncio.to_thread(
                        lambda: json.load(urlopen(f"http://127.0.0.1:{server.server_port}/health"))
                    )
                    assert response["healthy"]
                    assert response["monitors"]["oil"] == "ready"
                finally:
                    server.shutdown()
                    server.server_close()
            finally:
                stop.set()
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                await asyncio.wait_for(task, 5)
                await client.close()
        restart = WatchService(settings)
        restart._context(int(time.time()))
        assert restart.engine.monitors["oil"].frozen
        assert restart.engine.monitors["oil"].learned == 3
        assert restart.engine.monitors["oil"].finding is not None
        restart.store.close()
        with psycopg.connect(timescale_dsn) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM v_watch_findings WHERE closed_at IS NULL"
                ).fetchone()[0]
                == 1
            )

    asyncio.run(exercise())


def test_profile_conditions_and_current_sources_exist_in_dbc():
    import cantools

    catalog = yaml.safe_load((PROFILE / "catalog.yaml").read_text())["channels"]
    dbc = cantools.database.load_file(PROFILE / "dbcs/haltech-multiplexed.dbc", strict=False)
    names = {s.name for s in dbc.get_message_by_name("PD16A_OUTPUT_STATUS").signals}
    for monitor in load_config(PROFILE / "watch.yaml").monitors.values():
        for c in [monitor.target, *(c.channel for c in monitor.conditioned_on)]:
            source = catalog[c]["from"]
            if "PD16A_OUTPUT_STATUS." in source:
                assert source.rsplit(".", 1)[1] in names


def test_stored_fit_and_replay_injector(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import replay
    from watch_fit import fit

    from core.samples import Sample

    e = WatchEngine(config())
    samples = []
    for t in range(10):
        samples += [
            (t, "lap.event", '{"pit_status":"track"}', ""),
            (t, "car.rpm", 2000, "RPM"),
            (t, "car.oil_temp", 373, "K"),
            (t, "car.oil_pressure", 100, "kPa"),
        ]
    models = fit(samples, e)
    assert models["oil"]["frozen"] and models["oil"]["learned"] == 5
    received = []
    pipeline = SimpleNamespace(ingest=lambda source, sample: received.append(sample.value))
    emit = replay._emit(pipeline, "can", ScaleFault(after_s=5), "oil")
    for t in range(10):
        emit(Sample("oil", t * 10**9, t * 1000, 100))
    assert received == [100] * 5 + [70] * 5


def test_median_mad_resists_a_baseline_outlier():
    cfg = config()
    cfg.monitors["oil"].baseline_seconds = 5
    e = WatchEngine(cfg)
    for t, value in enumerate([98, 99, 100, 101, 150]):
        feed(e, t, value)
    row = feed(e, 5, 103)
    assert row["expected"] == 100
    assert row["residual"] == pytest.approx(3 / 1.4826)
    model = e.monitors["oil"].snapshot()
    assert model["table"]["8,37"]["mad"] == 1


def test_generated_watch_rules_select_exact_severity_and_match_finding_rows(timescale_dsn):
    sys.path.insert(0, str(ROOT / "tools"))
    import gen_alert_rules

    _, rendered = gen_alert_rules.render(PROFILE)
    rules = {r["uid"]: r for r in yaml.safe_load(rendered)["groups"][0]["rules"]}
    queries = {
        name: rules[name]["data"][0]["model"]["rawSql"]
        for name in ["watch-critical", "watch-warning"]
    }
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        apply_migrations(conn)
        for query in queries.values():
            assert conn.execute(query).fetchone()[1] == 0
        for severity in ["critical", "warning"]:
            conn.execute(
                "INSERT INTO watch_findings VALUES "
                "(gen_random_uuid(),'example-club-racer','test',now(),NULL,%s,1,'{}')",
                (severity,),
            )
        for query in queries.values():
            assert conn.execute(query).fetchone()[1] == 1
        conn.execute("UPDATE watch_findings SET closed_at=now()")
        for query in queries.values():
            assert conn.execute(query).fetchone()[1] == 0


def test_stored_file_loads_without_relearning(tmp_path, monkeypatch):
    cfg = config()
    model = trained().monitors["oil"].snapshot()
    cfg.monitors["oil"].baseline = "stored"
    cfg.monitors["oil"].stored_file = "oil.json"
    model["config"] = cfg.monitors["oil"].model_dump()
    model["source_session"] = "past-event"
    (tmp_path / "oil.json").write_text(json.dumps(model))
    (tmp_path / "watch.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    (tmp_path / "catalog.yaml").write_text((PROFILE / "catalog.yaml").read_text())
    service = WatchService(Settings(tmp_path / "watch.yaml", "v", "unused"))
    monkeypatch.setattr(service.store, "session", lambda *args: "current")
    monkeypatch.setattr(service.store, "load", lambda *args: {})
    monkeypatch.setattr(service.store, "close_previous", lambda *args: None)
    service._context(10)
    assert service.engine.monitors["oil"].frozen
    assert service.engine.monitors["oil"].learned == 5
    assert service.engine.monitors["oil"].source_session == "past-event"
