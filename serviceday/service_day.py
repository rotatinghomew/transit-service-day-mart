"""运营日归属与本地时间的确定性解析。

规则：
* 每次停靠取该运营商运营日窗口 [服务日 D 的起算时刻, D+1 的起算时刻)；
  23:45 落在 D 当晚，00:35 落在 D+1 凌晨，二者属于同一运营日。
* 每班车第一个停靠是日期锚点；后续停靠按资料里的 ``next_day`` 声明跨日，
  否则若墙钟大幅回退视为次日（资料不足时宁可报歧义）。
* 本地时间重复（夏令时回拨）或缺失（夏令时前拨）且无明确偏移可依时，抛
  ``TimeAmbiguityError``，由调用方隔离相关服务。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .model import Call, FixtureError, ServiceCalendar, TimeAmbiguityError, Trip

_MIDNIGHT_CUTOFF = time(12, 0)


def parse_hhmm(value: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (AttributeError, TypeError, ValueError) as exc:
        raise FixtureError(f"非法时刻 {value!r}，应为 HH:MM") from exc


def calendar_start(calendar: ServiceCalendar) -> time:
    return parse_hhmm(calendar.service_day_starts)


def resolve_local(
    service_date: date,
    clock: time,
    calendar: ServiceCalendar,
    *,
    cross_midnight: bool = False,
    fold: int | None = None,
) -> datetime:
    """把运营日 + 墙钟解析成带时区的绝对时刻。

    ``fold`` 显式给出时按它取重复时刻中的一次；否则遇到重复/缺失报歧义。
    """
    tz = ZoneInfo(calendar.timezone)
    start = calendar_start(calendar)
    nominal_day = service_date + timedelta(days=1 if cross_midnight else 0)
    candidate = datetime.combine(nominal_day, clock, tzinfo=tz)

    window_open = datetime.combine(service_date, start, tzinfo=tz)
    window_close = window_open + timedelta(days=1)
    if candidate < window_open:
        candidate += timedelta(days=1)
    elif candidate >= window_close:
        if cross_midnight:
            # 资料声明跨日，但落在下一个运营日窗口内：只看墙钟无法判定该停靠
            # 属于哪个运营日，必须隔离。
            raise TimeAmbiguityError(
                f"服务日 {service_date.isoformat()} 的跨日停靠 {clock.strftime('%H:%M')} "
                f"超出运营日窗口（{calendar.timezone}，{start.strftime('%H:%M')} 起算）"
            )
        candidate -= timedelta(days=1)

    # fold=0/1 给出该墙钟在本地时区的两个可能偏移；不同即存在重复或缺失。
    first = datetime.combine(candidate.date(), clock, tzinfo=tz).replace(fold=0)
    second = datetime.combine(candidate.date(), clock, tzinfo=tz).replace(fold=1)
    distinct = first.utcoffset() != second.utcoffset() or (
        first.astimezone(timezone.utc) != second.astimezone(timezone.utc)
    )
    if distinct:
        if fold is None:
            raise TimeAmbiguityError(
                f"{service_date.isoformat()} {clock.strftime('%H:%M')} 在 "
                f"{calendar.timezone} 存在重复或缺失的本地时刻，资料未给出偏移"
            )
        candidate = candidate.replace(fold=fold)
    return candidate


def call_times(
    trip: Trip, calendar: ServiceCalendar
) -> list[tuple[Call, datetime, datetime | None]]:
    """返回 [(停靠, 到达绝对时刻, 发车绝对时刻)]。"""
    if not trip.calls:
        raise FixtureError(f"服务 {trip.id} 没有停靠")
    results: list[tuple[Call, datetime, datetime | None]] = []
    previous: datetime | None = None
    for index, call in enumerate(trip.calls):
        arrival_clock = parse_hhmm(call.arrival or call.departure)
        cross = bool(call.next_day) and index > 0
        if index > 0 and previous is not None:
            prev_clock = parse_hhmm(trip.calls[index - 1].arrival or trip.calls[index - 1].departure)
            if prev_clock >= _MIDNIGHT_CUTOFF and arrival_clock < _MIDNIGHT_CUTOFF:
                cross = True
        arrival = resolve_local(
            trip.service_date,
            arrival_clock,
            calendar,
            cross_midnight=cross,
            fold=call.fold,
        )
        if previous is not None and arrival < previous:
            raise TimeAmbiguityError(
                f"服务 {trip.id} 停靠 {call.stop} 早于前一停靠，无法按运营日排序"
            )
        departure = None
        if call.departure:
            departure = resolve_local(
                trip.service_date,
                parse_hhmm(call.departure),
                calendar,
                cross_midnight=cross,
                fold=call.fold,
            )
            if departure < arrival:
                departure += timedelta(days=1)
        results.append((call, arrival, departure))
        previous = arrival
    return results


def service_day_of(moment: datetime, calendar: ServiceCalendar) -> date:
    """某绝对时刻属于哪个运营日。"""
    local = moment.astimezone(ZoneInfo(calendar.timezone))
    start = calendar_start(calendar)
    if local.time() >= start:
        return local.date()
    return local.date() - timedelta(days=1)
