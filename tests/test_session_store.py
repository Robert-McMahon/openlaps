"""Session state cache and persistence tests."""

from pathlib import Path

from agent.session import SessionStore


def test_update_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "session-state.json"
    store = SessionStore(path)
    assert store.current() == {}

    payload = b'{"session_id": "2026-07-27-race", "driver": "Alice", "stint_number": 1}'
    decoded = store.update_from_bytes(payload)
    assert decoded is not None and decoded["driver"] == "Alice"

    reloaded = SessionStore(path)
    assert reloaded.current()["session_id"] == "2026-07-27-race"
    assert reloaded.current()["stint_number"] == 1


def test_malformed_payload_is_counted_and_state_kept(tmp_path: Path):
    store = SessionStore(tmp_path / "s.json")
    store.update({"driver": "Alice"})
    assert store.update_from_bytes(b"{not json") is None
    assert store.update_from_bytes(b'"just a string"') is None
    assert store.malformed_updates == 2
    assert store.current() == {"driver": "Alice"}


def test_corrupt_state_file_is_ignored_on_load(tmp_path: Path):
    path = tmp_path / "s.json"
    path.write_text("{broken", encoding="utf-8")
    store = SessionStore(path)
    assert store.current() == {}
    # The store still works and repairs the file on the next update.
    store.update({"driver": "Bob"})
    assert SessionStore(path).current() == {"driver": "Bob"}


def test_missing_parent_directory_is_created(tmp_path: Path):
    path = tmp_path / "state" / "nested" / "s.json"
    SessionStore(path).update({"driver": "Cam"})
    assert SessionStore(path).current() == {"driver": "Cam"}
