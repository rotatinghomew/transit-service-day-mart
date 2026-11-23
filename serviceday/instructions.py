"""临调指令的应用与替代链解析。

* 指令定位到“哪一次服务和哪一处停靠”：由 ``trip`` + ``stop`` 决定；整趟指令
  （``cancel_service`` / ``add_service``）由 ``trip`` 单独决定。
* 指令的适用范围由 ``effective`` 给出：单个适用日期，或带时区的明确时刻范围。
* 同一作用域内多条指令按 (到达时刻, 修订号, id) 排序，最后一条生效。
* 显式 ``supersedes`` 链使被取代的旧指令完全不进入取值；即使旧指令先到、已经
  生效，也不因迟到指令而被“复原”。
* 若两条同作用域指令在时刻与修订上无法分出先后，抛 ``InstructionConflict``，
  由上层隔离该服务。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .model import Call, FixtureError, Instruction, ServiceCalendar, Trip
from .service_day import calendar_start


class InstructionConflict(FixtureError):
    """指令之间无法判定先后，或指令时刻本身有歧义。"""


@dataclass(frozen=True)
class AppliedChange:
    instruction_id: str
    action: str
    stop: str | None
    new_stop: str | None


@dataclass(frozen=True)
class EffectiveTrip:
    trip: Trip
    calendar: ServiceCalendar
    calls: tuple[Call, ...]
    applied: tuple[AppliedChange, ...] = ()
    cancelled: bool = False
    is_added: bool = False
    unconfirmed_change: bool = False
    # 与 calls 一一对应：该停靠来自原班次资料的哪个站码（票面站码）。
    # 增停或资料不足时为 None。
    origin_codes: tuple[str | None, ...] = ()


def _utc_naive(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        return moment.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return moment


def _order_key(instruction: Instruction) -> tuple:
    received = _utc_naive(instruction.received_at) if instruction.received_at else datetime.min
    return (received, instruction.revision, instruction.id)


def instruction_applies(
    instruction: Instruction,
    service_date: date,
    calendar: ServiceCalendar,
) -> bool:
    effective = instruction.effective
    if effective is None:
        return True
    if isinstance(effective, date):
        return effective == service_date
    start, end = effective
    if start.tzinfo is None or end.tzinfo is None:
        raise InstructionConflict(
            f"指令 {instruction.id} 的适用时刻范围缺少时区，无法跨运营方比较"
        )
    tz = ZoneInfo(calendar.timezone)
    window_open = datetime.combine(service_date, calendar_start(calendar), tzinfo=tz)
    window_close = window_open + timedelta(days=1)
    return start < window_close and end > window_open


def _group_key(instruction: Instruction) -> tuple:
    """同一作用域内的指令后到者生效。

    停靠级指令按“服务 + 目标停靠”归组；增停按新站码归组；整趟取消单独一组。
    这样同一停靠上的取消、替代、改时刻不会叠加出互相矛盾的结果。
    """
    if instruction.action == "cancel_service":
        return ("service", instruction.trip)
    if instruction.action == "add_call":
        return ("add", instruction.trip, instruction.new_stop)
    return ("stop", instruction.trip, instruction.stop)


def select_instructions(
    instructions: list[Instruction], service_date: date, calendar: ServiceCalendar
) -> list[Instruction]:
    """筛出适用于该运营日的指令，并解析取代链。"""
    applicable = [i for i in instructions if instruction_applies(i, service_date, calendar)]
    known = {i.id: i for i in applicable}
    superseded: set[str] = set()
    for instruction in applicable:
        for target in instruction.supersedes:
            if target in known:
                superseded.add(target)
            # 引用本批未提供的指令时无法核实，保守地视为不存在的旧版本。

    survivors = [i for i in applicable if i.id not in superseded]
    grouped: dict[tuple, Instruction] = {}
    for instruction in survivors:
        key = _group_key(instruction)
        current = grouped.get(key)
        if current is None:
            grouped[key] = instruction
            continue
        if instruction.id == current.id:
            continue
        # 只按 id 也总能排出先后，但那只是任意顺序；到达时刻与修订都相同、内容
        # 却不同，说明资料不足，必须隔离。
        if _order_key(instruction)[:2] == _order_key(current)[:2]:
            raise InstructionConflict(
                f"指令 {instruction.id} 与 {current.id} 作用相同且无法判定先后"
            )
        if _order_key(instruction) > _order_key(current):
            grouped[key] = instruction
    # 被取代的旧指令不参与取值；但同停靠上若有一条替代站指令和一条改时刻指令，
    # 它们属于同一作用域，只会留下最后一条 —— 这符合“一处停靠只有一个当前版本”。
    return sorted(grouped.values(), key=_order_key)


def _apply_one(
    trip: Trip, calls: list[tuple[Call, str | None]], instruction: Instruction
) -> list[tuple[Call, str | None]]:
    if instruction.action == "cancel_call":
        if instruction.stop is None:
            raise InstructionConflict(f"指令 {instruction.id} 未指明要取消的停靠")
        kept = [c for c in calls if c[0].stop != instruction.stop]
        if len(kept) == len(calls):
            raise InstructionConflict(
                f"指令 {instruction.id} 要取消的停靠 {instruction.stop} 不在服务 {trip.id} 中"
            )
        return kept
    if instruction.action == "replace_stop":
        if instruction.stop is None or instruction.new_stop is None:
            raise InstructionConflict(f"指令 {instruction.id} 缺少原停靠或替代停靠")
        matches = [
            (index, call, origin)
            for index, (call, origin) in enumerate(calls)
            if call.stop == instruction.stop
        ]
        if not matches:
            raise InstructionConflict(
                f"指令 {instruction.id} 要替代的停靠 {instruction.stop} 不在服务 {trip.id} 中"
            )
        if any(call.stop == instruction.new_stop for call, _ in calls):
            # 替代站已经是本次服务的另一处停靠：这条改线表示原停靠并入替代站，
            # 不能在两个时刻重复停同一地点。保留替代站，并把被并入的原票面站码
            # 挂到它上面，便于解释旧票。
            merged_origin = matches[0][2]
            return [
                (
                    call,
                    merged_origin
                    if call.stop == instruction.new_stop and origin is not None
                    else origin,
                )
                for call, origin in calls
                if call.stop != instruction.stop
            ]
        new_calls: list[tuple[Call, str | None]] = []
        for index, (call, origin) in enumerate(calls):
            if call.stop == instruction.stop:
                new_calls.append(
                    (
                        replace(
                            call,
                            stop=instruction.new_stop,
                            arrival=instruction.arrival or call.arrival,
                            departure=instruction.departure or call.departure,
                            next_day=call.next_day,
                        ),
                        origin,
                    )
                )
            else:
                new_calls.append((call, origin))
        return new_calls
    if instruction.action == "retime":
        if instruction.stop is None:
            raise InstructionConflict(f"指令 {instruction.id} 未指明要改时刻的停靠")
        found = False
        new_calls = []
        for call, origin in calls:
            if call.stop == instruction.stop:
                new_calls.append(
                    (
                        replace(
                            call,
                            arrival=instruction.arrival or call.arrival,
                            departure=instruction.departure or call.departure,
                        ),
                        origin,
                    )
                )
                found = True
            else:
                new_calls.append((call, origin))
        if not found:
            raise InstructionConflict(
                f"指令 {instruction.id} 要改时刻的停靠 {instruction.stop} 不在服务 {trip.id} 中"
            )
        return new_calls
    if instruction.action == "add_call":
        if instruction.new_stop is None:
            raise InstructionConflict(f"指令 {instruction.id} 未指明要增停的站点")
        return calls + [
            (
                Call(
                    stop=instruction.new_stop,
                    arrival=instruction.arrival,
                    departure=instruction.departure,
                ),
                None,
            )
        ]
    raise InstructionConflict(f"指令 {instruction.id} 的 action 无法应用到停靠: {instruction.action}")


def effective_trip(
    trip: Trip,
    instructions: list[Instruction],
    calendar: ServiceCalendar,
    service_date: date,
) -> EffectiveTrip:
    selected = select_instructions(
        [i for i in instructions if i.trip == trip.id], service_date, calendar
    )
    calls: list[tuple[Call, str | None]] = [(c, c.stop) for c in trip.calls]
    applied: list[AppliedChange] = []
    cancelled = False
    unconfirmed = False
    for instruction in selected:
        if instruction.action == "cancel_service":
            cancelled = True
            applied.append(
                AppliedChange(instruction.id, instruction.action, None, None)
            )
            continue
        if cancelled:
            continue
        calls = _apply_one(trip, calls, instruction)
        if not instruction.confirmed:
            unconfirmed = True
        applied.append(
            AppliedChange(
                instruction.id, instruction.action, instruction.stop, instruction.new_stop
            )
        )
    return EffectiveTrip(
        trip=trip,
        calendar=calendar,
        calls=tuple(c for c, _ in calls),
        applied=tuple(applied),
        cancelled=cancelled,
        unconfirmed_change=unconfirmed,
        origin_codes=tuple(o for _, o in calls),
    )


def added_trips(
    instructions: list[Instruction], service_date: date, calendar: ServiceCalendar
) -> list[EffectiveTrip]:
    """由 ``add_service`` 指令生成的增开服务。

    增开服务也可能再被同一服务标识上的后续指令（改停靠、取消等）修改；这些指令
    与基础指令走同一套 ``select_instructions`` 规则。
    """
    result: list[EffectiveTrip] = []
    base = [
        i
        for i in instructions
        if i.action == "add_service"
        and i.operator == calendar.operator
        and i.service_date == service_date
    ]
    superseded: set[str] = set()
    seen_ids: set[str] = set()
    for instruction in base:
        if instruction.trip in seen_ids:
            raise InstructionConflict(
                f"增开指令 {instruction.id} 与同服务 {instruction.trip} 的其他增开指令重复"
            )
        seen_ids.add(instruction.trip)
        for target in instruction.supersedes:
            superseded.add(target)
    for instruction in base:
        if instruction.id in superseded:
            continue
        trip = Trip(
            id=instruction.trip,
            operator=instruction.operator or calendar.operator,
            service_date=service_date,
            calls=instruction.calls,
        )
        # 增开之后针对该服务的修改（取消停靠、改时刻等）。
        following = [
            i
            for i in instructions
            if i.trip == trip.id
            and i.action != "add_service"
            and instruction.id not in i.supersedes
        ]
        selected = select_instructions(following, service_date, calendar)
        calls: list[tuple[Call, str | None]] = [(c, None) for c in trip.calls]
        applied: list[AppliedChange] = [
            AppliedChange(instruction.id, instruction.action, None, None)
        ]
        cancelled = False
        unconfirmed = not instruction.confirmed
        for later in selected:
            if later.action == "cancel_service":
                cancelled = True
                applied.append(
                    AppliedChange(later.id, later.action, None, None)
                )
                continue
            if cancelled:
                continue
            calls = _apply_one(trip, calls, later)
            if not later.confirmed:
                unconfirmed = True
            applied.append(
                AppliedChange(later.id, later.action, later.stop, later.new_stop)
            )
        result.append(
            EffectiveTrip(
                trip=trip,
                calendar=calendar,
                calls=tuple(c for c, _ in calls),
                applied=tuple(applied),
                cancelled=cancelled,
                is_added=True,
                unconfirmed_change=unconfirmed,
                origin_codes=tuple(o for _, o in calls),
            )
        )
    return result
