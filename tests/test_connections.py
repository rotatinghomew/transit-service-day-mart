"""票面站码/实际停靠点、步行通道与换乘判定的测试。"""
import unittest
from datetime import date, datetime

from serviceday.engine import build_day
from serviceday.loader import parse_facts
from serviceday.model import StopCode
from serviceday.paths import check_path
from serviceday.stops import StopResolutionError, explain_ticket_stop


def _dt(text):
    return datetime.fromisoformat(text)


class StopTests(unittest.TestCase):
    def setUp(self):
        self.stops = {
            ("A", "OLD-17"): StopCode("A", "OLD-17", "P-OLD", valid_to=date(2026, 11, 30)),
            ("A", "NEW-17"): StopCode("A", "NEW-17", "P-NEW", valid_from=date(2026, 12, 1)),
        }

    def test_old_code_still_explains_ticket_within_validity(self):
        meaning = explain_ticket_stop(self.stops, "A", "OLD-17", date(2026, 11, 29))
        self.assertEqual(meaning.actual_place, "P-OLD")

    def test_old_code_outside_validity_is_rejected(self):
        with self.assertRaises(StopResolutionError):
            explain_ticket_stop(self.stops, "A", "OLD-17", date(2026, 12, 2))


class PathTests(unittest.TestCase):
    def _path(self, **kwargs):
        from serviceday.model import WalkPath

        base = dict(
            from_place="P1",
            to_place="P2",
            walk_minutes=8,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 1),
            path_id="W1",
        )
        base.update(kwargs)
        return WalkPath(**base)

    def test_full_day_path_is_usable(self):
        result = check_path(
            [self._path()], "P1", "P2", _dt("2026-11-30T01:00:00+08:00")
        )
        self.assertTrue(result.usable, result.reason)

    def test_half_day_closure_blocks_outside_hours(self):
        path = self._path(open_windows=(("06:00", "12:00"),))
        blocked = check_path([path], "P1", "P2", _dt("2026-11-30T01:00:00+08:00"))
        self.assertFalse(blocked.usable)
        self.assertEqual(blocked.cause, "outside_hours")
        allowed = check_path([path], "P1", "P2", _dt("2026-11-30T11:00:00+08:00"))
        self.assertTrue(allowed.usable, allowed.reason)

    def test_wait_rule_after_close(self):
        path = self._path(
            open_windows=(("06:00", "12:00"),), wait_after_close_minutes=30
        )
        # 12:08 发车，步行 8 分钟，需 12:00 进入；12:00 恰好关门，可等待。
        result = check_path([path], "P1", "P2", _dt("2026-11-30T12:18:00+08:00"))
        self.assertTrue(result.usable, result.reason)
        too_late = check_path(
            [path], "P1", "P2", _dt("2026-11-30T12:50:00+08:00")
        )
        self.assertFalse(too_late.usable)

    def test_path_date_range_end_excludes_later_day(self):
        path = self._path(valid_to=date(2026, 11, 30))
        result = check_path([path], "P1", "P2", _dt("2026-12-01T01:00:00+08:00"))
        self.assertFalse(result.usable)
        self.assertIn(result.cause, {"date_range", "outside_hours"})
        # 12/01 深夜已完全没有可用日期范围。
        later = check_path([path], "P1", "P2", _dt("2026-12-02T12:00:00+08:00"))
        self.assertFalse(later.usable)
        self.assertEqual(later.cause, "date_range")

    def test_accessible_requirement(self):
        path = self._path(accessible=False)
        result = check_path(
            [path], "P1", "P2", _dt("2026-11-30T01:00:00+08:00"), accessible=True
        )
        self.assertFalse(result.usable)
        self.assertEqual(result.cause, "inaccessible")

    def test_missing_path_between_different_places(self):
        result = check_path([], "P1", "P2", _dt("2026-11-30T01:00:00+08:00"))
        self.assertFalse(result.usable)
        self.assertEqual(result.cause, "no_path")

    def test_same_place_needs_no_path(self):
        result = check_path([], "P1", "P1", _dt("2026-11-30T01:00:00+08:00"))
        self.assertTrue(result.usable)


class ConnectionTests(unittest.TestCase):
    def _facts(self, connections, paths=None, instructions=None):
        return parse_facts(
            {
                "service_calendars": [
                    {"operator": "A", "timezone": "Asia/Shanghai", "service_day_starts": "03:00"},
                    {"operator": "B", "timezone": "Asia/Shanghai", "service_day_starts": "04:00"},
                ],
                "stops": [
                    {"operator": "A", "code": "A1", "actual_place": "PA"},
                    {"operator": "B", "code": "B1", "actual_place": "PB"},
                ],
                "trips": [
                    {
                        "id": "A-1",
                        "operator": "A",
                        "service_date": "2026-11-29",
                        "calls": [{"stop": "A1", "arrival": "23:00", "departure": "23:10"}],
                    },
                    {
                        "id": "B-1",
                        "operator": "B",
                        "service_date": "2026-11-29",
                        "calls": [{"stop": "B1", "arrival": "23:40", "departure": "23:45"}],
                    },
                ],
                "instructions": instructions or [],
                "paths": paths or [],
                "connections": connections,
            }
        )

    def test_available_connection(self):
        facts = self._facts(
            [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "available",
                }
            ],
            paths=[
                {
                    "id": "W",
                    "from": "PA",
                    "to": "PB",
                    "walk_minutes": 5,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                }
            ],
        )
        board = build_day(facts, date(2026, 11, 29))
        connection = board.connections[0]
        self.assertEqual(connection.state, "available", connection.reason)
        self.assertTrue(connection.sellable)

    def test_same_pair_available_and_pending_merges_to_pending(self):
        facts = self._facts(
            [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "available",
                },
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "pending",
                    "reason": "通道开放时段未确认",
                },
            ],
            paths=[
                {
                    "id": "W",
                    "from": "PA",
                    "to": "PB",
                    "walk_minutes": 5,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                }
            ],
        )
        board = build_day(facts, date(2026, 11, 29))
        self.assertEqual(len(board.connections), 1)
        self.assertEqual(board.connections[0].state, "pending")
        self.assertFalse(board.connections[0].sellable)

    def test_walk_time_consumes_transfer_margin(self):
        facts = self._facts(
            [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "available",
                }
            ],
            paths=[
                {
                    "id": "W",
                    "from": "PA",
                    "to": "PB",
                    "walk_minutes": 10,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                }
            ],
        )
        board = build_day(facts, date(2026, 11, 29))
        connection = board.connections[0]
        # 23:00 -> 23:45 共 45 分钟；扣步行 10 分钟后 35 分钟，仍够 15 分钟。
        self.assertEqual(connection.transfer_minutes, 45)
        self.assertEqual(connection.state, "available", connection.reason)

    def test_insufficient_interval_is_unavailable(self):
        facts = self._facts(
            [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 60,
                    "state": "available",
                }
            ],
            paths=[
                {
                    "id": "W",
                    "from": "PA",
                    "to": "PB",
                    "walk_minutes": 5,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                }
            ],
        )
        board = build_day(facts, date(2026, 11, 29))
        self.assertEqual(board.connections[0].state, "unavailable")

    def test_connection_uses_actual_places_not_ticket_codes(self):
        # A-1 临时改停到 A9（实际地点 PA-NEW），票面仍是 A1；连接应以 A9 判定。
        raw = {
            "service_calendars": [
                {"operator": "A", "timezone": "Asia/Shanghai", "service_day_starts": "03:00"},
                {"operator": "B", "timezone": "Asia/Shanghai", "service_day_starts": "04:00"},
            ],
            "stops": [
                {"operator": "A", "code": "A1", "actual_place": "PA"},
                {"operator": "A", "code": "A9", "actual_place": "PA-NEW"},
                {"operator": "B", "code": "B1", "actual_place": "PB"},
            ],
            "trips": [
                {
                    "id": "A-1",
                    "operator": "A",
                    "service_date": "2026-11-29",
                    "calls": [{"stop": "A1", "arrival": "23:00", "departure": "23:10"}],
                },
                {
                    "id": "B-1",
                    "operator": "B",
                    "service_date": "2026-11-29",
                    "calls": [{"stop": "B1", "arrival": "23:40", "departure": "23:45"}],
                },
            ],
            "instructions": [
                {
                    "id": "I-1",
                    "trip": "A-1",
                    "action": "replace_stop",
                    "stop": "A1",
                    "new_stop": "A9",
                    "effective": "2026-11-29",
                }
            ],
            "paths": [
                {
                    "id": "W",
                    "from": "PA-NEW",
                    "to": "PB",
                    "walk_minutes": 5,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                }
            ],
            "connections": [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "available",
                }
            ],
        }
        board = build_day(parse_facts(raw), date(2026, 11, 29))
        stop = board.stops[0]
        self.assertEqual(stop.official_stop_code, "A9")
        self.assertEqual(stop.ticket_stop_code, "A1")
        self.assertEqual(stop.actual_place, "PA-NEW")
        connection = board.connections[0]
        self.assertEqual(connection.from_place, "PA-NEW")
        self.assertTrue(connection.sellable, connection.reason)

    def test_connection_requires_path_available_at_transfer_moment(self):
        # 施工只开半天：22:00 发车前通道 06:00-12:00 已关闭。
        facts = self._facts(
            [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "available",
                }
            ],
            paths=[
                {
                    "id": "W",
                    "from": "PA",
                    "to": "PB",
                    "walk_minutes": 5,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                    "open_windows": [["06:00", "12:00"]],
                }
            ],
        )
        board = build_day(facts, date(2026, 11, 29))
        connection = board.connections[0]
        self.assertEqual(connection.state, "unavailable")
        self.assertIn("开放时段", connection.reason)

    def test_connection_wait_rule_keeps_half_day_path_usable(self):
        facts = self._facts(
            [
                {
                    "from_trip": "A-1",
                    "to_trip": "B-1",
                    "transfer_minutes": 15,
                    "state": "available",
                }
            ],
            paths=[
                {
                    "id": "W",
                    "from": "PA",
                    "to": "PB",
                    "walk_minutes": 5,
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                    # 23:45 发车、步行 5 分钟，需 23:40 进入；通道 23:00 关闭，
                    # 等待规则允许关闭后继续等候，旅客仍可完成换乘。
                    "open_windows": [["22:50", "23:00"]],
                    "wait_after_close_minutes": 40,
                }
            ],
        )
        board = build_day(facts, date(2026, 11, 29))
        connection = board.connections[0]
        self.assertEqual(connection.state, "available", connection.reason)


if __name__ == "__main__":
    unittest.main()
