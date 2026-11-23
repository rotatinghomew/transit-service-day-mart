"""按运营日组装事实：停靠、连接、隔离与交割版本。

一次 ``build_day`` 只处理一个运营日，输出：
* 该日实际可用的停靠（区分票面站码与实际停靠点）；
* 该日实际可售/待定的连接（同一对服务只给一个判定）；
* 因时刻歧义、站码缺失或指令冲突被隔离的服务；
* 每条事实对应的指令与通道依据。

输出按固定顺序排列并做内容摘要，同一输入重复构建得到相同 ``version``。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .board import ConnectionFact, DayBoard, StopFact
from .instructions import (
    EffectiveTrip,
    InstructionConflict,
    added_trips,
    effective_trip,
)
from .model import (
    CandidateConnection,
    Facts,
    FixtureError,
    TimeAmbiguityError,
    Trip,
)
from .paths import check_path, walk_minutes as path_walk_minutes
from .service_day import (
    call_times,
    parse_hhmm,
    resolve_local,
    service_day_of,
)
from .stops import StopResolutionError, explain_ticket_stop, resolve_actual_place


@dataclass
class IsolatedService:
    trip_id: str
    operator: str
    reason: str
    kind: str  # ambiguity / stops / instruction / calendar


@dataclass
class PlacedCall:
    trip_id: str
    operator: str
    call: Any
    arrival: Any
    departure: Any
    actual_place: str
    official_code: str
    ticket_code: str | None
    ticket_note: str
    provisional: bool


def _isolate(board: DayBoard, service: IsolatedService) -> None:
    entry = {
        "trip_id": service.trip_id,
        "operator": service.operator,
        "reason": service.reason,
        "kind": service.kind,
    }
    if entry not in board.isolated:
        board.isolated.append(entry)


def _effective_for_day(
    facts: Facts, service_day: date
) -> tuple[dict[str, EffectiveTrip], list[IsolatedService]]:
    """算出该运营日所有运营方的实际班次（含增开）。"""
    effective: dict[str, EffectiveTrip] = {}
    isolated: list[IsolatedService] = []
    for trip in facts.trips.values():
        calendar = facts.calendars.get(trip.operator)
        if calendar is None:
            isolated.append(
                IsolatedService(trip.id, trip.operator, "运营商没有运营日历", "calendar")
            )
            continue
        try:
            if not trip.calls:
                raise FixtureError(f"服务 {trip.id} 没有停靠")
            first = trip.calls[0]
            anchor = resolve_local(
                trip.service_date,
                parse_hhmm(first.arrival or first.departure),
                calendar,
                fold=first.fold,
            )
            trip_day = service_day_of(anchor, calendar)
        except (FixtureError, TimeAmbiguityError) as exc:
            # 时刻无法唯一解析：相关服务进入隔离区，不再产出任何停靠。
            if service_day == trip.service_date:
                isolated.append(
                    IsolatedService(trip.id, trip.operator, str(exc), "ambiguity")
                )
            continue
        if trip_day != service_day:
            continue
        try:
            result = effective_trip(
                trip, list(facts.instructions), calendar, service_day
            )
        except InstructionConflict as exc:
            isolated.append(
                IsolatedService(trip.id, trip.operator, str(exc), "instruction")
            )
            continue
        if result.cancelled:
            continue
        effective[trip.id] = result

    for operator in sorted(facts.calendars):
        calendar = facts.calendars[operator]
        try:
            for added in added_trips(list(facts.instructions), service_day, calendar):
                if added.cancelled:
                    continue
                effective.setdefault(added.trip.id, added)
        except InstructionConflict as exc:
            isolated.append(IsolatedService("*", operator, str(exc), "instruction"))
    return effective, isolated


def _unconfirmed_codes(
    result: EffectiveTrip, facts: Facts
) -> set[str]:
    by_id = {i.id: i for i in facts.instructions}
    codes: set[str] = set()
    for change in result.applied:
        instruction = by_id.get(change.instruction_id)
        if instruction is None or instruction.confirmed:
            continue
        code = change.new_stop or change.stop
        if code:
            codes.add(code)
    return codes


def _place_calls(
    facts: Facts,
    effective: dict[str, EffectiveTrip],
    service_day: date,
    board: DayBoard,
) -> dict[str, list[PlacedCall]]:
    from zoneinfo import ZoneInfo

    placed: dict[str, list[PlacedCall]] = {}
    for trip_id in sorted(effective):
        result = effective[trip_id]
        calendar = result.calendar
        effective_calls = Trip(
            id=result.trip.id,
            operator=result.trip.operator,
            service_date=result.trip.service_date,
            calls=result.calls,
        )
        try:
            times = call_times(effective_calls, calendar)
        except (FixtureError, TimeAmbiguityError) as exc:
            _isolate(
                board,
                IsolatedService(trip_id, result.trip.operator, str(exc), "ambiguity"),
            )
            continue
        tz = ZoneInfo(calendar.timezone)
        unconfirmed = _unconfirmed_codes(result, facts)
        rows: list[PlacedCall] = []
        failed = False
        for (call, arrival, departure), origin in zip(times, result.origin_codes):
            try:
                place = resolve_actual_place(
                    facts.stops, result.trip.operator, call.stop
                )
            except StopResolutionError as exc:
                _isolate(
                    board,
                    IsolatedService(
                        trip_id, result.trip.operator, str(exc), "stops"
                    ),
                )
                failed = True
                break
            local_day = arrival.astimezone(tz).date()
            ticket_code, note = _ticket_code(
                facts, result.trip.operator, origin, local_day, call.stop, place
            )
            if result.is_added and result.unconfirmed_change:
                # 增开服务本身尚未确认：整趟停靠都只能作待定。
                provisional = True
            else:
                # 只把真正被未确认指令动过的停靠标为待定；同车其他停靠照常发。
                provisional = call.stop in unconfirmed
            rows.append(
                PlacedCall(
                    trip_id=trip_id,
                    operator=result.trip.operator,
                    call=call,
                    arrival=arrival,
                    departure=departure,
                    actual_place=place,
                    official_code=call.stop,
                    ticket_code=ticket_code,
                    ticket_note=note,
                    provisional=provisional,
                )
            )
        if not failed:
            placed[trip_id] = rows
    return placed


def _ticket_code(
    facts: Facts,
    operator: str,
    origin: str | None,
    travel_day: date,
    actual_code: str,
    actual_place: str,
) -> tuple[str | None, str]:
    """票面站码始终保留原始站码；只对“能否按适用期解释”给说明。"""
    if origin is None:
        return None, "增停站点没有原票面站码"
    if origin == actual_code:
        # 未改线的停靠：票面站码就是计划站码，无需额外说明。
        return origin, ""
    try:
        meaning = explain_ticket_stop(facts.stops, operator, origin, travel_day)
    except StopResolutionError as exc:
        # 旧码已过适用期：票面仍印着它，但不能再解释成某个实际地点。
        return origin, f"{exc}；实际停靠为 {actual_code}（{actual_place}）"
    return origin, (
        f"票面 {origin} 在其适用期内解释为 {meaning.actual_place}，"
        f"实际停靠已改为 {actual_code}（{actual_place}）"
    )


def build_day(
    facts: Facts, service_day: date, operator: str | None = None
) -> DayBoard:
    """构建某运营日的交割内容。

    ``operator`` 只过滤对外发布的停靠与连接；运营日归属、跨运营商连接判定仍用
    各方自己的日历与站码。
    """
    board = DayBoard(service_day=service_day, operator=operator or "*")
    effective, isolated = _effective_for_day(facts, service_day)
    for service in isolated:
        _isolate(board, service)

    placed = _place_calls(facts, effective, service_day, board)
    isolated_ids = {i["trip_id"] for i in board.isolated}

    for trip_id in sorted(placed):
        for row in placed[trip_id]:
            if operator is not None and row.operator != operator:
                continue
            board.stops.append(
                StopFact(
                    trip_id=row.trip_id,
                    operator=row.operator,
                    official_stop_code=row.official_code,
                    ticket_stop_code=row.ticket_code,
                    actual_place=row.actual_place,
                    arrival=row.arrival,
                    departure=row.departure,
                    service_day=service_day,
                    provisional=row.provisional,
                    note=row.ticket_note,
                )
            )

    _attach_connections(board, facts, placed, isolated_ids, effective, operator)
    _attach_evidence(board, facts, effective)

    board.stops.sort(
        key=lambda s: (s.trip_id, s.arrival, s.official_stop_code, s.actual_place)
    )
    board.connections.sort(key=lambda c: (c.from_trip, c.to_trip))
    board.isolated.sort(key=lambda i: (i["trip_id"], i["kind"], i["reason"]))
    board.version = board.digest()
    return board


def _pick_best(
    from_rows: list[PlacedCall],
    to_rows: list[PlacedCall],
    candidate: CandidateConnection,
) -> tuple[PlacedCall, PlacedCall] | None:
    """在同一对服务的所有停靠组合里挑一组最合适的。

    候选显式点名了停靠码时，只在该实际停靠上判定；若该站码当日已不存在，返回
    ``None``（连接不能悄悄改挂到别的停靠）。未点名时，满足最少换乘时间且间隔最
    贴近的组合优先。
    """
    if candidate.from_stop is not None:
        from_options = [
            r for r in from_rows if r.official_code == candidate.from_stop
        ]
    else:
        from_options = list(from_rows)
    if candidate.to_stop is not None:
        to_options = [r for r in to_rows if r.official_code == candidate.to_stop]
    else:
        to_options = list(to_rows)
    if not from_options or not to_options:
        return None
    best: tuple[PlacedCall, PlacedCall] | None = None
    best_key: tuple | None = None
    for a in from_options:
        for b in to_options:
            board_at = b.departure or b.arrival
            ready = a.arrival
            minutes = int((board_at - ready).total_seconds() // 60)
            if minutes < 0:
                continue
            # 满足最少换乘时间的组合优先，其次取间隔最小、最贴近实际衔接的。
            # 全都不满足时取间隔最大的一组，给出最有利的判定依据。
            if minutes >= candidate.transfer_minutes:
                key = (0, minutes)
            else:
                key = (1, -minutes)
            if best_key is None or key < best_key:
                best_key = key
                best = (a, b)
    return best


def _merge_candidates(
    candidates: list[CandidateConnection],
) -> list[CandidateConnection]:
    """同一对服务只保留一个候选，冲突时取最保守的确认范围。

    资料里同一对服务可能同时给出可售与待定两行；以待定/不可用为准，不能因为
    一行写着 ``available`` 就把未确认的一侧当成可售。
    """
    merged: dict[tuple[str, str], CandidateConnection] = {}
    severity = {"available": 0, "pending": 1, "unavailable": 2}
    for candidate in candidates:
        key = (candidate.from_trip, candidate.to_trip)
        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            continue
        confirmed = dict(current.confirmed)
        for name, flag in candidate.confirmed.items():
            confirmed[name] = bool(confirmed.get(name, True)) and bool(flag)
        state = max(
            (current.state, candidate.state), key=lambda s: severity.get(s, 1)
        )
        reasons = "；".join(
            dict.fromkeys(
                [r for r in (current.reason, candidate.reason) if r]
            )
        )
        merged[key] = CandidateConnection(
            from_trip=candidate.from_trip,
            to_trip=candidate.to_trip,
            transfer_minutes=max(
                current.transfer_minutes, candidate.transfer_minutes
            ),
            state=state,
            reason=reasons,
            from_stop=current.from_stop or candidate.from_stop,
            to_stop=current.to_stop or candidate.to_stop,
            requires_accessible=(
                current.requires_accessible or candidate.requires_accessible
            ),
            confirmed=confirmed,
        )
    return [merged[key] for key in sorted(merged)]


def _attach_connections(
    board: DayBoard,
    facts: Facts,
    placed: dict[str, list[PlacedCall]],
    isolated_ids: set[str],
    effective: dict[str, EffectiveTrip],
    operator: str | None,
) -> None:
    operators = {trip_id: result.trip.operator for trip_id, result in effective.items()}
    for trip_id, trip in facts.trips.items():
        operators.setdefault(trip_id, trip.operator)

    for candidate in _merge_candidates(list(facts.connections)):
        if operator is not None and operator not in {
            operators.get(candidate.from_trip),
            operators.get(candidate.to_trip),
        }:
            continue
        from_rows = placed.get(candidate.from_trip)
        to_rows = placed.get(candidate.to_trip)
        if from_rows is None or to_rows is None:
            missing = [
                trip_id
                for trip_id, rows in (
                    (candidate.from_trip, from_rows),
                    (candidate.to_trip, to_rows),
                )
                if rows is None
            ]
            if all(t in isolated_ids for t in missing):
                # 一侧被隔离：受影响连接不能带出旧版另一侧的时刻。
                _emit_undefined(
                    board,
                    candidate,
                    "pending",
                    "；".join(
                        dict.fromkeys(
                            [r for r in [candidate.reason] if r]
                            + [f"{t} 已隔离，受影响连接暂不可交割" for t in missing]
                        )
                    ),
                )
            else:
                _emit_undefined(
                    board,
                    candidate,
                    "unavailable",
                    f"服务 {'、'.join(missing)} 在该运营日没有可用停靠",
                )
            continue

        chosen = _pick_best(from_rows, to_rows, candidate)
        if chosen is None:
            pinned_missing = [
                code
                for code, rows in (
                    (candidate.from_stop, from_rows),
                    (candidate.to_stop, to_rows),
                )
                if code is not None and not any(r.official_code == code for r in rows)
            ]
            _emit_undefined(
                board,
                candidate,
                "unavailable",
                (
                    f"候选指定的停靠 {'、'.join(pinned_missing)} 在该运营日已不存在"
                    if pinned_missing
                    else candidate.reason or "两班车在该运营日没有可行的到发顺序"
                ),
            )
            continue
        a, b = chosen
        board_at = b.departure or b.arrival
        transfer_minutes = int((board_at - a.arrival).total_seconds() // 60)
        walk = path_walk_minutes(
            facts.paths, a.actual_place, b.actual_place, board_at
        )

        reasons: list[str] = []
        kind = "available"

        if transfer_minutes < candidate.transfer_minutes:
            kind = "unavailable"
            reasons.append(
                f"到发间隔 {transfer_minutes} 分钟少于要求的 "
                f"{candidate.transfer_minutes} 分钟"
            )

        path_check = check_path(
            list(facts.paths),
            a.actual_place,
            b.actual_place,
            board_at,
            accessible=candidate.requires_accessible,
        )
        path_confirmed_by_data = path_check.confirmed
        if not path_check.usable:
            kind = "unavailable"
            reasons.append(path_check.reason)
        elif walk and walk > transfer_minutes:
            # 到发间隔比步行还短，旅客不可能走完。
            kind = "unavailable"
            reasons.append(
                f"到发间隔 {transfer_minutes} 分钟不足以走完 {walk} 分钟通道"
            )

        confirmed = candidate.confirmed or {}
        from_confirmed = confirmed.get("from", True) and not a.provisional
        to_confirmed = confirmed.get("to", True) and not b.provisional
        path_confirmed = confirmed.get("path", True) and path_confirmed_by_data
        stop_confirmed = confirmed.get("stop", True)
        if not all((from_confirmed, to_confirmed, path_confirmed, stop_confirmed)):
            if kind == "available":
                kind = "pending"
            missing_labels = [
                label
                for label, flag in (
                    ("发车站停靠", from_confirmed),
                    ("接续站停靠", to_confirmed),
                    ("步行通道", path_confirmed),
                    ("改线替代站", stop_confirmed),
                )
                if not flag
            ]
            reasons.append("待定：" + "、".join(missing_labels) + "尚未确认")
        if candidate.state != "available" and kind != "unavailable":
            kind = "pending"
            if candidate.reason:
                reasons.append(candidate.reason)

        reason = "；".join(dict.fromkeys(r for r in reasons if r))
        if not reason:
            reason = (
                f"到发间隔 {transfer_minutes} 分钟（要求 "
                f"{candidate.transfer_minutes} 分钟），停靠、通道与确认范围均满足"
            )
        board.connections.append(
            ConnectionFact(
                from_trip=candidate.from_trip,
                to_trip=candidate.to_trip,
                from_operator=a.operator,
                to_operator=b.operator,
                state=kind,
                reason=reason,
                from_stop_code=a.official_code,
                to_stop_code=b.official_code,
                from_place=a.actual_place,
                to_place=b.actual_place,
                transfer_minutes=transfer_minutes,
                required_minutes=candidate.transfer_minutes,
                path_id=path_check.path_id,
                walk_minutes=path_check.walk_minutes,
                confirmed_from=bool(from_confirmed),
                confirmed_to=bool(to_confirmed),
                requires_accessible=candidate.requires_accessible,
            )
        )


def _emit_undefined(
    board: DayBoard,
    candidate: CandidateConnection,
    state: str,
    reason: str,
) -> None:
    board.connections.append(
        ConnectionFact(
            from_trip=candidate.from_trip,
            to_trip=candidate.to_trip,
            from_operator="",
            to_operator="",
            state=state,
            reason=reason,
            from_stop_code=candidate.from_stop or "",
            to_stop_code=candidate.to_stop or "",
            from_place="",
            to_place="",
            transfer_minutes=None,
            required_minutes=candidate.transfer_minutes,
            path_id=None,
            walk_minutes=None,
            confirmed_from=bool(candidate.confirmed.get("from", True)),
            confirmed_to=bool(candidate.confirmed.get("to", True)),
            requires_accessible=candidate.requires_accessible,
        )
    )


def _attach_evidence(
    board: DayBoard,
    facts: Facts,
    effective: dict[str, EffectiveTrip],
) -> None:
    by_id = {i.id: i for i in facts.instructions}
    instructions: list[dict[str, Any]] = []
    for trip_id in sorted(effective):
        result = effective[trip_id]
        for change in result.applied:
            instruction = by_id.get(change.instruction_id)
            if instruction is None:
                continue
            effective_value: Any
            if instruction.effective is None:
                effective_value = None
            elif isinstance(instruction.effective, date):
                effective_value = instruction.effective.isoformat()
            else:
                start, end = instruction.effective
                effective_value = [start.isoformat(), end.isoformat()]
            instructions.append(
                {
                    "trip": trip_id,
                    "instruction": instruction.id,
                    "action": instruction.action,
                    "stop": change.stop,
                    "new_stop": change.new_stop,
                    "revision": instruction.revision,
                    "effective": effective_value,
                    "received_at": instruction.received_at.isoformat()
                    if instruction.received_at
                    else None,
                    "supersedes": sorted(instruction.supersedes),
                    "confirmed": instruction.confirmed,
                }
            )
    instructions.sort(key=lambda e: (e["trip"], e["instruction"]))
    board.evidence = {
        "instructions": instructions,
        "calendars": {
            op: {
                "timezone": cal.timezone,
                "service_day_starts": cal.service_day_starts,
            }
            for op, cal in sorted(facts.calendars.items())
        },
        "paths": [],
    }
    for path in sorted(
        facts.paths, key=lambda p: (p.path_id, p.from_place, p.to_place)
    ):
        board.evidence["paths"].append(
            {
                "id": path.path_id,
                "from": path.from_place,
                "to": path.to_place,
                "walk_minutes": path.walk_minutes,
                "valid_from": path.valid_from.isoformat() if path.valid_from else None,
                "valid_to": path.valid_to.isoformat() if path.valid_to else None,
                "open_windows": [list(w) for w in path.open_windows],
                "wait_after_close_minutes": path.wait_after_close_minutes,
                "confirmed": path.confirmed,
            }
        )
