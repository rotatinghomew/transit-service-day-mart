"""步行通道可用性检查。

通道资料可能给出：
* 有效期 ``valid_from``/``valid_to``（含当日）；
* 每日可通行时段 ``open_windows``（空表示全日）；
* 可通行时段的日期范围 ``window_from``/``window_to``（施工只开半天时常用）；
* 末段通行结束后仍可在通道内等待的分钟数 ``wait_after_close_minutes``；
* ``confirmed`` 表示时段本身是否已由站方核实。

换乘判定要求旅客能在后一班车发车前，在通道实际开放的时间范围内走完；
若两停靠点相同且未登记通道，则视为无需步行。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .model import WalkPath
from .service_day import parse_hhmm


@dataclass(frozen=True)
class PathCheck:
    usable: bool
    reason: str
    path_id: str | None = None
    walk_minutes: int = 0
    confirmed: bool = True
    # 用于解释“为什么变了”：none / date_range / outside_hours / no_path / inaccessible
    cause: str = "ok"


def _windows_for(
    path: WalkPath, day: date, tz: ZoneInfo, *, offset_days: int = 0
) -> list[tuple[datetime, datetime]]:
    """返回以 ``day + offset_days`` 为起始日的可通行时段。

    每个时段按其起始日判断日期范围，因此跨零点的通道在换乘前后两天都成立。
    """
    start_day = day + timedelta(days=offset_days)
    if not path.windows_available_on(start_day) or not path.valid_on(start_day):
        return []
    if not path.open_windows:
        open_at = datetime.combine(start_day, time(0, 0), tzinfo=tz)
        return [(open_at, open_at + timedelta(days=1))]
    windows: list[tuple[datetime, datetime]] = []
    for start, end in path.open_windows:
        start_dt = datetime.combine(start_day, parse_hhmm(start), tzinfo=tz)
        end_dt = datetime.combine(start_day, parse_hhmm(end), tzinfo=tz)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        windows.append((start_dt, end_dt))
    return windows


def _all_windows(
    path: WalkPath, day: date, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """覆盖换乘可能跨越的前后各一天。"""
    windows: list[tuple[datetime, datetime]] = []
    for offset in (-1, 0, 1):
        windows.extend(_windows_for(path, day, tz, offset_days=offset))
    return windows


def _date_range_reason(path: WalkPath) -> str:
    bounds = [
        b
        for b in (path.window_from, path.window_to, path.valid_from, path.valid_to)
        if b is not None
    ]
    if bounds:
        return (
            f"通道仅在 {min(bounds).isoformat()} 至 {max(bounds).isoformat()} 适用"
        )
    return "通道无可通行日期范围"


def check_path(
    paths: list[WalkPath],
    from_place: str,
    to_place: str,
    boarding: datetime,
    *,
    accessible: bool = False,
) -> PathCheck:
    """检查能否在 ``boarding``（后一班车发车，带时区）前从 ``from_place``
    走到 ``to_place``。"""
    tz = boarding.tzinfo
    if tz is None:
        return PathCheck(False, "发车时刻缺少时区，无法核对通道时段", cause="no_tz")
    local_day = boarding.astimezone(tz).date()

    matching = [
        p
        for p in paths
        if {p.from_place, p.to_place} == {from_place, to_place}
    ]
    if not matching:
        if from_place == to_place:
            return PathCheck(True, "同一实际停靠点，无需步行通道", walk_minutes=0)
        return PathCheck(
            False,
            f"两停靠点 {from_place} 与 {to_place} 之间没有登记可走通道",
            cause="no_path",
        )

    failures: list[PathCheck] = []
    for path in matching:
        if accessible and not path.accessible:
            failures.append(
                PathCheck(
                    False,
                    f"通道 {path.path_id or '未命名'} 非无障碍",
                    path_id=path.path_id,
                    walk_minutes=path.walk_minutes,
                    confirmed=path.confirmed,
                    cause="inaccessible",
                )
            )
            continue
        # 换乘可能从前一天深夜开始，通道按「开放时段的起始日」判断日期范围，
        # 因此前后各看一天；不再要求 boarding 当天本身在有效期内。
        windows = _all_windows(path, local_day, tz)
        if not windows:
            failures.append(
                PathCheck(
                    False,
                    f"{_date_range_reason(path)}，{local_day.isoformat()} 前后无开放时段",
                    path_id=path.path_id,
                    walk_minutes=path.walk_minutes,
                    confirmed=path.confirmed,
                    cause="date_range",
                )
            )
            continue
        latest_start = boarding - timedelta(minutes=path.walk_minutes)
        grace = timedelta(minutes=path.wait_after_close_minutes)
        result: PathCheck | None = None
        for open_at, close_at in windows:
            if open_at <= latest_start <= close_at:
                result = PathCheck(
                    True,
                    "旅客可在通道开放时段内走完",
                    path_id=path.path_id,
                    walk_minutes=path.walk_minutes,
                    confirmed=path.confirmed,
                )
                break
            if close_at < latest_start <= close_at + grace:
                result = PathCheck(
                    True,
                    "旅客可在通道结束后按等待规则通行",
                    path_id=path.path_id,
                    walk_minutes=path.walk_minutes,
                    confirmed=path.confirmed,
                )
                break
        if result is not None:
            return result
        failures.append(
            PathCheck(
                False,
                f"换乘时刻 {latest_start.astimezone(tz).strftime('%Y-%m-%d %H:%M')} 不在"
                f"通道开放时段 {', '.join(f'{s}-{e}' for s, e in path.open_windows) or '全日'} 内",
                path_id=path.path_id,
                walk_minutes=path.walk_minutes,
                confirmed=path.confirmed,
                cause="outside_hours",
            )
        )

    if not failures:
        failures.append(
            PathCheck(False, "两停靠点之间没有可走通的通道", cause="no_path")
        )
    distinct = []
    for failure in failures:
        if failure.reason not in [f.reason for f in distinct]:
            distinct.append(failure)
    return PathCheck(
        False,
        "；".join(f.reason for f in distinct),
        path_id=distinct[0].path_id,
        walk_minutes=distinct[0].walk_minutes,
        confirmed=all(f.confirmed for f in distinct),
        cause=distinct[0].cause,
    )


def walk_minutes(
    paths: list[WalkPath], from_place: str, to_place: str, boarding: datetime
) -> int | None:
    if from_place == to_place and not any(
        {p.from_place, p.to_place} == {from_place, to_place} for p in paths
    ):
        return 0
    for path in paths:
        if {path.from_place, path.to_place} == {from_place, to_place}:
            return path.walk_minutes
    return None
