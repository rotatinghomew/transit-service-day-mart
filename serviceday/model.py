"""运营日事实集市的公共输入类型与错误。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


class FixtureError(ValueError):
    """输入资料自身有误（缺字段、格式不对、引用不存在）。"""


class TimeAmbiguityError(FixtureError):
    """本地时间重复或不存在，且资料不足以判定唯一时刻。"""


@dataclass(frozen=True)
class ServiceCalendar:
    operator: str
    timezone: str
    service_day_starts: str  # "HH:MM"，运营日在该本地时刻起算


@dataclass(frozen=True)
class StopCode:
    operator: str
    code: str
    actual_place: str
    valid_from: date | None = None   # 含当日
    valid_to: date | None = None     # 含当日

    def valid_on(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True


@dataclass(frozen=True)
class Call:
    """计划中的一次停靠，时刻为本地墙钟 "HH:MM"。

    ``fold`` 用于本地时间重复（夏令时回拨）时指定取第一次还是第二次出现；
    资料未声明时保持 None，由解析器在遇到重复时隔离该服务。
    """

    stop: str
    arrival: str | None
    departure: str | None
    next_day: bool = False
    fold: int | None = None


@dataclass(frozen=True)
class Trip:
    id: str
    operator: str
    service_date: date
    calls: tuple[Call, ...]


@dataclass(frozen=True)
class Instruction:
    """对某次服务（可细化到某个停靠）的临时指令。

    ``effective`` 为单个适用日期，或 ``(from, to)`` 明确时刻范围（两端含）。
    ``action`` 取值：``cancel_service``、``add_service``、``cancel_call``、
    ``replace_stop``、``retime``、``add_call``。

    ``confirmed`` 表示替代停靠/改线是否已由运营方确认；未确认的改线仍可给
    站内人员看，但不得作为可售连接。
    """

    id: str
    trip: str
    action: str
    revision: int = 1
    effective: date | tuple[datetime, datetime] | None = None
    stop: str | None = None
    new_stop: str | None = None
    arrival: str | None = None
    departure: str | None = None
    received_at: datetime | None = None
    supersedes: tuple[str, ...] = ()
    confirmed: bool = True
    note: str = ""
    # add_service 专用
    operator: str | None = None
    service_date: date | None = None
    calls: tuple[Call, ...] = ()
    # 原始指令文本（若上游只给自然语言指令）
    raw_change: str = ""


@dataclass(frozen=True)
class WalkPath:
    from_place: str
    to_place: str
    walk_minutes: int
    valid_from: date | None = None            # 含当日
    valid_to: date | None = None              # 含当日
    accessible: bool = True
    # 每日实际可通行时段，如 (("06:00","12:00"),)；空表示全日通行。
    open_windows: tuple[tuple[str, str], ...] = ()
    # 可通行时段的日期范围（施工只开半天时通常与 valid_* 相同）。
    window_from: date | None = None
    window_to: date | None = None
    # 末段通行结束后仍可在通道内等待的分钟数。
    wait_after_close_minutes: int = 0
    # 通道开放时段是否已由站方确认；未确认的连接只能待定。
    confirmed: bool = True
    path_id: str = ""

    def valid_on(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True

    def windows_available_on(self, day: date) -> bool:
        if self.window_from is not None and day < self.window_from:
            return False
        if self.window_to is not None and day > self.window_to:
            return False
        return True


@dataclass(frozen=True)
class CandidateConnection:
    """上游交来的连接候选，状态只作参考，最终由引擎重新判定。"""

    from_trip: str
    to_trip: str
    transfer_minutes: int
    state: str = "available"
    reason: str = ""
    from_stop: str | None = None
    to_stop: str | None = None
    requires_accessible: bool = False
    # 形如 {"from": true, "to": false, "path": true, "stop": true}
    confirmed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Facts:
    calendars: dict[str, ServiceCalendar]
    stops: dict[tuple[str, str], StopCode]
    trips: dict[str, Trip]
    instructions: tuple[Instruction, ...]
    paths: tuple[WalkPath, ...]
    connections: tuple[CandidateConnection, ...]
