"""Alert parsing and the acknowledgement book: the notifier's state, with no I/O.

Everything here is pure so it can be tested without a server, a database or
a clock that moves on its own. The book holds what is firing, who has
acknowledged what, and when the heartbeat last arrived; the service around
it turns those into deliveries and a page.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SEVERITIES = ("none", "warning", "critical")
# Grafana batches alerts by policy group; a notification can carry many.
# Nothing here is trusted, so every dimension of the payload is bounded.
MAX_ALERTS_PER_NOTIFICATION = 50
MAX_TEXT_CHARS = 240
MAX_LABELS = 32
TEST_ALERT_NAME = "notifier-test"


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else SEVERITIES.index("critical")


@dataclass(frozen=True, slots=True)
class Alert:
    fingerprint: str
    name: str
    status: str
    severity: str
    started_at: datetime
    labels: dict[str, str]
    annotations: dict[str, str]
    rule_uid: str | None
    summary: str


def _text(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str | int | float):
        return str(value)[:MAX_TEXT_CHARS]
    return ""


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        if len(out) >= MAX_LABELS:
            break
        if isinstance(key, str) and key:
            out[key[:MAX_TEXT_CHARS]] = _text(item)
    return out


def _parse_time(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Grafana sends 0001-01-01 for "not ended"; anything before this
    # project existed is not a real timestamp.
    return parsed if parsed.year >= 2000 else fallback


def parse_notification(payload: Any, now: datetime) -> list[Alert]:
    """Grafana's webhook shape -> bounded, typed alerts. Junk yields nothing."""
    if not isinstance(payload, dict):
        return []
    raw_alerts = payload.get("alerts")
    if not isinstance(raw_alerts, list):
        return []
    alerts: list[Alert] = []
    for raw in raw_alerts[:MAX_ALERTS_PER_NOTIFICATION]:
        if not isinstance(raw, dict):
            continue
        labels = _string_map(raw.get("labels"))
        annotations = _string_map(raw.get("annotations"))
        name = labels.get("alertname") or "unnamed"
        status = raw.get("status")
        if status not in ("firing", "resolved"):
            continue
        fingerprint = _text(raw.get("fingerprint"))
        if not fingerprint:
            digest = hashlib.sha1(json.dumps(labels, sort_keys=True).encode()).hexdigest()
            fingerprint = digest[:16]
        severity = labels.get("severity", "")
        if severity not in SEVERITIES:
            # A rule that forgot to say is treated as the loud kind; a quiet
            # default is how an alert gets lost.
            severity = "critical"
        alerts.append(
            Alert(
                fingerprint=fingerprint,
                name=name,
                status=status,
                severity=severity,
                started_at=_parse_time(raw.get("startsAt"), now),
                labels=labels,
                annotations=annotations,
                rule_uid=labels.get("__alert_rule_uid__") or None,
                summary=annotations.get("summary") or name,
            )
        )
    return alerts


@dataclass(slots=True)
class ActiveAlert:
    alert: Alert
    first_seen: datetime
    last_seen: datetime
    acked_at: datetime | None = None
    acked_by: str | None = None
    note: str | None = None
    # Per delivery channel: when it was last told, for the repeat policy.
    last_notified: dict[str, datetime] = field(default_factory=dict)

    @property
    def acknowledged(self) -> bool:
        return self.acked_at is not None

    def as_dict(self, now: datetime) -> dict[str, object]:
        return {
            "fingerprint": self.alert.fingerprint,
            "name": self.alert.name,
            "summary": self.alert.summary,
            "severity": self.alert.severity,
            "rule_uid": self.alert.rule_uid,
            "started_at": self.alert.started_at.isoformat(),
            "age_s": round(max(0.0, (now - self.alert.started_at).total_seconds()), 1),
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "acked_by": self.acked_by,
            "note": self.note,
            "runbook_url": self.alert.annotations.get("runbook_url"),
        }


@dataclass(frozen=True, slots=True)
class Event:
    """Something the notifier tells channels, the ledger and the page about."""

    kind: str  # firing | resolved | repeat | ack | test
    alert: Alert
    at: datetime
    by: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "fingerprint": self.alert.fingerprint,
            "name": self.alert.name,
            "summary": self.alert.summary,
            "severity": self.alert.severity,
            "rule_uid": self.alert.rule_uid,
            "by": self.by,
            "note": self.note,
        }


class AlertBook:
    """What is firing, what has been acknowledged, and whether the path is alive."""

    def __init__(
        self,
        *,
        heartbeat_rule: str,
        heartbeat_expected_s: float,
        heartbeat_missed_intervals: int = 3,
        history_keep_s: float = 3600.0,
        max_history: int = 500,
    ) -> None:
        self.heartbeat_rule = heartbeat_rule
        self.heartbeat_expected_s = heartbeat_expected_s
        self.heartbeat_missed_intervals = heartbeat_missed_intervals
        self.history_keep_s = history_keep_s
        self.active: dict[str, ActiveAlert] = {}
        self.history: deque[Event] = deque(maxlen=max_history)
        self.heartbeat_seen: datetime | None = None
        self.heartbeats = 0
        self.notifications = 0

    def _is_heartbeat(self, alert: Alert) -> bool:
        return alert.rule_uid == self.heartbeat_rule or alert.name == self.heartbeat_rule

    def receive(self, alerts: list[Alert], now: datetime) -> list[Event]:
        """Fold a notification in; return the events it caused, in order."""
        self.notifications += 1
        events: list[Event] = []
        for alert in alerts:
            if self._is_heartbeat(alert):
                if alert.status == "firing":
                    self.heartbeat_seen = now
                    self.heartbeats += 1
                continue
            current = self.active.get(alert.fingerprint)
            if alert.status == "firing":
                if current is None:
                    self.active[alert.fingerprint] = ActiveAlert(alert, now, now)
                    events.append(Event("firing", alert, now))
                else:
                    current.last_seen = now
            elif current is not None:
                del self.active[alert.fingerprint]
                events.append(Event("resolved", alert, now))
        self._remember(events)
        return events

    def ack(self, fingerprint: str, by: str, note: str | None, now: datetime) -> Event | None:
        current = self.active.get(fingerprint)
        if current is None:
            return None
        current.acked_at, current.acked_by, current.note = now, by, note
        event = Event("ack", current.alert, now, by=by, note=note)
        self._remember([event])
        if current.alert.name == TEST_ALERT_NAME:
            # A test alert has no resolver but the person who raised it.
            del self.active[fingerprint]
            self._remember([Event("resolved", current.alert, now)])
        return event

    def raise_test(self, severity: str, now: datetime) -> list[Event]:
        """A synthetic alert through the whole chain, for the delivery drill."""
        fingerprint = f"test-{now.strftime('%H%M%S')}"
        alert = Alert(
            fingerprint=fingerprint,
            name=TEST_ALERT_NAME,
            status="firing",
            severity=severity,
            started_at=now,
            labels={"alertname": TEST_ALERT_NAME, "severity": severity},
            annotations={"summary": f"Test alert ({severity}) — acknowledge to clear"},
            rule_uid=None,
            summary=f"Test alert ({severity}) — acknowledge to clear",
        )
        return self.receive([alert], now)

    def _remember(self, events: list[Event]) -> None:
        for event in events:
            self.history.append(event)

    def unacknowledged(self) -> list[ActiveAlert]:
        return [entry for entry in self.active.values() if not entry.acknowledged]

    def heartbeat_age_s(self, now: datetime) -> float | None:
        if self.heartbeat_seen is None:
            return None
        return max(0.0, (now - self.heartbeat_seen).total_seconds())

    def heartbeat_ok(self, now: datetime) -> bool | None:
        """True while the path is proven alive; False once quiet; None until first seen."""
        age = self.heartbeat_age_s(now)
        if age is None:
            return None
        return age <= self.heartbeat_expected_s * self.heartbeat_missed_intervals

    def snapshot(self, now: datetime) -> dict[str, object]:
        cutoff = now.timestamp() - self.history_keep_s
        ordered = sorted(
            self.active.values(),
            key=lambda entry: (-severity_rank(entry.alert.severity), entry.alert.started_at),
        )
        return {
            "now": now.isoformat(),
            "active": [entry.as_dict(now) for entry in ordered],
            "unacknowledged_critical": sum(
                1 for e in ordered if not e.acknowledged and e.alert.severity == "critical"
            ),
            "history": [
                event.as_dict() for event in self.history if event.at.timestamp() >= cutoff
            ][-100:],
            "heartbeat": {
                "rule": self.heartbeat_rule,
                "seen_at": self.heartbeat_seen.isoformat() if self.heartbeat_seen else None,
                "age_s": self.heartbeat_age_s(now),
                "expected_s": self.heartbeat_expected_s,
                "ok": self.heartbeat_ok(now),
            },
        }
