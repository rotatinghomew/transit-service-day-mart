"""更正交割、撤行清单与隔离服务的测试。"""
import unittest
from datetime import date

from serviceday.delivery import DeliveryStore
from serviceday.engine import build_day
from serviceday.loader import parse_facts


def _base(**overrides):
    raw = {
        "service_calendars": [
            {"operator": "A", "timezone": "Asia/Shanghai", "service_day_starts": "03:00"},
            {"operator": "B", "timezone": "Asia/Shanghai", "service_day_starts": "04:00"},
        ],
        "stops": [
            {"operator": "A", "code": "A-OLD", "actual_place": "P-OLD", "valid_to": "2026-11-30"},
            {"operator": "A", "code": "A-NEW", "actual_place": "P-NEW", "valid_from": "2026-12-01"},
            {"operator": "B", "code": "B1", "actual_place": "P-NEW"},
        ],
        "trips": [
            {
                "id": "A-1",
                "operator": "A",
                "service_date": "2026-11-29",
                "calls": [
                    {"stop": "A-OLD", "arrival": "23:00", "departure": "23:05"},
                    {"stop": "A-NEW", "arrival": "00:20", "departure": "00:25", "next_day": True},
                ],
            },
            {
                "id": "B-1",
                "operator": "B",
                "service_date": "2026-11-29",
                "calls": [{"stop": "B1", "arrival": "00:45", "departure": "00:50"}],
            },
        ],
        "instructions": [],
        "paths": [
            {
                "id": "W",
                "from": "P-NEW",
                "to": "P-NEW",
                "walk_minutes": 8,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
            }
        ],
        "connections": [
            {
                "from_trip": "A-1",
                "to_trip": "B-1",
                "transfer_minutes": 10,
                "state": "available",
            }
        ],
    }
    raw.update(overrides)
    return raw


class CorrectionManifestTests(unittest.TestCase):
    def test_correction_manifest_retracts_old_stop_rows(self):
        facts = parse_facts(_base())
        store = DeliveryStore()
        first = store.deliver(facts, date(2026, 11, 29))
        # 临时改线：A-OLD 停用，改停 A-NEW（替代站已确认）。
        changed = parse_facts(
            _base(
                instructions=[
                    {
                        "id": "I-1",
                        "trip": "A-1",
                        "action": "cancel_call",
                        "stop": "A-OLD",
                        "effective": "2026-11-29",
                        "received_at": "2026-11-29T12:00:00+08:00",
                    }
                ],
                connections=[
                    {
                        "from_trip": "A-1",
                        "to_trip": "B-1",
                        "transfer_minutes": 10,
                        "state": "available",
                        "from_stop": "A-NEW",
                    }
                ],
            )
        )
        manifest = store.deliver_correction(changed, date(2026, 11, 29))
        self.assertEqual(manifest["previous_version"], first.version)
        self.assertNotEqual(manifest["version"], first.version)
        retracted_trips = {row["trip_id"] for row in manifest["retract"] if row["kind"] == "stop"}
        self.assertIn("A-1", retracted_trips)
        # 撤行清单里不能还留着旧停靠点的可售连接。
        kept_connections = [
            row for row in manifest["keep"] if row["kind"] == "connection"
        ]
        for row in kept_connections:
            self.assertNotEqual(row["row"]["from_stop_code"], "A-OLD")
        # 清单自身可稳定重算。
        self.assertTrue(manifest["manifest_version"])

    def test_first_delivery_manifest_adds_everything(self):
        store = DeliveryStore()
        manifest = store.deliver_correction(
            parse_facts(_base()), date(2026, 11, 29)
        )
        self.assertIsNone(manifest["previous_version"])
        self.assertEqual(manifest["retract"], [])
        self.assertGreater(len(manifest["add"]), 0)


class AddedServiceTests(unittest.TestCase):
    def test_added_service_appears_on_its_day_only(self):
        facts = parse_facts(
            _base(
                instructions=[
                    {
                        "id": "ADD-1",
                        "trip": "A-900",
                        "action": "add_service",
                        "operator": "A",
                        "service_date": "2026-11-29",
                        "effective": "2026-11-29",
                        "calls": [
                            {"stop": "A-OLD", "arrival": "21:00", "departure": "21:05"}
                        ],
                    }
                ]
            )
        )
        board = build_day(facts, date(2026, 11, 29))
        added = [s for s in board.stops if s.trip_id == "A-900"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].arrival.hour, 21)
        other_day = build_day(facts, date(2026, 11, 30))
        self.assertFalse([s for s in other_day.stops if s.trip_id == "A-900"])

    def test_cancelled_added_service_is_absent(self):
        facts = parse_facts(
            _base(
                instructions=[
                    {
                        "id": "ADD-1",
                        "trip": "A-900",
                        "action": "add_service",
                        "operator": "A",
                        "service_date": "2026-11-29",
                        "effective": "2026-11-29",
                        "calls": [
                            {"stop": "A-OLD", "arrival": "21:00", "departure": "21:05"}
                        ],
                    },
                    {
                        "id": "CANCEL-900",
                        "trip": "A-900",
                        "action": "cancel_service",
                        "effective": "2026-11-29",
                        "received_at": "2026-11-29T10:00:00+08:00",
                    },
                ]
            )
        )
        board = build_day(facts, date(2026, 11, 29))
        self.assertFalse([s for s in board.stops if s.trip_id == "A-900"])


class IsolationTests(unittest.TestCase):
    def _ambiguous_facts(self):
        raw = _base()
        raw["service_calendars"] = [
            {"operator": "A", "timezone": "America/New_York", "service_day_starts": "03:00"},
            {"operator": "B", "timezone": "Asia/Shanghai", "service_day_starts": "04:00"},
        ]
        raw["trips"][0]["service_date"] = "2026-10-31"
        raw["trips"][0]["calls"] = [
            {"stop": "A-OLD", "arrival": "01:30", "departure": "01:35"}
        ]
        # B 的同日服务仍然正常，用来验证“一侧隔离、另一侧独立可用”。
        raw["trips"][1]["service_date"] = "2026-10-31"
        raw["paths"] = [
            {
                "id": "W",
                "from": "P-OLD",
                "to": "P-NEW",
                "walk_minutes": 8,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
            }
        ]
        return parse_facts(raw)

    def test_ambiguous_service_is_isolated_and_connection_not_sold(self):
        facts = self._ambiguous_facts()
        board = build_day(facts, date(2026, 10, 31))
        self.assertTrue(any(i["trip_id"] == "A-1" for i in board.isolated))
        # 受影响连接应保留为待定，且不得带出旧版另一侧的时刻。
        connection = next(c for c in board.connections if c.from_trip == "A-1")
        self.assertNotEqual(connection.state, "available")
        self.assertFalse(connection.sellable)
        self.assertIsNone(connection.transfer_minutes)
        self.assertIn("隔离", connection.reason)

    def test_independent_stops_still_published_when_other_side_isolated(self):
        facts = self._ambiguous_facts()
        board = build_day(facts, date(2026, 10, 31))
        # B 方在自己的运营日仍可独立发布。
        b_stops = [s for s in board.stops if s.operator == "B"]
        self.assertEqual(len(b_stops), 1)
        self.assertFalse([s for s in board.stops if s.operator == "A"])

    def test_cancelled_service_connection_not_revived_from_old_package(self):
        facts = parse_facts(
            _base(
                instructions=[
                    {
                        "id": "C-1",
                        "trip": "A-1",
                        "action": "cancel_service",
                        "effective": "2026-11-29",
                    }
                ]
            )
        )
        board = build_day(facts, date(2026, 11, 29))
        connection = board.connections[0]
        self.assertEqual(connection.state, "unavailable")
        self.assertFalse(connection.sellable)


if __name__ == "__main__":
    unittest.main()
