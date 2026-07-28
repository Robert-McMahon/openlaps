"""Session-control deploy-time settings tests."""

from pathlib import Path

import pytest

from pit.session_control.service import SessionControlSettings


def test_settings_from_env_uses_pit_wiring_only():
    settings = SessionControlSettings.from_env(
        {
            "OPENLAPS_NATS_URL": "nats://pit:4222",
            "OPENLAPS_NATS_CREDS": "/run/nats.creds",
            "OPENLAPS_VEHICLE_ID": "car-7",
            "OPENLAPS_VEHICLE_JS_DOMAIN": "vehicle-domain",
            "OPENLAPS_SESSION_STATE_FILE": "/data/session.json",
            "OPENLAPS_SESSION_ROSTER": "/config/roster.json",
            "OPENLAPS_SESSION_DEFAULT_TRACK": "Wanneroo",
            "OPENLAPS_SESSION_PORT": "9090",
            "TIMESCALE_DSN": "postgresql://db/openlaps",
        }
    )

    assert settings.nats_url == "nats://pit:4222"
    assert settings.creds_path == "/run/nats.creds"
    assert settings.vehicle_id == "car-7"
    assert settings.vehicle_js_domain == "vehicle-domain"
    assert settings.state_file == Path("/data/session.json")
    assert settings.roster_file == Path("/config/roster.json")
    assert settings.default_track == "Wanneroo"
    assert settings.http_port == 9090
    assert settings.dsn == "postgresql://db/openlaps"


def test_settings_require_vehicle_id():
    with pytest.raises(ValueError, match="OPENLAPS_VEHICLE_ID"):
        SessionControlSettings.from_env({"TIMESCALE_DSN": "postgresql://db/openlaps"})
