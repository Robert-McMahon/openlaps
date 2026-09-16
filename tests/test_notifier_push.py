"""The push channels (P7.3): what reaches a phone, and how failure is classified."""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pit.notifier.channels import Broadcaster, Message, Retryable
from pit.notifier.config import ChannelConfig, NotifierConfig, NotifierSettings, load_config
from pit.notifier.push import DiscordChannel, NtfyChannel
from pit.notifier.service import build_channels

ROOT = Path(__file__).parents[1]
T0 = datetime(2026, 9, 16, 3, 14, 0, tzinfo=UTC)


def _message(kind: str = "firing", severity: str = "critical") -> Message:
    return Message(
        kind=kind,
        title=f"{kind.upper()}: Oil pressure low against RPM",
        body="Oil pressure low against RPM (critical)",
        severity=severity,
        fingerprint="abc123",
        at=T0,
        url="/d/reliability/reliability?var-alert=oil-pressure-low",
    )


class _Opener:
    def __init__(self, status: int = 200, raise_retryable: bool = False) -> None:
        self.status = status
        self.raise_retryable = raise_retryable
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout_s: float) -> int:
        self.requests.append(request)
        if self.raise_retryable:
            raise Retryable("connection refused")
        return self.status

    def last_json(self) -> dict:
        return json.loads(self.requests[-1].data.decode())


def test_ntfy_routes_by_topic_priority_and_carries_an_acknowledge_action():
    opener = _Opener()
    channel = NtfyChannel(
        url="http://ntfy/",
        public_url="http://192.168.12.203:8086/",
        token="secret-x",
        opener=opener,
    )
    channel.deliver(_message())
    request = opener.requests[-1]
    assert request.full_url == "http://ntfy"
    assert request.get_header("Authorization") == "Bearer secret-x"
    payload = opener.last_json()
    assert payload["topic"] == "openlaps-critical" and payload["priority"] == 5
    assert payload["click"] == "http://192.168.12.203:8086/"
    ack = payload["actions"][0]
    assert ack["action"] == "http" and ack["url"] == "http://192.168.12.203:8086/ack"
    assert json.loads(ack["body"]) == {"fingerprint": "abc123", "by": "phone (ntfy)"}

    channel.deliver(_message(severity="warning"))
    payload = opener.last_json()
    assert payload["topic"] == "openlaps-warning" and payload["priority"] == 3

    channel.deliver(_message(kind="resolved"))
    payload = opener.last_json()
    assert payload["priority"] == 2 and "actions" not in payload


def test_ntfy_without_a_public_url_or_token_sends_a_plain_push():
    opener = _Opener()
    NtfyChannel(opener=opener).deliver(_message())
    payload = opener.last_json()
    assert "click" not in payload and "actions" not in payload
    assert opener.requests[-1].get_header("Authorization") is None


@pytest.mark.parametrize("status", [429, 500, 503])
def test_server_side_failures_are_retryable_and_client_errors_are_not(status: int):
    with pytest.raises(Retryable):
        NtfyChannel(opener=_Opener(status=status)).deliver(_message())
    with pytest.raises(RuntimeError, match="not retrying"):
        NtfyChannel(opener=_Opener(status=404)).deliver(_message())
    with pytest.raises(Retryable):
        NtfyChannel(opener=_Opener(raise_retryable=True)).deliver(_message())


def test_discord_posts_an_embed_and_never_echoes_its_webhook_in_an_error():
    opener = _Opener()
    channel = DiscordChannel(
        webhook_url="https://discord.example/api/webhooks/123/very-secret",
        public_url="http://192.168.12.203:8086",
        opener=opener,
    )
    channel.deliver(_message())
    payload = opener.last_json()
    assert payload["username"] == "openlaps"
    embed = payload["embeds"][0]
    assert embed["title"].startswith("FIRING") and embed["url"] == "http://192.168.12.203:8086/"
    assert embed["color"] == 0xB00020

    failing = DiscordChannel(
        webhook_url="https://discord.example/api/webhooks/123/very-secret",
        opener=_Opener(status=400),
    )
    with pytest.raises(RuntimeError) as excinfo:
        failing.deliver(_message())
    assert "very-secret" not in str(excinfo.value)
    with pytest.raises(ValueError):
        DiscordChannel(webhook_url="")


def test_the_shipped_config_builds_phones_and_skips_discord_without_its_secret():
    config = load_config(ROOT / "deploy/pit-config/notifier.yaml")
    settings = NotifierSettings(config_path=Path("x"), dsn=None)
    names = [c.name for c in build_channels(config, Broadcaster(), settings)]
    assert names == ["annunciator", "log", "phones"]

    with_discord = NotifierSettings(
        config_path=Path("x"), dsn=None, discord_webhook="https://discord.example/hook"
    )
    names = [c.name for c in build_channels(config, Broadcaster(), with_discord)]
    assert names == ["annunciator", "log", "phones", "discord"]


def test_settings_read_the_push_secrets_and_the_public_url_from_the_environment():
    settings = NotifierSettings.from_env(
        {
            "OPENLAPS_NOTIFIER_PUBLIC_URL": "http://192.168.12.203:8086 ",
            "OPENLAPS_NTFY_TOKEN": "tk",
            "OPENLAPS_DISCORD_WEBHOOK": "",
        }
    )
    assert settings.public_url == "http://192.168.12.203:8086"
    assert settings.ntfy_token == "tk" and settings.discord_webhook is None


def test_an_unknown_channel_type_is_refused_by_the_config_not_the_builder():
    with pytest.raises(ValueError):
        NotifierConfig(channels=(ChannelConfig(name="x", type="pigeon"),))  # type: ignore[arg-type]
