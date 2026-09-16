"""Hand-built Natsoft documents, from the documented types and nothing else.

Every element and attribute here is one the protocol description names
(``docs/plan/PHASE7.md`` locked decision 6). The values are invented: four
cars, a six-hour race, a safety car, a stop. ``write_fixture`` produces
``tests/fixtures/natsoft/hand-built.jsonl`` -- the replay fixture every
timing-feed test drives the schema with until a real capture replaces it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import quoteattr

FIXTURE = Path(__file__).parent / "fixtures" / "natsoft" / "hand-built.jsonl"
T0 = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)

# (competitor id, car number, class, drivers)
COMPETITORS = (
    ("1", "27", "A", {"1": "Driver A", "2": "Driver B"}),
    ("2", "14", "A", {"3": "Rival One"}),
    ("3", "7", "B", {"4": "Rival Two"}),
    ("4", "99", "B", {"5": "Rival Three"}),
)
OUR_CAR = "27"


def _attrs(**values: object) -> str:
    return " ".join(f"{key}={quoteattr(str(value))}" for key, value in values.items())


def _tod(at: datetime) -> int:
    return int(at.timestamp())


def event(at: datetime, seq: int = 1) -> str:
    return (
        f"<Event {_attrs(TOD=_tod(at), Seq=seq, Type='Race', Code='R01')} "
        f"{_attrs(Description1='6 Hour Regularity Relay', Laps=0, Seconds=21600)}/>"
    )


def track(at: datetime, seq: int = 2) -> str:
    return (
        f"<Track {_attrs(TOD=_tod(at), Seq=seq, Code='WANN', Name='Wanneroo', Length=2.42)}>"
        f"<PitEntry {_attrs(PassType=3, FromMain=2100)}/>"
        f"<PitExit {_attrs(PassType=4, FromMain=180)}/>"
        f"<PitMain {_attrs(PassType=2, FromMain=0)}/>"
        "</Track>"
    )


def competitor_list(at: datetime, seq: int = 3, competitors=COMPETITORS) -> str:
    body = []
    for competitor_id, number, car_class, drivers in competitors:
        driver_xml = "".join(
            f"<Driver {_attrs(ID=driver_id, Name=name, Code=name[:3].upper())}/>"
            for driver_id, name in drivers.items()
        )
        body.append(
            f"<Competitor {_attrs(ID=competitor_id, Number=number, Class=car_class)} "
            f"{_attrs(Category=0, Vehicle='Club racer')}>{driver_xml}</Competitor>"
        )
    return (
        f"<CompetitorList {_attrs(TOD=_tod(at), Seq=seq, Type='full')}>"
        + "".join(body)
        + "</CompetitorList>"
    )


def status(at: datetime, seq: int, state: str, sub: str = "") -> str:
    return (
        f"<Status {_attrs(TOD=_tod(at), Seq=seq, Status=state, SubStatus=sub)} "
        f"{_attrs(Time=_tod(at), Event='R01', PartNumber=1, SubPart=0)}/>"
    )


def heartbeat(at: datetime, seq: int, state: str, sub: str = "", track_temp: float = 31.0) -> str:
    return (
        f"<Heartbeat {_attrs(TOD=_tod(at), Seq=seq, Status=state, SubStatus=sub)} "
        f"{_attrs(TrackTemp=track_temp)}/>"
    )


def counters(
    at: datetime,
    seq: int,
    *,
    kind: str,
    count: float,
    elapsed: float,
    state: str = "Green",
    sub: str = "",
) -> str:
    return (
        f"<Counters {_attrs(TOD=_tod(at), Seq=seq, Type=kind, Count=count, Elapsed=elapsed)} "
        f"{_attrs(RedStopTime=0, Status=state, SubStatus=sub, TrackTemp=31)}/>"
    )


def passing(at: datetime, seq: int, competitor_id: str, kind: int = 1, tod=None) -> str:
    stamp = _tod(at) if tod is None else tod
    transmitter = 1000 + (int(competitor_id) if competitor_id.isdigit() else 0)
    return (
        f"<Passing {_attrs(TOD=_tod(at), Seq=seq, Status='Green', SubStatus='')} "
        f"{_attrs(ID=competitor_id, Transmitter=transmitter, Type=kind)} "
        f"{_attrs(Time=stamp, Sector=0, Active='Active')}/>"
    )


def position(
    line: int,
    competitor_id: str,
    *,
    pos: str | None = None,
    driver: str = "1",
    laps: int = 0,
    last: float = 0.0,
    best: float = 0.0,
    gap_lead: float = 0.0,
    gap_next: float = 0.0,
    sectors=(0.0, 0.0, 0.0),
    pit_stops: int = 0,
    pit_flag: str = "",
    out_lap: str = "N",
) -> str:
    pos = str(line) if pos is None else pos
    detail = _attrs(
        Driv="All",
        LastLap=laps,
        LastTime=last,
        FastLap=laps if best else 0,
        FastTime=best,
        GapLeadLap=0,
        GapLeadTime=gap_lead,
        GapNextLap=0,
        GapNextTime=gap_next,
        LastSec1Time=sectors[0],
        LastSec2Time=sectors[1],
        LastSec3Time=sectors[2],
        PitStops=pit_stops,
        PitLaneFlag=pit_flag,
        OutLap=out_lap,
    )
    return (
        f"<Position {_attrs(Line=line, LiveLine=line, Pos=pos, LivePos=pos)} "
        f"{_attrs(Comp=competitor_id, Driv=driver, TrackComp=competitor_id)}>"
        f"<Detail {detail}/></Position>"
    )


def leaderboard(at: datetime, seq: int, kind: str, positions: list[str]) -> str:
    return (
        f"<Leaderboard {_attrs(TOD=_tod(at), Seq=seq, Type=kind, Lines=len(positions))}>"
        + "".join(positions)
        + "</Leaderboard>"
    )


def new(at: datetime, seq: int, children: list[str]) -> str:
    return f"<New {_attrs(TOD=_tod(at), Seq=seq)}>" + "".join(children) + "</New>"


def full_grid(at: datetime, seq: int) -> str:
    return leaderboard(
        at,
        seq,
        "full",
        [
            position(1, "1"),
            position(2, "2", driver="3"),
            position(3, "3", driver="4"),
            position(4, "4", driver="5"),
        ],
    )


def race_documents() -> list[tuple[datetime, str]]:
    """The whole hand-built race, in arrival order."""
    t = T0
    lap = timedelta(seconds=95)
    docs: list[tuple[datetime, str]] = [
        (t, event(t)),
        (t, track(t)),
        (t, competitor_list(t)),
        (t, status(t, 4, "WaitStart")),
        (t, full_grid(t, 5)),
        (t, counters(t, 6, kind="Time", count=21600, elapsed=0, state="WaitStart")),
    ]
    t += timedelta(seconds=30)
    docs.append((t, status(t, 7, "Green")))
    docs.append((t, heartbeat(t, 8, "Green")))
    seq = 9
    # Lap 1: everyone crosses, car 27 leads.
    t += lap
    for comp, offset in (("1", 0), ("2", 1.4), ("3", 3.2), ("4", 6.0)):
        at = t + timedelta(seconds=offset)
        docs.append((at, passing(at, seq, comp)))
        seq += 1
    t += timedelta(seconds=7)
    docs.append(
        (
            t,
            leaderboard(
                t,
                seq,
                "part",
                [
                    position(1, "1", laps=1, last=95.0, best=95.0, sectors=(30.0, 32.0, 33.0)),
                    position(
                        2, "2", driver="3", laps=1, last=96.4, best=96.4, gap_lead=1.4, gap_next=1.4
                    ),
                    position(
                        3, "3", driver="4", laps=1, last=98.2, best=98.2, gap_lead=3.2, gap_next=1.8
                    ),
                    position(
                        4,
                        "4",
                        driver="5",
                        laps=1,
                        last=101.0,
                        best=101.0,
                        gap_lead=6.0,
                        gap_next=2.8,
                    ),
                ],
            ),
        )
    )
    seq += 1
    docs.append((t, counters(t, seq, kind="Time", count=21600 - 102, elapsed=102)))
    seq += 1
    # Lap 2: car 14 pits (a pit-main crossing rather than the main line).
    t += lap
    for comp, offset, kind in (("1", 0, 1), ("3", 3.5, 1), ("4", 6.2, 1), ("2", 9, 2)):
        at = t + timedelta(seconds=offset)
        docs.append((at, passing(at, seq, comp, kind=kind)))
        seq += 1
    t += timedelta(seconds=10)
    docs.append(
        (
            t,
            leaderboard(
                t,
                seq,
                "part",
                [
                    position(1, "1", laps=2, last=95.2, best=95.0),
                    position(
                        2, "3", driver="4", laps=2, last=95.3, best=95.3, gap_lead=3.5, gap_next=3.5
                    ),
                    position(
                        3, "4", driver="5", laps=2, last=95.2, best=95.2, gap_lead=6.2, gap_next=2.7
                    ),
                    position(
                        4,
                        "2",
                        driver="3",
                        laps=2,
                        last=103.6,
                        best=96.4,
                        gap_lead=9.0,
                        gap_next=2.8,
                        pit_stops=1,
                        pit_flag="P",
                    ),
                ],
            ),
        )
    )
    seq += 1
    # A safety car.
    t += timedelta(seconds=20)
    docs.append((t, status(t, seq, "Yellow", "SafetyCar")))
    seq += 1
    docs.append((t, heartbeat(t, seq, "Yellow", "SafetyCar", track_temp=30.5)))
    seq += 1
    # Lap 3 under the safety car: slow laps, arriving inside a New container.
    t += timedelta(seconds=150)
    children = [passing(t, seq, "1"), passing(t + timedelta(seconds=2), seq + 1, "3")]
    seq += 2
    children.append(
        leaderboard(
            t,
            seq,
            "part",
            [
                position(1, "1", laps=3, last=170.0, best=95.0),
                position(
                    2, "3", driver="4", laps=3, last=169.8, best=95.3, gap_lead=2.0, gap_next=2.0
                ),
            ],
        )
    )
    seq += 1
    children.append(
        counters(
            t, seq, kind="Time", count=21600 - 382, elapsed=382, state="Yellow", sub="SafetyCar"
        )
    )
    seq += 1
    docs.append((t, new(t, seq, children)))
    seq += 1
    # Green again; car 14 rejoins on an out-lap.
    t += timedelta(seconds=40)
    docs.append((t, status(t, seq, "Green")))
    seq += 1
    docs.append((t, heartbeat(t, seq, "Green")))
    seq += 1
    t += timedelta(seconds=60)
    docs.append(
        (
            t,
            leaderboard(
                t,
                seq,
                "part",
                [
                    position(
                        3,
                        "4",
                        driver="5",
                        laps=3,
                        last=168.0,
                        best=95.2,
                        gap_lead=8.0,
                        gap_next=6.0,
                    ),
                    position(
                        4,
                        "2",
                        driver="3",
                        laps=3,
                        last=210.0,
                        best=96.4,
                        gap_lead=40.0,
                        gap_next=32.0,
                        pit_stops=1,
                        pit_flag="",
                        out_lap="Y",
                    ),
                ],
            ),
        )
    )
    seq += 1
    # Lap 4, then the flag and a final full leaderboard.
    t += lap
    docs.append((t, passing(t, seq, "1")))
    seq += 1
    docs.append(
        (
            t,
            leaderboard(
                t,
                seq,
                "part",
                [position(1, "1", laps=4, last=94.8, best=94.8, sectors=(29.9, 31.8, 33.1))],
            ),
        )
    )
    seq += 1
    t += timedelta(seconds=5)
    docs.append((t, status(t, seq, "Checkered")))
    seq += 1
    docs.append(
        (
            t,
            leaderboard(
                t,
                seq,
                "full",
                [
                    position(1, "1", laps=4, last=94.8, best=94.8, sectors=(29.9, 31.8, 33.1)),
                    position(
                        2,
                        "3",
                        driver="4",
                        laps=3,
                        last=169.8,
                        best=95.3,
                        gap_lead=2.0,
                        gap_next=2.0,
                    ),
                    position(
                        3,
                        "4",
                        driver="5",
                        laps=3,
                        last=168.0,
                        best=95.2,
                        gap_lead=8.0,
                        gap_next=6.0,
                    ),
                    position(
                        4,
                        "2",
                        driver="3",
                        laps=3,
                        last=210.0,
                        best=96.4,
                        gap_lead=40.0,
                        gap_next=32.0,
                        pit_stops=1,
                        out_lap="N",
                    ),
                ],
            ),
        )
    )
    seq += 1
    t += timedelta(seconds=30)
    docs.append((t, status(t, seq, "Ended")))
    return docs


def write_fixture(path: Path = FIXTURE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for at, document in race_documents():
            handle.write(json.dumps({"t": at.timestamp(), "doc": document}, separators=(",", ":")))
            handle.write("\n")
    return path


if __name__ == "__main__":
    print(write_fixture())


def our_main_line_passings() -> list[datetime]:
    """When the timekeepers stamped our car crossing the main line, in the fixture."""
    stamps: list[datetime] = []
    for _at, document in race_documents():
        for chunk in document.split("<Passing ")[1:]:
            attributes = dict(re.findall(r'(\w+)="([^"]*)"', chunk.split("/>")[0]))
            if attributes.get("ID") == "1" and attributes.get("Type") == "1":
                stamps.append(datetime.fromtimestamp(int(attributes["Time"]), tz=UTC))
    return stamps
