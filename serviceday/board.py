"""单个运营日的交割结果及其版本化输出。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class StopFact:
    trip_id: str
    operator: str
    official_stop_code: str        # 当日实际停靠的站码
    ticket_stop_code: str | None   # 票面站码（改线前）；增停时为 None
    actual_place: str
    arrival: datetime
    departure: datetime | None
    service_day: date
    provisional: bool = False
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "trip_id": self.trip_id,
            "operator": self.operator,
            "official_stop_code": self.official_stop_code,
            "ticket_stop_code": self.ticket_stop_code,
            "actual_place": self.actual_place,
            "arrival": self.arrival.isoformat(),
            "departure": self.departure.isoformat() if self.departure else None,
            "service_day": self.service_day.isoformat(),
            "provisional": self.provisional,
            "note": self.note,
        }


@dataclass(frozen=True)
class ConnectionFact:
    from_trip: str
    to_trip: str
    state: str  # available / pending / unavailable / isolated
    reason: str
    from_stop_code: str
    to_stop_code: str
    from_place: str
    to_place: str
    transfer_minutes: int | None
    path_id: str | None
    confirmed_from: bool
    confirmed_to: bool
    from_operator: str = ""
    to_operator: str = ""
    required_minutes: int | None = None
    walk_minutes: int | None = None
    requires_accessible: bool = False

    @property
    def sellable(self) -> bool:
        return self.state == "available"

    def as_json(self) -> dict[str, Any]:
        return {
            "from_trip": self.from_trip,
            "to_trip": self.to_trip,
            "from_operator": self.from_operator,
            "to_operator": self.to_operator,
            "state": self.state,
            "sellable": self.sellable,
            "reason": self.reason,
            "from_stop_code": self.from_stop_code,
            "to_stop_code": self.to_stop_code,
            "from_place": self.from_place,
            "to_place": self.to_place,
            "transfer_minutes": self.transfer_minutes,
            "required_minutes": self.required_minutes,
            "walk_minutes": self.walk_minutes,
            "path_id": self.path_id,
            "confirmed": {"from": self.confirmed_from, "to": self.confirmed_to},
            "requires_accessible": self.requires_accessible,
        }


@dataclass
class DayBoard:
    """某个运营日、某个运营方的实际可用停靠与连接。"""

    service_day: date
    operator: str
    version: str = ""
    stops: list[StopFact] = field(default_factory=list)
    connections: list[ConnectionFact] = field(default_factory=list)
    isolated: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "service_day": self.service_day.isoformat(),
            "operator": self.operator,
            "version": self.version,
            "stops": [s.as_json() for s in self.stops],
            "connections": [c.as_json() for c in self.connections],
            "isolated": self.isolated,
            "evidence": self.evidence,
        }

    def digest(self) -> str:
        payload = self.as_json()
        payload.pop("version", None)
        return stable_digest(payload)


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
