"""The channels that reach a phone: ntfy on the pit LAN, and Discord.

Both are one HTTP POST per message over the standard library, with an
injectable opener so the tests never touch a network. A connection failure,
a timeout, a 429 or a 5xx is `Retryable` and goes to the dispatcher's queue;
any other 4xx is a bug in the request and is logged, not retried.

Secrets never come from the YAML: the ntfy access token and the Discord
webhook URL arrive through the environment (`example.env`), and an empty
Discord URL disables that channel with a log line rather than an error.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable

from pit.notifier.channels import Message, Retryable

logger = logging.getLogger(__name__)

_TIMEOUT_S = 5.0

# status code returned; raises Retryable for anything network-shaped.
Opener = Callable[[urllib.request.Request, float], int]


def http_post(request: urllib.request.Request, timeout_s: float) -> int:
    """POST and return the status code; network-shaped failures are Retryable."""
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Retryable(str(exc)) from exc


def _raise_for_status(name: str, status: int, target: str) -> None:
    if 200 <= status < 300:
        return
    if status == 429 or status >= 500:
        raise Retryable(f"{name}: HTTP {status} from {target}")
    raise RuntimeError(f"{name}: HTTP {status} from {target}; not retrying")


def _json_request(
    url: str, payload: dict[str, object], headers: dict[str, str]
) -> urllib.request.Request:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "openlaps-notifier")
    for key, value in headers.items():
        request.add_header(key, value)
    return request


class NtfyChannel:
    """Push to phones through a self-hosted ntfy server on the pit LAN.

    Two topics -- critical and warning -- so a phone can subscribe to one
    without the other. Firing and repeats carry an Acknowledge action that
    POSTs straight to the notifier's ``/ack``; resolutions and
    acknowledgements go out at low priority so the phone sees them clear.
    """

    def __init__(
        self,
        *,
        name: str = "phones",
        min_severity: str = "warning",
        repeat_s: float | None = None,
        url: str = "http://ntfy",
        critical_topic: str = "openlaps-critical",
        warning_topic: str = "openlaps-warning",
        public_url: str | None = None,
        token: str | None = None,
        opener: Opener = http_post,
    ) -> None:
        self.name = name
        self.min_severity = min_severity
        self.repeat_s = repeat_s
        self.url = url.rstrip("/")
        self.critical_topic = critical_topic
        self.warning_topic = warning_topic
        self.public_url = public_url.rstrip("/") if public_url else None
        self._token = token or None
        self._opener = opener

    def deliver(self, message: Message) -> None:
        topic = self.critical_topic if message.severity == "critical" else self.warning_topic
        payload = self.payload(message, topic)
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        status = self._opener(_json_request(self.url, payload, headers), _TIMEOUT_S)
        _raise_for_status(self.name, status, f"{self.url} topic {topic}")

    def payload(self, message: Message, topic: str) -> dict[str, object]:
        firing = message.kind in ("firing", "repeat")
        if firing:
            priority = 5 if message.severity == "critical" else 3
            tags = ["rotating_light"] if message.severity == "critical" else ["warning"]
        elif message.kind == "resolved":
            priority, tags = 2, ["white_check_mark"]
        else:
            priority, tags = 1, ["eyes"]
        payload: dict[str, object] = {
            "topic": topic,
            "title": message.title,
            "message": message.body,
            "priority": priority,
            "tags": tags,
        }
        if self.public_url:
            payload["click"] = f"{self.public_url}/"
            if firing:
                payload["actions"] = [
                    {
                        "action": "http",
                        "label": "Acknowledge",
                        "url": f"{self.public_url}/ack",
                        "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps(
                            {"fingerprint": message.fingerprint, "by": "phone (ntfy)"}
                        ),
                        "clear": True,
                    },
                    {"action": "view", "label": "Open annunciator", "url": f"{self.public_url}/"},
                ]
        return payload


_DISCORD_COLOURS = {"critical": 0xB00020, "warning": 0xE6A100, "none": 0x808080}


class DiscordChannel:
    """A Discord webhook. Works from anywhere, only while the pit has internet."""

    def __init__(
        self,
        *,
        name: str = "discord",
        min_severity: str = "critical",
        repeat_s: float | None = None,
        webhook_url: str,
        public_url: str | None = None,
        opener: Opener = http_post,
    ) -> None:
        if not webhook_url:
            raise ValueError("a Discord channel needs a webhook URL")
        self.name = name
        self.min_severity = min_severity
        self.repeat_s = repeat_s
        self._webhook_url = webhook_url
        self.public_url = public_url.rstrip("/") if public_url else None
        self._opener = opener

    def deliver(self, message: Message) -> None:
        status = self._opener(
            _json_request(self._webhook_url, self.payload(message), {}), _TIMEOUT_S
        )
        # Discord's webhook address is a secret; never echo it in an error.
        _raise_for_status(self.name, status, "the Discord webhook")

    def payload(self, message: Message) -> dict[str, object]:
        colour = _DISCORD_COLOURS.get(message.severity, 0x808080)
        if message.kind == "resolved":
            colour = 0x2B7A3A
        embed: dict[str, object] = {
            "title": message.title,
            "description": message.body,
            "color": colour,
            "timestamp": message.at.isoformat(),
        }
        if self.public_url:
            embed["url"] = f"{self.public_url}/"
        return {"username": "openlaps", "embeds": [embed]}
