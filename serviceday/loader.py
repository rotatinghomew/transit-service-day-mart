"""加载并校验 ``facts.json`` 之类的原始资料。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .model import (
    Call,
    CandidateConnection,
    Facts,
    FixtureError,
    Instruction,
    ServiceCalendar,
    StopCode,
    Trip,
    WalkPath,
)


def _date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FixtureError(f"{field} 不是合法日期: {value!r}") from exc


def _datetime(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise FixtureError(f"{field} 不是合法时刻: {value!r}") from exc


def _call(raw: dict[str, Any]) -> Call:
    if not isinstance(raw, dict) or "stop" not in raw:
        raise FixtureError("停靠缺少 stop")
    return Call(
        stop=raw["stop"],
        arrival=raw.get("arrival"),
        departure=raw.get("departure"),
        next_day=bool(raw.get("next_day", False)),
    )


def _parse_change_text(
    change: str, stop: str | None, new_stop: str | None
) -> tuple[str, str | None, str | None]:
    """兼容上游只给自然语言指令的情况。

    识别形如 ``close OLD-17; replace with NEW-17``、``cancel``、``add stop X``
    的简单文本。返回 (action, stop, new_stop)；无法识别时抛错，交由上游改用
    结构化 ``action``。
    """
    closed = stop
    replacement = new_stop
    parts = [p.strip() for p in change.split(";") if p.strip()] if ";" in change else [change.strip()]
    for part in parts:
        lowered = part.lower()
        if lowered.startswith("close "):
            closed = part[6:].strip()
        elif lowered.startswith("replace with "):
            replacement = part[13:].strip()
        elif lowered.startswith("add stop "):
            return "add_call", closed, part[9:].strip()
        elif "cancel" in lowered and ("service" in lowered or "trip" in lowered):
            return "cancel_service", closed, replacement
        elif "cancel" in lowered:
            return "cancel_call", closed, replacement
        else:
            raise FixtureError(f"无法解析临时指令文本: {change!r}")
    if closed and replacement:
        return "replace_stop", closed, replacement
    if closed:
        return "cancel_call", closed, replacement
    raise FixtureError(f"无法解析临时指令文本: {change!r}")


def _instruction(raw: dict[str, Any]) -> Instruction:
    for required in ("id", "trip"):
        if required not in raw:
            raise FixtureError(f"指令缺少字段 {required}")
    instruction_id = raw["id"]
    trip = raw["trip"]

    stop = raw.get("stop")
    new_stop = raw.get("new_stop")
    action = raw.get("action")
    if action is None:
        change = raw.get("change")
        if change is None:
            raise FixtureError(f"指令 {instruction_id} 缺少 action 或 change")
        action, stop, new_stop = _parse_change_text(str(change), stop, new_stop)

    effective: date | tuple[datetime, datetime] | None = None
    raw_effective = raw.get("effective")
    if isinstance(raw_effective, dict):
        start = _datetime(raw_effective.get("from"), "effective.from")
        end = _datetime(raw_effective.get("to"), "effective.to")
        if start is None or end is None:
            raise FixtureError(f"指令 {instruction_id} 的时刻范围不完整")
        if end < start:
            raise FixtureError(f"指令 {instruction_id} 的时刻范围倒置")
        effective = (start, end)
    elif raw_effective is not None:
        effective = _date(raw_effective, "effective")

    if action not in {
        "cancel_service",
        "add_service",
        "cancel_call",
        "replace_stop",
        "retime",
        "add_call",
    }:
        raise FixtureError(f"指令 {instruction_id} 的 action 不支持: {action}")
    supersedes = raw.get("supersedes") or ()
    if isinstance(supersedes, str):
        supersedes = (supersedes,)
    return Instruction(
        id=instruction_id,
        trip=trip,
        action=action,
        revision=int(raw.get("revision", 1)),
        effective=effective,
        stop=stop,
        new_stop=new_stop,
        arrival=raw.get("arrival"),
        departure=raw.get("departure"),
        received_at=_datetime(raw.get("received_at"), "received_at"),
        supersedes=tuple(supersedes),
        confirmed=bool(raw.get("confirmed", True)),
        note=raw.get("note", ""),
        operator=raw.get("operator"),
        service_date=_date(raw.get("service_date"), "service_date"),
        calls=tuple(_call(c) for c in raw.get("calls", ())),
        raw_change=str(raw.get("change", "")),
    )


def _path(raw: dict[str, Any]) -> WalkPath:
    try:
        from_place = raw["from"]
        to_place = raw["to"]
        walk_minutes = int(raw["walk_minutes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FixtureError("通道缺少 from/to/walk_minutes") from exc
    if walk_minutes < 0:
        raise FixtureError("通道步行分钟数不能为负")
    windows = raw.get("open_windows") or ()
    parsed_windows: list[tuple[str, str]] = []
    for window in windows:
        if isinstance(window, (list, tuple)) and len(window) == 2:
            parsed_windows.append((str(window[0]), str(window[1])))
        elif isinstance(window, dict):
            parsed_windows.append((str(window["from"]), str(window["to"])))
        else:
            raise FixtureError(f"通道开放时段格式不对: {window!r}")
    return WalkPath(
        from_place=from_place,
        to_place=to_place,
        walk_minutes=walk_minutes,
        valid_from=_date(raw.get("valid_from"), "valid_from"),
        valid_to=_date(raw.get("valid_to"), "valid_to"),
        accessible=bool(raw.get("accessible", True)),
        open_windows=tuple(parsed_windows),
        window_from=_date(raw.get("window_from"), "window_from"),
        window_to=_date(raw.get("window_to"), "window_to"),
        wait_after_close_minutes=int(raw.get("wait_after_close_minutes", 0)),
        confirmed=bool(raw.get("confirmed", True)),
        path_id=str(raw.get("id", "")),
    )


def load_facts(path: str | Path) -> Facts:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_facts(raw)


def parse_facts(raw: dict[str, Any]) -> Facts:
    if not isinstance(raw, dict):
        raise FixtureError("资料根节点应为对象")
    calendars: dict[str, ServiceCalendar] = {}
    for item in raw.get("service_calendars", ()):
        operator = item.get("operator")
        if not operator:
            raise FixtureError("运营日资料缺少 operator")
        calendars[operator] = ServiceCalendar(
            operator=operator,
            timezone=item["timezone"],
            service_day_starts=item["service_day_starts"],
        )

    stops: dict[tuple[str, str], StopCode] = {}
    for item in raw.get("stops", ()):
        stop = StopCode(
            operator=item["operator"],
            code=item["code"],
            actual_place=item["actual_place"],
            valid_from=_date(item.get("valid_from"), "valid_from"),
            valid_to=_date(item.get("valid_to"), "valid_to"),
        )
        stops[(stop.operator, stop.code)] = stop

    trips: dict[str, Trip] = {}
    for item in raw.get("trips", ()):
        trip_id = item["id"]
        if trip_id in trips:
            raise FixtureError(f"服务 {trip_id} 重复")
        trip = Trip(
            id=trip_id,
            operator=item["operator"],
            service_date=_date(item["service_date"], "service_date"),
            calls=tuple(_call(c) for c in item.get("calls", ())),
        )
        trips[trip_id] = trip

    instructions = tuple(_instruction(i) for i in raw.get("instructions", ()))
    added_ids = {
        i.trip for i in instructions if i.action == "add_service"
    }
    seen_instruction: set[str] = set()
    for instruction in instructions:
        if instruction.id in seen_instruction:
            raise FixtureError(f"指令 {instruction.id} 重复")
        seen_instruction.add(instruction.id)
        if instruction.trip not in trips and instruction.trip not in added_ids:
            raise FixtureError(
                f"指令 {instruction.id} 引用了不存在的服务 {instruction.trip}"
            )
        if instruction.action == "add_service" and not instruction.calls:
            raise FixtureError(f"增开指令 {instruction.id} 没有停靠")

    paths = tuple(_path(p) for p in raw.get("paths", ()))
    connections = tuple(
        CandidateConnection(
            from_trip=c["from_trip"],
            to_trip=c["to_trip"],
            transfer_minutes=int(c.get("transfer_minutes", 0)),
            state=c.get("state", "available"),
            reason=c.get("reason", ""),
            from_stop=c.get("from_stop"),
            to_stop=c.get("to_stop"),
            requires_accessible=bool(c.get("requires_accessible", False)),
            confirmed=c.get("confirmed", {}),
        )
        for c in raw.get("connections", ())
    )
    for connection in connections:
        if connection.from_trip not in trips or connection.to_trip not in trips:
            raise FixtureError(
                f"连接候选引用了不存在的服务 {connection.from_trip} -> {connection.to_trip}"
            )
    return Facts(
        calendars=calendars,
        stops=stops,
        trips=trips,
        instructions=instructions,
        paths=paths,
        connections=connections,
    )
