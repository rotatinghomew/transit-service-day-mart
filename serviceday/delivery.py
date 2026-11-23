"""版本化交割、重取一致性与前后差异解释。

下游按 (运营日, 版本号) 取数；同一版本重取必须得到完全相同的依据与内容。
调度日后用旧票核对时，可同时查：
* 交割时的版本（当时的依据）；
* 当前更正结果（按同一版本号重新计算，若资料已变则版本号也会变，此时通过
  ``compare`` 得到“变化原因”）。

交割包是纯数据（JSON 可序列化），不依赖运行时对象，便于落盘与下游复用。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from typing import Any

from .board import DayBoard, stable_digest
from .engine import build_day
from .model import Facts


@dataclass(frozen=True)
class Delivery:
    """一次交割：运营日 + 版本号 + 内容与依据。"""

    service_day: date
    version: str
    payload: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


class DeliveryStore:
    """内存交割库：按运营日保存已交割版本，支持重取与更正。"""

    def __init__(self) -> None:
        self._by_day: dict[date, dict[str, Delivery]] = {}
        self._latest: dict[date, str] = {}
        self._history: dict[date, list[str]] = {}

    def deliver(self, facts: Facts, service_day: date) -> Delivery:
        board = build_day(facts, service_day)
        delivery = Delivery(
            service_day=service_day,
            version=board.version,
            payload=board.as_json(),
        )
        versions = self._by_day.setdefault(service_day, {})
        versions[board.version] = delivery
        self._latest[service_day] = board.version
        history = self._history.setdefault(service_day, [])
        if board.version not in history:
            history.append(board.version)
        return delivery

    def get(self, service_day: date, version: str | None = None) -> Delivery:
        versions = self._by_day.get(service_day)
        if not versions:
            raise KeyError(f"运营日 {service_day.isoformat()} 尚未交割")
        chosen = version or self._latest.get(service_day)
        if chosen is None or chosen not in versions:
            raise KeyError(
                f"运营日 {service_day.isoformat()} 没有版本 {version!r}"
            )
        return versions[chosen]

    def versions(self, service_day: date) -> list[str]:
        return list(self._history.get(service_day, []))

    def latest(self, service_day: date) -> Delivery:
        return self.get(service_day)

    def deliver_correction(self, facts: Facts, service_day: date) -> dict[str, Any]:
        """交割一次更正：返回新版本，并列出相对上一版需要撤回/保留/新增的行。

        接收方据此可以整包替换，而不会出现“半张表已换站、另一半还在卖旧换乘”。
        """
        previous = None
        if service_day in self._latest:
            previous = self.get(service_day, self._latest[service_day])
        delivery = self.deliver(facts, service_day)
        manifest: dict[str, Any] = {
            "service_day": service_day.isoformat(),
            "previous_version": previous.version if previous else None,
            "version": delivery.version,
            "retract": [],
            "keep": [],
            "add": [],
        }
        if previous is None:
            manifest["add"] = _row_keys(delivery.payload)
            manifest["manifest_version"] = stable_digest(manifest)
            return manifest

        before_stops = {
            (s["trip_id"], s["arrival"], s["official_stop_code"]): s
            for s in previous.payload.get("stops", [])
        }
        after_stops = {
            (s["trip_id"], s["arrival"], s["official_stop_code"]): s
            for s in delivery.payload.get("stops", [])
        }
        before_connections = {
            (c["from_trip"], c["to_trip"]): c
            for c in previous.payload.get("connections", [])
        }
        after_connections = {
            (c["from_trip"], c["to_trip"]): c
            for c in delivery.payload.get("connections", [])
        }

        for key, row in before_stops.items():
            if key not in after_stops:
                manifest["retract"].append(
                    {"kind": "stop", "trip_id": key[0], "row": row}
                )
            elif _row_fingerprint(row) != _row_fingerprint(after_stops[key]):
                # 同一停靠内容有变（如实际停靠点、票面说明）：旧行撤回、新行新增。
                manifest["retract"].append(
                    {"kind": "stop", "trip_id": key[0], "row": row}
                )
                manifest["add"].append(
                    {"kind": "stop", "trip_id": key[0], "row": after_stops[key]}
                )
            else:
                manifest["keep"].append({"kind": "stop", "trip_id": key[0], "row": row})
        for key, row in after_stops.items():
            if key not in before_stops:
                manifest["add"].append(
                    {"kind": "stop", "trip_id": key[0], "row": row}
                )
        for key, row in before_connections.items():
            if key not in after_connections:
                manifest["retract"].append(
                    {
                        "kind": "connection",
                        "from_trip": key[0],
                        "to_trip": key[1],
                        "row": row,
                    }
                )
            elif _row_fingerprint(row) != _row_fingerprint(after_connections[key]):
                manifest["retract"].append(
                    {
                        "kind": "connection",
                        "from_trip": key[0],
                        "to_trip": key[1],
                        "row": row,
                    }
                )
                manifest["add"].append(
                    {
                        "kind": "connection",
                        "from_trip": key[0],
                        "to_trip": key[1],
                        "row": after_connections[key],
                    }
                )
            else:
                manifest["keep"].append(
                    {
                        "kind": "connection",
                        "from_trip": key[0],
                        "to_trip": key[1],
                        "row": row,
                    }
                )
        for key, row in after_connections.items():
            if key not in before_connections:
                manifest["add"].append(
                    {
                        "kind": "connection",
                        "from_trip": key[0],
                        "to_trip": key[1],
                        "row": row,
                    }
                )
        manifest["manifest_version"] = stable_digest(manifest)
        return manifest


def _row_fingerprint(row: dict[str, Any]) -> str:
    return stable_digest(row)


def _row_keys(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stop in payload.get("stops", []):
        rows.append({"kind": "stop", "trip_id": stop["trip_id"], "row": stop})
    for connection in payload.get("connections", []):
        rows.append(
            {
                "kind": "connection",
                "from_trip": connection["from_trip"],
                "to_trip": connection["to_trip"],
                "row": connection,
            }
        )
    return rows


class RefetchMismatch(AssertionError):
    """同一版本重取得到不同内容或依据。"""


def refetch(
    store: DeliveryStore, facts: Facts, service_day: date, version: str
) -> Delivery:
    """重取同一版本；若重算结果与已交割版本不一致，说明资料发生了漂移。"""
    stored = store.get(service_day, version)
    recomputed = build_day(facts, service_day)
    if recomputed.version != version:
        raise RefetchMismatch(
            f"运营日 {service_day.isoformat()} 重取版本 {version} 时算得 "
            f"{recomputed.version}，资料或版本已变化"
        )
    if stable_digest(stored.payload) != stable_digest(recomputed.as_json()):
        raise RefetchMismatch(
            f"运营日 {service_day.isoformat()} 版本 {version} 重取内容不一致"
        )
    return stored


@dataclass(frozen=True)
class ChangeExplanation:
    """连接在两次交割之间为何变化。"""

    from_trip: str
    to_trip: str
    before_state: str | None
    after_state: str | None
    causes: tuple[str, ...]  # stop / path / confirmation / timing / service

    def as_json(self) -> dict[str, Any]:
        return {
            "from_trip": self.from_trip,
            "to_trip": self.to_trip,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "causes": list(self.causes),
        }


_CAUSE_MARKERS = {
    "stop": ("停靠", "站码", "替代站", "隔离", "没有可用停靠", "改线"),
    "path": ("通道", "步行", "开放时段", "可通行"),
    "confirmation": ("待定", "确认", "未确认"),
    "timing": ("间隔", "分钟", "时刻"),
}


def _causes(before: dict[str, Any] | None, after: dict[str, Any] | None) -> tuple[str, ...]:
    if before is None and after is None:
        return ()
    causes: list[str] = []
    if (before or {}).get("from_place") != (after or {}).get("from_place") or (
        before or {}
    ).get("to_place") != (after or {}).get("to_place"):
        causes.append("stop")
    if (before or {}).get("from_stop_code") != (after or {}).get("from_stop_code") or (
        before or {}
    ).get("to_stop_code") != (after or {}).get("to_stop_code"):
        causes.append("stop")
    text = " ".join(
        [
            str((before or {}).get("reason", "")),
            str((after or {}).get("reason", "")),
        ]
    )
    for cause, markers in _CAUSE_MARKERS.items():
        if any(marker in text for marker in markers):
            if cause not in causes:
                causes.append(cause)
    if (before or {}).get("state") == "pending" or (after or {}).get("state") == "pending":
        if "confirmation" not in causes:
            causes.append("confirmation")
    if not causes:
        causes.append("service")
    return tuple(causes)


def compare(
    before: Delivery | DayBoard,
    after: Delivery | DayBoard,
) -> list[ChangeExplanation]:
    """比较两次交割的连接变化，给出停靠/路径/确认范围等原因。"""
    before_payload = (
        before.payload if isinstance(before, Delivery) else before.as_json()
    )
    after_payload = after.payload if isinstance(after, Delivery) else after.as_json()
    before_map = {
        (c["from_trip"], c["to_trip"]): c
        for c in before_payload.get("connections", [])
    }
    after_map = {
        (c["from_trip"], c["to_trip"]): c
        for c in after_payload.get("connections", [])
    }
    keys = sorted(set(before_map) | set(after_map))
    explanations: list[ChangeExplanation] = []
    for key in keys:
        b = before_map.get(key)
        a = after_map.get(key)
        if b == a:
            continue
        explanations.append(
            ChangeExplanation(
                from_trip=key[0],
                to_trip=key[1],
                before_state=(b or {}).get("state"),
                after_state=(a or {}).get("state"),
                causes=_causes(b, a),
            )
        )
    return explanations


def audit(
    store: DeliveryStore,
    facts: Facts,
    service_day: date,
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """交割后核对：返回指定（或最新）版本与当前更正结果的对照。"""
    delivered = store.get(service_day, version)
    current = build_day(facts, service_day)
    return {
        "service_day": service_day.isoformat(),
        "delivered_version": delivered.version,
        "current_version": current.version,
        "delivered": delivered.as_json(),
        "current": current.as_json(),
        "changes": [
            e.as_json() for e in compare(delivered, current)
        ],
    }
