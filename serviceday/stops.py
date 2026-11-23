"""票面站码与实际停靠点的解释。

* 车票上的站码先按票面日期在 ``stops`` 的有效期内解释成 ``actual_place``。
  旧码在旧适用期内仍能解释票面，即使临时改线已经停用该码。
* 实际停靠点取该站码在资料中登记的物理地点；一旦临时改线，用改线后的站码。
* 若站码在资料中不存在，该服务进入隔离区。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .model import FixtureError, StopCode


class StopResolutionError(FixtureError):
    """站码无法解释，或解释出矛盾地点。"""


@dataclass(frozen=True)
class StopMeaning:
    operator: str
    code: str
    actual_place: str
    valid_from: date | None
    valid_to: date | None


def explain_ticket_stop(
    stops: dict[tuple[str, str], StopCode],
    operator: str,
    code: str,
    travel_date: date,
) -> StopMeaning:
    """按票面日期解释站码；超出适用期则拒绝解释。"""
    stop = stops.get((operator, code))
    if stop is None:
        raise StopResolutionError(f"运营商 {operator} 没有站码 {code}")
    if not stop.valid_on(travel_date):
        raise StopResolutionError(
            f"站码 {code} 在 {travel_date.isoformat()} 已不在适用期，票面无法解释"
        )
    return StopMeaning(stop.operator, stop.code, stop.actual_place, stop.valid_from, stop.valid_to)


def resolve_actual_place(
    stops: dict[tuple[str, str], StopCode],
    operator: str,
    code: str,
) -> str:
    """实际停靠使用站码登记的物理地点，与票面有效期无关。"""
    stop = stops.get((operator, code))
    if stop is None:
        raise StopResolutionError(f"运营商 {operator} 没有站码 {code}，实际停靠点无法确定")
    return stop.actual_place
