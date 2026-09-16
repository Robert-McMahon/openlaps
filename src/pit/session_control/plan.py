"""The race plan: validation of what the operator types, with no I/O.

Everything the strategy service (P7.9) needs to know about the event that
the car cannot tell it: when the race ends, how much fuel the tank holds and
how much of it is usable, how long the regulated stops are, the driver-time
rules, and the stops the team intends to make. Validated here so the HTTP
layer, the database and the tests all agree on one shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

STOP_TYPES = ("refuel", "service")
DRIVER_LIMIT_KEYS = ("max_continuous_min", "max_total_min", "min_rest_min")
_MAX_STOPS = 50
_MAX_TEXT = 80


class PlanError(ValueError):
    """The submitted plan is not one the team could race to."""


@dataclass(frozen=True, slots=True)
class PlannedStop:
    type: Literal["refuel", "service"]
    at_lap: int | None = None
    at_ms: int | None = None
    driver_in: str | None = None


@dataclass(frozen=True, slots=True)
class RacePlan:
    race_end_at_ms: int | None
    race_end_laps: int | None
    end_authority: Literal["time", "laps"]
    tank_l: float
    usable_fuel_l: float
    refuel_min_s: int
    service_typical_s: int
    driver_limits: dict[str, int] = field(default_factory=dict)
    planned_stops: list[PlannedStop] = field(default_factory=list)
    car_number: str | None = None
    updated_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["planned_stops"] = [
            {k: v for k, v in stop.items() if v is not None} for stop in data["planned_stops"]
        ]
        return data


def _number(body: Mapping[str, object], key: str, *, required: bool = True) -> float | None:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise PlanError(f"{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlanError(f"{key} must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise PlanError(f"{key} must be finite")
    return float(value)


def _integer(body: Mapping[str, object], key: str, *, required: bool = True) -> int | None:
    value = _number(body, key, required=required)
    if value is None:
        return None
    if not float(value).is_integer():
        raise PlanError(f"{key} must be a whole number")
    return int(value)


def _text(value: object, key: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PlanError(f"{key} must be text")
    return value.strip()[:_MAX_TEXT] or None


def validate_plan(body: Mapping[str, object]) -> RacePlan:
    """Turn an operator's submission into a RacePlan, or say what is wrong."""
    race_end_at_ms = _integer(body, "race_end_at_ms", required=False)
    race_end_laps = _integer(body, "race_end_laps", required=False)
    if race_end_at_ms is None and race_end_laps is None:
        raise PlanError("the race needs an end: race_end_at_ms, race_end_laps, or both")
    if race_end_laps is not None and race_end_laps <= 0:
        raise PlanError("race_end_laps must be positive")
    authority = body.get("end_authority")
    if authority is None or authority == "":
        authority = "time" if race_end_at_ms is not None else "laps"
    if authority not in ("time", "laps"):
        raise PlanError("end_authority must be time or laps")
    if authority == "time" and race_end_at_ms is None:
        raise PlanError("end_authority is time but race_end_at_ms is missing")
    if authority == "laps" and race_end_laps is None:
        raise PlanError("end_authority is laps but race_end_laps is missing")

    tank_l = _number(body, "tank_l")
    usable_fuel_l = _number(body, "usable_fuel_l")
    assert tank_l is not None and usable_fuel_l is not None
    if tank_l <= 0:
        raise PlanError("tank_l must be positive")
    if usable_fuel_l <= 0:
        raise PlanError("usable_fuel_l must be positive")
    if usable_fuel_l > tank_l:
        raise PlanError("usable_fuel_l cannot exceed tank_l")

    refuel_min_s = _integer(body, "refuel_min_s")
    service_typical_s = _integer(body, "service_typical_s")
    assert refuel_min_s is not None and service_typical_s is not None
    if refuel_min_s < 0 or service_typical_s < 0:
        raise PlanError("stop durations cannot be negative")

    limits_raw = body.get("driver_limits", {})
    if limits_raw is None:
        limits_raw = {}
    if not isinstance(limits_raw, Mapping):
        raise PlanError("driver_limits must be an object")
    driver_limits: dict[str, int] = {}
    for key, value in limits_raw.items():
        if key not in DRIVER_LIMIT_KEYS:
            raise PlanError(f"driver_limits.{key} is not a known limit")
        if value is None or value == "":
            continue
        minutes = _integer({key: value}, key)
        assert minutes is not None
        if minutes < 0:
            raise PlanError(f"driver_limits.{key} cannot be negative")
        driver_limits[key] = minutes

    stops_raw = body.get("planned_stops", [])
    if stops_raw is None:
        stops_raw = []
    if not isinstance(stops_raw, list):
        raise PlanError("planned_stops must be a list")
    if len(stops_raw) > _MAX_STOPS:
        raise PlanError(f"planned_stops: at most {_MAX_STOPS} stops")
    stops: list[PlannedStop] = []
    for index, raw in enumerate(stops_raw):
        if not isinstance(raw, Mapping):
            raise PlanError(f"planned_stops[{index}] must be an object")
        stop_type = raw.get("type")
        if stop_type not in STOP_TYPES:
            raise PlanError(f"planned_stops[{index}].type must be refuel or service")
        at_lap = _integer(raw, "at_lap", required=False)
        at_ms = _integer(raw, "at_ms", required=False)
        if (at_lap is None) == (at_ms is None):
            raise PlanError(f"planned_stops[{index}] needs exactly one of at_lap / at_ms")
        if at_lap is not None and at_lap <= 0:
            raise PlanError(f"planned_stops[{index}].at_lap must be positive")
        stops.append(
            PlannedStop(
                type=stop_type,
                at_lap=at_lap,
                at_ms=at_ms,
                driver_in=_text(raw.get("driver_in"), "driver_in"),
            )
        )

    return RacePlan(
        race_end_at_ms=race_end_at_ms,
        race_end_laps=race_end_laps,
        end_authority=authority,
        tank_l=tank_l,
        usable_fuel_l=usable_fuel_l,
        refuel_min_s=refuel_min_s,
        service_typical_s=service_typical_s,
        driver_limits=driver_limits,
        planned_stops=stops,
        car_number=_text(body.get("car_number"), "car_number"),
        updated_by=_text(body.get("updated_by"), "updated_by"),
    )
