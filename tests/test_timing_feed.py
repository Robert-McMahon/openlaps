"""P7.10: the timing feed without a database -- framing, documents, shapes, endpoints.

Every acceptance line of the brief that does not need Timescale is here:
the packet framer against single- and multi-packet documents, a broken
chain and a bad magic; a `Leaderboard part` merging without disturbing
untouched lines; both ingest endpoints accepting their shapes; the client
surviving a severed connection and rebuilding from the next full
leaderboard; every live document captured.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import threading
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from natsoft_docs import (
    FIXTURE,
    T0,
    competitor_list,
    counters,
    full_grid,
    heartbeat,
    leaderboard,
    new,
    passing,
    position,
    race_documents,
    status,
)

from pit.timing_feed import websocket
from pit.timing_feed.capture import CaptureWriter, capture_path, read_capture
from pit.timing_feed.framing import (
    HEADER_LEN,
    PacketError,
    PacketFramer,
    encode_document,
    encode_packet,
    next_packet_id,
)
from pit.timing_feed.health import HealthState
from pit.timing_feed.ingest import MAX_REQUEST_BODY_BYTES, serve_ingest
from pit.timing_feed.model import Batch, DocumentError, FieldState, Snapshot, flag_state
from pit.timing_feed.reconcile import Finding, clock_offset_s, lap_count_finding
from pit.timing_feed.service import TimingFeedService, TimingFeedSettings
from pit.timing_feed.shapes import ShapeError, T71Translator, snapshot_from_json
from pit.timing_feed.sources import Document, NatsoftSource, ReplaySource

T1 = T0 + timedelta(seconds=100)


# --- framing


def test_packet_ids_cycle_upper_then_lower_and_wrap():
    assert next_packet_id("A") == "B"
    assert next_packet_id("Z") == "a"
    assert next_packet_id("z") == "A"
    with pytest.raises(PacketError):
        next_packet_id("=")


def test_a_single_packet_document_frames_and_decodes():
    stream = encode_document("<Heartbeat Status='Green'/>")
    assert stream[:3] == b"!@#"
    assert stream[3:8] == b"00027"
    assert stream[8:10] == b"=="
    framer = PacketFramer()
    assert framer.feed(stream) == ["<Heartbeat Status='Green'/>"]
    assert framer.buffered == 0


def test_a_multi_packet_document_is_reassembled_however_the_bytes_arrive():
    text = "<Leaderboard>" + "x" * 250 + "</Leaderboard>"
    stream = encode_document(text, chunk=100)
    # Three packets: '=' -> A, A -> B, B -> '='.
    assert stream[8:10] == b"=A"
    framer = PacketFramer()
    documents: list[str] = []
    for index in range(len(stream)):
        documents += framer.feed(stream[index : index + 1])
    assert documents == [text]


def test_two_documents_in_one_read_come_out_in_order():
    stream = encode_document("<A/>") + encode_document("<B/>", chunk=2)
    assert PacketFramer().feed(stream) == ["<A/>", "<B/>"]


def test_a_chain_that_starts_mid_cycle_is_accepted_but_a_skipped_id_is_not():
    text = "y" * 30
    ok = encode_document(text, chunk=10, first_id="x")
    assert PacketFramer().feed(ok) == [text]
    broken = encode_packet(b"part1", "=", "A") + encode_packet(b"part2", "A", "C")
    framer = PacketFramer()
    with pytest.raises(PacketError, match="does not follow"):
        framer.feed(broken)
    # The framer resets on error so the caller can redial cleanly.
    assert framer.buffered == 0
    assert framer.feed(encode_document("<ok/>")) == ["<ok/>"]


def test_a_packet_whose_previous_id_does_not_match_is_a_broken_chain():
    stream = encode_packet(b"part1", "=", "A") + encode_packet(b"part2", "Q", "=")
    with pytest.raises(PacketError, match="broken chain"):
        PacketFramer().feed(stream)


def test_a_bad_magic_and_a_bad_length_are_refused():
    with pytest.raises(PacketError, match="bad magic"):
        PacketFramer().feed(b"!@X00004==<a/>")
    with pytest.raises(PacketError, match="bad length"):
        PacketFramer().feed(b"!@#000x4==<a/>")


def test_latin_1_payload_falls_back_instead_of_failing():
    payload = "<Driver Name='Björn'/>".encode("latin-1")
    stream = encode_packet(payload, "=", "=")
    assert PacketFramer().feed(stream) == ["<Driver Name='Björn'/>"]


def test_the_framer_waits_for_a_whole_header_and_a_whole_payload():
    framer = PacketFramer()
    stream = encode_document("<Counters/>")
    assert framer.feed(stream[: HEADER_LEN - 1]) == []
    assert framer.feed(stream[HEADER_LEN - 1 : HEADER_LEN + 3]) == []
    assert framer.buffered == HEADER_LEN + 3
    assert framer.feed(stream[HEADER_LEN + 3 :]) == ["<Counters/>"]


# --- capture


def test_capture_round_trips_and_skips_a_corrupt_line(tmp_path):
    path = tmp_path / "cap.jsonl"
    writer = CaptureWriter(path)
    writer.write(T0, "<A/>")
    writer.write(T1, "<B Name='é'/>")
    writer.close()
    with path.open("a") as handle:
        handle.write("not json\n")
    assert writer.written == 2
    assert list(read_capture(path)) == [(T0, "<A/>"), (T1, "<B Name='é'/>")]


def test_capture_paths_are_per_source_and_start_time():
    assert capture_path("/data/captures", "natsoft", T0) == Path(
        "/data/captures/natsoft-20260916T040000Z.jsonl"
    )


def test_the_committed_fixture_replays_every_document_in_order():
    documents = list(read_capture(FIXTURE))
    assert len(documents) == len(race_documents()) == 30
    assert [at for at, _ in documents] == sorted(at for at, _ in documents)


# --- the model: Natsoft documents into rows


def _state() -> FieldState:
    state = FieldState("natsoft")
    state.apply_document(competitor_list(T0), T0)
    return state


def test_a_full_leaderboard_lands_every_car_with_its_registry_facts():
    state = _state()
    batch = state.apply_document(full_grid(T0, 5), T0)
    assert [car.car_number for car in batch.cars] == ["27", "14", "7", "99"]
    lead = batch.cars[0]
    assert lead.competitor_id == "1"
    assert lead.car_class == "A"
    assert lead.driver == "Driver A"
    assert lead.position == 1 and lead.class_position == 1
    assert batch.cars[2].car_class == "B" and batch.cars[2].class_position == 1
    assert lead.state == "RUN" and lead.in_pit is False
    assert lead.epoch == T0 and state.epoch == T0
    assert not batch.laps


def test_a_part_update_merges_without_disturbing_untouched_lines():
    state = _state()
    state.apply_document(full_grid(T0, 5), T0)
    part = leaderboard(
        T1, 6, "part", [position(2, "2", driver="3", laps=1, last=96.4, gap_lead=1.4)]
    )
    batch = state.apply_document(part, T1)
    assert [car.car_number for car in batch.cars] == ["14"]
    assert batch.cars[0].laps == 1 and batch.cars[0].last_lap_s == 96.4
    # Everyone else is exactly as the full leaderboard left them.
    assert state.cars["27"].laps == 0 and state.cars["27"].time == T0
    assert state.cars["99"].driver == "Rival Three"
    # And the epoch did not move: same set of cars.
    assert state.epoch == T0 and batch.cars[0].epoch == T0


def test_a_repeated_part_update_changes_nothing_and_emits_nothing():
    state = _state()
    state.apply_document(full_grid(T0, 5), T0)
    part = leaderboard(T1, 6, "part", [position(1, "1", laps=1, last=95.0)])
    assert state.apply_document(part, T1).cars
    again = state.apply_document(part, T1 + timedelta(seconds=1))
    assert not again


def test_laps_are_derived_from_increments_never_from_first_sight():
    state = _state()
    first = state.apply_document(
        leaderboard(T0, 5, "full", [position(1, "1", laps=12, last=95.0)]), T0
    )
    assert not first.laps, "a restart mid-race must not invent twelve laps"
    state.apply_document(status(T0, 6, "Yellow", "SafetyCar"), T0)
    second = state.apply_document(
        leaderboard(T1, 7, "part", [position(1, "1", laps=13, last=96.1, sectors=(30, 32, 34.1))]),
        T1,
    )
    assert len(second.laps) == 1
    lap = second.laps[0]
    assert lap.car_number == "27" and lap.lap_number == 13 and lap.lap_time_s == 96.1
    assert lap.sec3_s == 34.1 and lap.flag_state == "yellow" and lap.sub_status == "sc"


def test_a_full_leaderboard_with_a_different_field_starts_a_new_epoch():
    state = _state()
    state.apply_document(full_grid(T0, 5), T0)
    smaller = leaderboard(
        T1, 6, "full", [position(1, "1", laps=1), position(2, "3", driver="4", laps=1)]
    )
    batch = state.apply_document(smaller, T1)
    assert state.epoch == T1
    assert {car.car_number for car in batch.cars} == {"27", "7"}
    assert all(car.epoch == T1 for car in batch.cars)
    assert set(state.cars) == {"27", "7"}


def test_a_new_registry_renames_cars_and_a_missing_registry_falls_back_to_the_id():
    state = FieldState("natsoft")
    batch = state.apply_document(full_grid(T0, 5), T0)
    assert [car.car_number for car in batch.cars] == ["1", "2", "3", "4"]
    assert batch.cars[0].driver is None
    renamed = state.apply_document(competitor_list(T1), T1)
    assert {car.car_number for car in renamed.cars} == {"27", "14", "7", "99"}


def test_pit_lane_and_out_lap_flags_become_the_car_state():
    state = _state()
    grid = leaderboard(
        T0,
        5,
        "full",
        [
            position(1, "1", pit_flag="P", pit_stops=1),
            position(2, "2", driver="3", out_lap="Y"),
            position(3, "3", driver="4", pos="DNF"),
        ],
    )
    batch = state.apply_document(grid, T0)
    by_number = {car.car_number: car for car in batch.cars}
    assert (
        by_number["27"].state == "PIT" and by_number["27"].in_pit and by_number["27"].pit_count == 1
    )
    assert by_number["14"].state == "OUT"
    assert by_number["7"].state == "DNF" and by_number["7"].position is None


def test_session_rows_land_on_change_only_and_carry_the_clock_and_flag():
    state = _state()
    first = state.apply_document(status(T0, 4, "WaitStart"), T0)
    assert first.session is not None and first.session.flag_state == "none"
    same = state.apply_document(heartbeat(T0, 5, "WaitStart", track_temp=None or 31.0), T0)
    assert same.session is not None and same.session.track_temp == 31.0
    again = state.apply_document(heartbeat(T0, 6, "WaitStart", track_temp=31.0), T0)
    assert again.session is None, "nothing changed"
    clock = state.apply_document(
        counters(T1, 7, kind="Time", count=21480, elapsed=120, state="Green"), T1
    )
    assert clock.session is not None
    assert clock.session.flag_state == "green"
    assert clock.session.time_remaining_s == 21480 and clock.session.time_elapsed_s == 120
    sc = state.apply_document(status(T1, 8, "Yellow", "SafetyCar"), T1)
    assert sc.session is not None and (sc.session.flag_state, sc.session.sub_status) == (
        "yellow",
        "sc",
    )
    green = state.apply_document(status(T1, 9, "Green"), T1)
    assert green.session is not None and green.session.sub_status is None
    laps = state.apply_document(counters(T1, 10, kind="Laps", count=40, elapsed=130), T1)
    assert laps.session is not None and laps.session.laps_remaining == 40


def test_the_event_document_names_the_session():
    state = FieldState("natsoft")
    batch = state.apply_document(race_documents()[0][1], T0)
    assert batch.session is not None
    assert batch.session.session_name == "6 Hour Regularity Relay"
    assert batch.session.event_type == "Race"


def test_a_passing_carries_the_line_the_timekeepers_stamp_and_the_car_number():
    state = _state()
    batch = state.apply_document(passing(T1, 9, "2", kind=2), T1)
    assert len(batch.passings) == 1
    row = batch.passings[0]
    assert row.competitor_id == "2" and row.car_number == "14"
    assert row.line == "pit_main" and row.passing_type == 2 and row.active == "Active"
    assert row.tod == T1 and row.time == T1
    unknown = state.apply_document(passing(T1, 10, "Safety", kind=71), T1).passings[0]
    assert unknown.car_number is None and unknown.line == "71"


def test_a_new_container_dispatches_each_child_and_unknown_tags_are_counted():
    state = _state()
    state.apply_document(full_grid(T0, 5), T0)
    document = new(
        T1,
        20,
        [
            passing(T1, 21, "1"),
            leaderboard(T1, 22, "part", [position(1, "1", laps=1, last=95.0)]),
            counters(T1, 23, kind="Time", count=21500, elapsed=100),
            "<PointsSeries Lines='0'/>",
        ],
    )
    batch = state.apply_document(document, T1)
    assert len(batch.passings) == 1 and len(batch.cars) == 1 and len(batch.laps) == 1
    assert batch.session is not None and batch.session.time_elapsed_s == 100
    assert state.unknown_tags == {"PointsSeries": 1}
    assert batch.rows == 4


def test_junk_is_a_document_error_not_a_crash():
    state = FieldState("natsoft")
    with pytest.raises(DocumentError):
        state.apply_document("<Leaderboard", T0)
    too_many = leaderboard(T0, 1, "full", [position(i, str(i)) for i in range(1, 202)])
    with pytest.raises(DocumentError, match="cap"):
        state.apply_document(too_many, T0)


def test_the_whole_fixture_derives_the_laps_the_story_tells():
    state = FieldState("natsoft")
    laps: list = []
    sessions: list = []
    for at, document in race_documents():
        batch = state.apply_document(document, at)
        laps += batch.laps
        if batch.session:
            sessions.append(batch.session)
    by_car: dict[str, list[int]] = {}
    for lap in laps:
        by_car.setdefault(lap.car_number, []).append(lap.lap_number)
    assert by_car == {"27": [1, 2, 3, 4], "14": [1, 2, 3], "7": [1, 2, 3], "99": [1, 2, 3]}
    flags = [(s.flag_state, s.sub_status) for s in sessions]
    assert ("yellow", "sc") in flags and flags[-1] == ("ended", None)
    assert state.cars["14"].pit_count == 1
    assert state.unknown_tags == {"Track": 1}


def test_flag_vocabulary_is_timing71s_whatever_the_source_says():
    assert flag_state("Checkered") == "chequered"
    assert flag_state("WaitStart") == "none"
    assert flag_state("sc") == "yellow"
    assert flag_state("") is None
    assert flag_state("Purple") == "purple"


# --- the other two shapes


def _snapshot_body(**overrides):
    body = {
        "source": "relay",
        "session": {"flag_state": "green", "time_remaining_s": "1:59:30", "track_temp": 32},
        "cars": [
            {
                "car_number": "27",
                "laps": 12,
                "last_lap_s": "1:35.200",
                "gap_lead_s": "",
                "state": "RUN",
            },
            {
                "car_number": "14",
                "laps": 12,
                "last_lap_s": 96.4,
                "gap_lead_s": "+1.4",
                "class": "A",
            },
            {"car_number": "7", "laps": 11, "gap_lead_s": "1 lap", "state": "PIT"},
        ],
    }
    body.update(overrides)
    return body


def test_a_relay_snapshot_maps_onto_the_schema_with_positions_from_order():
    snapshot = snapshot_from_json(_snapshot_body(), T0, "relay")
    assert snapshot.source == "relay"
    assert snapshot.session is not None
    assert snapshot.session.flag_state == "green"
    assert snapshot.session.time_remaining_s == 7170.0 and snapshot.session.track_temp == 32.0
    numbers = [car.car_number for car in snapshot.cars]
    assert numbers == ["27", "14", "7"]
    assert [car.position for car in snapshot.cars] == [1, 2, 3]
    assert snapshot.cars[0].last_lap_s == 95.2 and snapshot.cars[0].gap_lead_s is None
    assert snapshot.cars[1].gap_lead_s == 1.4 and snapshot.cars[1].car_class == "A"
    assert snapshot.cars[2].gap_lead_s is None and snapshot.cars[2].in_pit is True


def test_a_relay_snapshot_is_checked_and_bounded():
    with pytest.raises(ShapeError, match="cars must be a list"):
        snapshot_from_json({"cars": {}}, T0, "relay")
    with pytest.raises(ShapeError, match="car_number"):
        snapshot_from_json({"cars": [{"laps": 1}]}, T0, "relay")
    with pytest.raises(ShapeError, match="twice"):
        snapshot_from_json({"cars": [{"car_number": "1"}, {"car_number": "1"}]}, T0, "relay")
    with pytest.raises(ShapeError, match="cap"):
        snapshot_from_json({"cars": [{"car_number": str(i)} for i in range(201)]}, T0, "relay")
    with pytest.raises(ShapeError, match="session must be an object"):
        snapshot_from_json({"cars": [], "session": 3}, T0, "relay")


def test_a_snapshot_applied_to_the_state_diffs_like_a_full_leaderboard():
    state = FieldState("relay")
    first = state.apply_snapshot(snapshot_from_json(_snapshot_body(), T0, "relay"))
    assert len(first.cars) == 3 and first.session is not None and not first.laps
    body = _snapshot_body()
    body["cars"][0]["laps"] = 13
    second = state.apply_snapshot(snapshot_from_json(body, T1, "relay"))
    assert [car.car_number for car in second.cars] == ["27"]
    assert len(second.laps) == 1 and second.laps[0].lap_number == 13
    assert second.session is None


def test_timing71_state_reads_against_its_manifest():
    translator = T71Translator()
    with pytest.raises(ShapeError, match="before any MANIFEST"):
        translator.state({"cars": []}, T0)
    translator.manifest(
        {
            "colSpec": [
                ["Num", "text", "Car number"],
                ["State", "text"],
                ["Class", "class"],
                ["PIC", "numeric", "Position in class"],
                ["Driver", "text"],
                ["Laps", "numeric"],
                ["Gap", "delta", "Gap to leader"],
                ["Int", "delta", "Interval to car in front"],
                ["S1", "time"],
                ["S2", "time"],
                ["S3", "time"],
                ["Last", "time", "Last lap time"],
                ["Best", "time", "Best lap time"],
                ["Pits", "numeric"],
            ],
            "description": "4H of Sepang - Race",
            "trackDataSpec": ["Air temperature", "Track temperature"],
        }
    )
    snapshot = translator.state(
        {
            "cars": [
                [
                    "27",
                    "RUN",
                    "A",
                    1,
                    "Driver A",
                    24,
                    "",
                    "",
                    [30.1, ""],
                    [32.0, "pb"],
                    ["", ""],
                    [95.2, "sb"],
                    [94.8, ""],
                    1,
                ],
                [
                    "14",
                    "PIT",
                    "A",
                    2,
                    "Rival One",
                    24,
                    "1.434",
                    "1.434",
                    [30.5, ""],
                    [32.4, ""],
                    [33.0, ""],
                    [96.4, ""],
                    [96.4, ""],
                    2,
                ],
                [
                    "7",
                    "RUN",
                    "B",
                    1,
                    "Rival Two",
                    23,
                    "1 lap",
                    "1 lap",
                    [],
                    [],
                    [],
                    [98.0, ""],
                    [97.0, ""],
                    1,
                ],
            ],
            "session": {
                "timeElapsed": 5688.4,
                "timeRemain": 1511.6,
                "flagState": "sc",
                "trackData": ["23°C", "34°C"],
            },
            "messages": [],
        },
        T1,
    )
    assert [car.car_number for car in snapshot.cars] == ["27", "14", "7"]
    lead = snapshot.cars[0]
    assert lead.position == 1 and lead.class_position == 1 and lead.driver == "Driver A"
    assert lead.laps == 24 and lead.sec2_s == 32.0 and lead.sec3_s is None
    assert lead.last_lap_s == 95.2 and lead.best_lap_s == 94.8 and lead.pit_count == 1
    assert snapshot.cars[1].gap_lead_s == 1.434 and snapshot.cars[1].in_pit is True
    assert snapshot.cars[2].gap_lead_s is None and snapshot.cars[2].sec1_s is None
    session = snapshot.session
    assert session is not None
    assert (session.flag_state, session.sub_status) == ("yellow", "sc")
    assert session.time_remaining_s == 1511.6 and session.track_temp == 34.0
    assert session.session_name == "4H of Sepang - Race"


# --- reconciliation


def test_lap_count_findings_say_who_is_higher_and_why():
    assert lap_count_finding("27", None, 5) is None
    assert lap_count_finding("27", 5, 5) is None
    assert lap_count_finding("27", 6, 5) is None, "a lap apart is timing, not a fault"
    feed_ahead = lap_count_finding("27", 8, 5)
    assert feed_ahead is not None
    assert feed_ahead.severity == "warning" and feed_ahead.summary["higher"] == "feed"
    assert "missed a line crossing" in str(feed_ahead.summary["message"])
    vehicle_ahead = lap_count_finding("27", 5, 10)
    assert vehicle_ahead is not None
    assert vehicle_ahead.severity == "critical" and vehicle_ahead.summary["higher"] == "vehicle"
    assert "transponder" in str(vehicle_ahead.summary["likely_cause"])
    assert lap_count_finding("27", 8, 5, tolerance=3) is None


def test_clock_offset_is_the_nearest_crossing_within_the_window():
    tod = T1
    crossings = [T0, T1 + timedelta(seconds=0.35), T1 + timedelta(seconds=90)]
    assert clock_offset_s(tod, crossings) == 0.35
    assert clock_offset_s(tod, [T0]) is None
    assert clock_offset_s(tod, [T1 - timedelta(seconds=2)]) == -2.0


# --- the websocket codec


def test_the_accept_key_matches_the_rfc_example():
    assert websocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 5, 125, 126, 65535, 65536])
def test_frames_round_trip_at_every_length_encoding(size):
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    frame = websocket.encode_frame(websocket.OP_BINARY, payload, mask=b"\x01\x02\x03\x04")
    fin, opcode, decoded = websocket.read_frame(BytesIO(frame))
    assert (fin, opcode, decoded) == (True, websocket.OP_BINARY, payload)


def test_messages_answer_pings_assemble_fragments_and_stop_at_close():
    mask = b"\xaa\xbb\xcc\xdd"
    stream = BytesIO(
        websocket.encode_frame(websocket.OP_PING, b"hi", mask=mask)
        + _fragmented('{"type": ', '"MANIFEST_UPDATE"}', mask)
        + websocket.encode_frame(websocket.OP_TEXT, b'{"type":"STATE_UPDATE"}', mask=mask)
        + websocket.encode_frame(websocket.OP_CLOSE, b"\x03\xe8", mask=mask)
        + websocket.encode_frame(websocket.OP_TEXT, b"never read", mask=mask)
    )
    out = BytesIO()
    received = list(websocket.messages(stream, out))
    assert received == ['{"type": "MANIFEST_UPDATE"}', '{"type":"STATE_UPDATE"}']
    replies = out.getvalue()
    assert replies.startswith(websocket.encode_frame(websocket.OP_PONG, b"hi"))
    assert replies.endswith(websocket.encode_close())


def _fragmented(first: str, second: str, mask: bytes) -> bytes:
    head = bytearray(websocket.encode_frame(websocket.OP_TEXT, first.encode(), mask=mask))
    head[0] &= 0x7F  # clear FIN
    tail = websocket.encode_frame(websocket.OP_CONTINUATION, second.encode(), mask=mask)
    return bytes(head) + tail


def test_an_unmasked_client_frame_is_a_protocol_error():
    stream = BytesIO(websocket.encode_frame(websocket.OP_TEXT, b"{}"))
    with pytest.raises(websocket.WebSocketError, match="not masked"):
        list(websocket.messages(stream, BytesIO()))


# --- settings


def _env(**extra: str) -> dict[str, str]:
    base = {
        "OPENLAPS_VEHICLE_ID": "example-club-racer",
        "TIMESCALE_HOST": "db",
        "TIMESCALE_DB": "openlaps",
        "TIMESCALE_USER": "openlaps",
    }
    base.update(extra)
    return base


def test_settings_default_to_the_public_feed_and_refuse_bad_values():
    settings = TimingFeedSettings.from_env(_env())
    assert settings.source_kind == "natsoft"
    assert (settings.feed_host, settings.feed_port) == ("natsoft.com.au", 8889)
    assert settings.http_port == 8089 and settings.replay_paced
    local = TimingFeedSettings.from_env(
        _env(OPENLAPS_TIMING_FEED_HOST="192.168.12.10", OPENLAPS_TIMING_FEED_HOST_PORT="8889")
    )
    assert local.feed_host == "192.168.12.10"
    with pytest.raises(ValueError, match="OPENLAPS_VEHICLE_ID"):
        TimingFeedSettings.from_env(_env(OPENLAPS_VEHICLE_ID=""))
    with pytest.raises(ValueError, match="one of"):
        TimingFeedSettings.from_env(_env(OPENLAPS_TIMING_FEED_SOURCE="browser"))
    with pytest.raises(ValueError, match="REPLAY_FILE"):
        TimingFeedSettings.from_env(_env(OPENLAPS_TIMING_FEED_SOURCE="replay"))
    with pytest.raises(ValueError, match="between"):
        TimingFeedSettings.from_env(_env(OPENLAPS_TIMING_FEED_PORT="70000"))
    replay = TimingFeedSettings.from_env(
        _env(
            OPENLAPS_TIMING_FEED_SOURCE="replay",
            OPENLAPS_TIMING_FEED_REPLAY_FILE="/x.jsonl",
            OPENLAPS_TIMING_FEED_REPLAY_PACED="0",
        )
    )
    assert replay.replay_file == "/x.jsonl" and not replay.replay_paced


# --- the ingest endpoints, against the real server


class _Sink:
    def __init__(self) -> None:
        self.snapshots: list[Snapshot] = []
        self.event = threading.Event()

    def __call__(self, snapshot: Snapshot) -> None:
        self.snapshots.append(snapshot)
        self.event.set()


@pytest.fixture
def ingest_server():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sink = _Sink()
    health = HealthState()
    server = serve_ingest(loop, sink, health, port=0, host="127.0.0.1")
    try:
        yield server.server_port, sink, health
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def _post(port: int, path: str, body: bytes, content_type: str = "application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": content_type, "Origin": "https://www.timing71.org"},
    )
    response = connection.getresponse()
    payload = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, payload


def test_the_snapshot_endpoint_accepts_json_from_any_origin_and_refuses_junk(ingest_server):
    port, sink, health = ingest_server
    status_code, payload = _post(port, "/ingest/snapshot", json.dumps(_snapshot_body()).encode())
    assert (status_code, payload) == (200, {"accepted": 3})
    assert sink.event.wait(2)
    assert sink.snapshots[0].cars[0].car_number == "27"
    # A no-cors fetch from the relay can only send text/plain; that is JSON too.
    sink.event.clear()
    status_code, _ = _post(
        port, "/ingest/snapshot", json.dumps(_snapshot_body()).encode(), "text/plain"
    )
    assert status_code == 200 and sink.event.wait(2)
    assert _post(port, "/ingest/snapshot", b'{"cars": "no"}')[0] == 400
    assert _post(port, "/ingest/snapshot", b"[]")[0] == 400
    assert _post(port, "/ingest/snapshot", b"{}", "application/xml")[0] == 415
    assert _post(port, "/ingest/snapshot", b"{" * (MAX_REQUEST_BODY_BYTES + 1))[0] == 413
    assert _post(port, "/ingest/other", b"{}")[0] == 404
    assert health.ingest_snapshots == 2 and health.ingest_rejected == 1
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/health")
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read())["ingest_snapshots"] == 2
    connection.request("GET", "/ingest/t71")
    assert connection.getresponse().status == 426


def _ws_connect(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        b"GET /ingest/t71 HTTP/1.1\r\nHost: pit\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        # The handshake nonce from RFC 6455 section 1.3, not a credential.
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"  # gitleaks:allow
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)
    assert response.startswith(b"HTTP/1.1 101")
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in response
    return sock


def _ws_send(sock: socket.socket, message: dict) -> None:
    sock.sendall(
        websocket.encode_frame(websocket.OP_TEXT, json.dumps(message).encode(), mask=os.urandom(4))
    )


def test_the_t71_endpoint_takes_a_manifest_then_states_over_a_websocket(ingest_server):
    port, sink, health = ingest_server
    sock = _ws_connect(port)
    _ws_send(sock, {"type": "STATE_UPDATE", "state": {"cars": []}})  # before any manifest
    _ws_send(
        sock,
        {
            "type": "MANIFEST_UPDATE",
            "manifest": {"colSpec": [["Num", "text"], ["Laps", "numeric"], ["Last", "time"]]},
        },
    )
    _ws_send(sock, {"type": "ANALYSIS_STATE", "data": {}})
    _ws_send(
        sock,
        {
            "type": "STATE_UPDATE",
            "state": {
                "cars": [["27", 3, [95.1, ""]], ["14", 3, [96.0, ""]]],
                "session": {"flagState": "green", "timeRemain": 100},
            },
        },
    )
    assert sink.event.wait(2)
    snapshot = sink.snapshots[0]
    assert snapshot.source == "t71"
    assert [car.car_number for car in snapshot.cars] == ["27", "14"]
    assert snapshot.cars[0].last_lap_s == 95.1 and snapshot.cars[1].position == 2
    assert snapshot.session is not None and snapshot.session.flag_state == "green"
    sock.sendall(websocket.encode_frame(websocket.OP_CLOSE, b"\x03\xe8", mask=os.urandom(4)))
    reply = sock.recv(64)
    assert reply[:1] == bytes([0x80 | websocket.OP_CLOSE])
    sock.close()
    assert health.ingest_t71_messages == 2 and health.ingest_rejected == 1


# --- the natsoft client, against a fake feed that cuts the connection


async def _fake_feed(scripts: list[list[bytes]], connections: list[int]):
    """A TCP server that plays one script per connection, then closes it."""

    async def handle(reader, writer):
        index = len(connections)
        connections.append(index)
        script = scripts[min(index, len(scripts) - 1)]
        try:
            for chunk in script:
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0.01)
            if index < len(scripts) - 1:
                writer.close()
                return
            # The last script keeps the connection open until the client goes.
            await reader.read(1)
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_the_client_survives_a_severed_connection_and_rebuilds_from_the_next_full_leaderboard():
    async def scenario():
        connections: list[int] = []
        grid = full_grid(T0, 5)
        after = leaderboard(
            T1,
            9,
            "full",
            [position(1, "1", laps=7, last=95.0), position(2, "2", driver="3", laps=7)],
        )
        server = await _fake_feed(
            [
                [encode_document(competitor_list(T0)), encode_document(grid, chunk=64)],
                # A stream that breaks its own framing: the client must drop it too.
                [b"!@#0000x==junk"],
                [encode_document(after), encode_document(heartbeat(T1, 10, "Green"))],
            ],
            connections,
        )
        port = server.sockets[0].getsockname()[1]
        stop = asyncio.Event()
        health = HealthState()
        source = NatsoftSource("127.0.0.1", port, health=health, backoff_s=(0.05, 0.1), stop=stop)
        state = FieldState("natsoft")
        seen: list[str] = []
        async with server:
            async for document in source:
                seen.append(document.text[:12])
                state.apply_document(document.text, document.at)
                if "Heartbeat" in document.text:
                    stop.set()
                    break
        assert len(connections) == 3
        assert health.source_reconnects == 2 and not health.source_connected
        assert seen[0].startswith("<CompetitorL") and seen[-1].startswith("<Heartbeat")
        # Rebuilt from the full leaderboard after the cut: two cars, lap 7,
        # nothing derived from the jump because the epoch changed.
        assert set(state.cars) == {"27", "14"}
        assert state.cars["27"].laps == 7 and state.epoch is not None

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


# --- the service, against a fake database


class _FakeDatabase:
    def __init__(self) -> None:
        self.batches: list[Batch] = []
        self.metrics: list[tuple[datetime, str, float]] = []
        self.findings: list[list[Finding]] = []
        self.open_findings: dict[str, object] = {}
        self.our = None
        self.laps = (0, None)
        self.crossings: list[datetime] = []
        self.closed = False

    def write(self, batch: Batch) -> int:
        self.batches.append(batch)
        return batch.rows

    def write_metric(self, at, metric, value):
        self.metrics.append((at, metric, value))

    def our_car(self):
        return self.our

    def vehicle_laps(self, session_id):
        return self.laps

    def vehicle_crossings(self, session_id, since):
        return self.crossings

    def adopt_open_findings(self):
        return 0

    def reconcile_findings(self, findings, at):
        self.findings.append(findings)
        self.open_findings = {f.monitor: object() for f in findings}

    def close(self):
        self.closed = True


def _settings(**overrides) -> TimingFeedSettings:
    values = dict(dsn="postgresql://x", vehicle_id="example-club-racer", source_kind="natsoft")
    values.update(overrides)
    return TimingFeedSettings(**values)


def test_the_service_writes_each_document_and_captures_every_live_one(tmp_path):
    async def scenario():
        database = _FakeDatabase()
        service = TimingFeedService(
            _settings(capture_dir=str(tmp_path)), database=database, clock=lambda: T0
        )
        stop = asyncio.Event()
        runner = asyncio.create_task(service.run(stop))
        await asyncio.sleep(0.05)
        for at, document in race_documents():
            await service.handle_document(Document(at, document), capture=True)
        stop.set()
        await runner
        return database, service

    database, service = asyncio.run(scenario())
    assert service.health.documents == 30
    assert service.health.rows_written == sum(batch.rows for batch in database.batches) > 0
    assert service.health.capture_path is not None
    replayed = list(read_capture(service.health.capture_path))
    assert len(replayed) == 30 and service.health.capture_written == 30
    assert database.closed


def test_replay_drives_the_service_unpaced_and_a_snapshot_joins_through_the_queue():
    async def scenario():
        database = _FakeDatabase()
        service = TimingFeedService(
            _settings(source_kind="replay", replay_file=str(FIXTURE), replay_paced=False),
            database=database,
        )
        stop = asyncio.Event()
        runner = asyncio.create_task(service.run(stop))
        await asyncio.wait_for(service.source_finished.wait(), timeout=10)
        service.submit(snapshot_from_json(_snapshot_body(), T1 + timedelta(hours=1), "relay"))
        await asyncio.sleep(0.1)
        stop.set()
        await runner
        return database, service

    database, service = asyncio.run(scenario())
    laps = [lap for batch in database.batches for lap in batch.laps]
    assert len(laps) == 13
    assert service.health.documents == 31 and service.health.capture_path is None
    assert database.batches[-1].cars[0].source == "replay"


def test_reconciliation_opens_a_finding_on_disagreement_and_closes_it_on_agreement():
    async def scenario():
        database = _FakeDatabase()
        service = TimingFeedService(_settings(reconcile_s=100), database=database, clock=lambda: T1)
        service.state.apply_document(competitor_list(T0), T0)
        service.state.apply_document(full_grid(T0, 5), T0)
        service.state.apply_document(
            leaderboard(T1, 6, "part", [position(1, "1", laps=9, last=95.0)]), T1
        )
        assert await service.reconcile() == []
        assert service.health.our_car is None
        from pit.timing_feed.database import OurCar

        database.our = OurCar("s-1", T0, "27")
        database.laps = (4, T1)
        findings = await service.reconcile()
        assert len(findings) == 1 and findings[0].summary["delta"] == 5
        assert service.health.feed_laps == 9 and service.health.vehicle_laps == 4
        assert service.health.findings_open == 1
        database.laps = (9, T1)
        assert await service.reconcile() == []
        assert service.health.findings_open == 0
        # A main-line passing for us with a crossing 0.4 s later: the offset.
        database.crossings = [T1 + timedelta(seconds=0.4)]
        batch = service.state.apply_document(passing(T1, 7, "1"), T1 + timedelta(seconds=1.5))
        service._note_passings(batch)
        await service.reconcile()
        return database, service

    database, service = asyncio.run(scenario())
    assert service.health.clock_offset_s == 0.4
    metrics = {metric: value for _, metric, value in database.metrics}
    assert metrics == {"clock_offset_s": 0.4, "feed_latency_s": 1.5}


def test_a_replay_source_paces_by_recorded_gaps(tmp_path):
    path = tmp_path / "cap.jsonl"
    writer = CaptureWriter(path)
    writer.write(T0, "<A/>")
    writer.write(T0 + timedelta(seconds=0.2), "<B/>")
    writer.close()

    async def run(paced: bool):
        started = asyncio.get_running_loop().time()
        docs = [d.text async for d in ReplaySource(path, paced=paced)]
        return docs, asyncio.get_running_loop().time() - started

    docs, took = asyncio.run(run(True))
    assert docs == ["<A/>", "<B/>"] and took >= 0.15
    _, quick = asyncio.run(run(False))
    assert quick < 0.15


# --- the deploy wiring: entry point, compose, env, the topology row


def test_entrypoint_compose_env_and_docs_wire_the_service():
    repo = Path(__file__).parents[1]
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    compose = (repo / "deploy" / "pit-compose.yaml").read_text(encoding="utf-8")
    env = (repo / "example.env").read_text(encoding="utf-8")
    operations = (repo / "docs" / "operations" / "verification.md").read_text(encoding="utf-8")
    schema = (repo / "docs" / "PIT_SCHEMA.md").read_text(encoding="utf-8")

    assert 'openlaps-timing-feed = "pit.timing_feed.__main__:main"' in pyproject
    assert "\n  timing-feed:\n" in compose
    assert 'command: ["openlaps-timing-feed"]' in compose
    assert '"8089:8089"' in compose
    assert "timing-feed-data:/data" in compose and "\n  timing-feed-data:\n" in compose
    assert "OPENLAPS_TIMING_FEED_SOURCE=natsoft" in env
    assert "OPENLAPS_TIMING_FEED_PORT=8089" in env
    assert "| 8089 | timing-feed |" in operations
    for view in (
        "v_field_standings",
        "v_field_laps",
        "v_field_passings",
        "v_field_flags",
        "v_field_gaps",
    ):
        assert f"| `{view}` |" in schema
