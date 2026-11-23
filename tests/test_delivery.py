"""交割、重取一致性与审计查询的测试。"""
import json
import unittest
from datetime import date

from serviceday.delivery import (
    DeliveryStore,
    RefetchMismatch,
    audit,
    compare,
    refetch,
)
from serviceday.engine import build_day
from serviceday.loader import parse_facts


def _raw(**overrides):
    raw = {
        "service_calendars": [
            {"operator": "A", "timezone": "Asia/Shanghai", "service_day_starts": "03:00"},
            {"operator": "B", "timezone": "Asia/Shanghai", "service_day_starts": "04:00"},
        ],
        "stops": [
            {"operator": "A", "code": "A1", "actual_place": "P1"},
            {"operator": "A", "code": "A2", "actual_place": "P2"},
            {"operator": "B", "code": "B1", "actual_place": "P2"},
        ],
        "trips": [
            {
                "id": "A-1",
                "operator": "A",
                "service_date": "2026-11-29",
                "calls": [
                    {"stop": "A1", "arrival": "22:00", "departure": "22:05"},
                    {"stop": "A2", "arrival": "23:30", "departure": "23:35"},
                ],
            },
            {
                "id": "B-1",
                "operator": "B",
                "service_date": "2026-11-29",
                "calls": [{"stop": "B1", "arrival": "23:55", "departure": "00:00"}],
            },
        ],
        "instructions": [],
        "paths": [
            {
                "id": "W",
                "from": "P2",
                "to": "P2",
                "walk_minutes": 5,
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


class DeliveryTests(unittest.TestCase):
    def test_refetch_same_version_is_identical(self):
        facts = parse_facts(_raw())
        store = DeliveryStore()
        delivered = store.deliver(facts, date(2026, 11, 29))
        again = refetch(store, facts, date(2026, 11, 29), delivered.version)
        self.assertEqual(
            json.dumps(again.as_json(), sort_keys=True),
            json.dumps(delivered.as_json(), sort_keys=True),
        )

    def test_refetch_detects_data_drift(self):
        facts = parse_facts(_raw())
        store = DeliveryStore()
        delivered = store.deliver(facts, date(2026, 11, 29))
        changed = parse_facts(
            _raw(
                paths=[
                    {
                        "id": "W",
                        "from": "P2",
                        "to": "P2",
                        "walk_minutes": 12,
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-12-31",
                    }
                ]
            )
        )
        with self.assertRaises(RefetchMismatch):
            refetch(store, changed, date(2026, 11, 29), delivered.version)

    def test_audit_returns_delivered_and_current(self):
        facts = parse_facts(_raw())
        store = DeliveryStore()
        delivered = store.deliver(facts, date(2026, 11, 29))
        result = audit(store, facts, date(2026, 11, 29), version=delivered.version)
        self.assertEqual(result["delivered_version"], delivered.version)
        self.assertEqual(result["current_version"], delivered.version)
        self.assertEqual(result["changes"], [])

    def test_compare_explains_path_change(self):
        before_facts = parse_facts(_raw())
        before = build_day(before_facts, date(2026, 11, 29))
        after_facts = parse_facts(
            _raw(
                paths=[
                    {
                        "id": "W",
                        "from": "P2",
                        "to": "P2",
                        "walk_minutes": 5,
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-11-28",
                    }
                ]
            )
        )
        after = build_day(after_facts, date(2026, 11, 29))
        self.assertEqual(before.connections[0].state, "available")
        self.assertEqual(after.connections[0].state, "unavailable")
        changes = compare(before, after)
        self.assertEqual(len(changes), 1)
        self.assertIn("path", changes[0].causes)
        self.assertEqual(changes[0].before_state, "available")
        self.assertEqual(changes[0].after_state, "unavailable")

    def test_compare_explains_confirmation_change(self):
        before_facts = parse_facts(
            _raw(
                connections=[
                    {
                        "from_trip": "A-1",
                        "to_trip": "B-1",
                        "transfer_minutes": 10,
                        "state": "available",
                    }
                ]
            )
        )
        after_facts = parse_facts(
            _raw(
                connections=[
                    {
                        "from_trip": "A-1",
                        "to_trip": "B-1",
                        "transfer_minutes": 10,
                        "state": "pending",
                        "reason": "接续站停靠未确认",
                    }
                ]
            )
        )
        before = build_day(before_facts, date(2026, 11, 29))
        after = build_day(after_facts, date(2026, 11, 29))
        changes = compare(before, after)
        self.assertIn("confirmation", changes[0].causes)

    def test_compare_explains_stop_change(self):
        before_facts = parse_facts(_raw())
        raw = _raw(
            instructions=[
                {
                    "id": "I-1",
                    "trip": "A-1",
                    "action": "replace_stop",
                    "stop": "A2",
                    "new_stop": "A1",
                    "effective": "2026-11-29",
                }
            ]
        )
        after_facts = parse_facts(raw)
        before = build_day(before_facts, date(2026, 11, 29))
        after = build_day(after_facts, date(2026, 11, 29))
        changes = compare(before, after)
        self.assertEqual(len(changes), 1)
        self.assertIn("stop", changes[0].causes)
        self.assertEqual(changes[0].after_state, "unavailable")  # P1 -> P2 无通道

    def test_versions_are_recorded_in_order(self):
        facts = parse_facts(_raw())
        store = DeliveryStore()
        first = store.deliver(facts, date(2026, 11, 29))
        changed = parse_facts(
            _raw(
                paths=[
                    {
                        "id": "W",
                        "from": "P2",
                        "to": "P2",
                        "walk_minutes": 12,
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-12-31",
                    }
                ]
            )
        )
        second = store.deliver(changed, date(2026, 11, 29))
        self.assertNotEqual(first.version, second.version)
        self.assertEqual(
            store.versions(date(2026, 11, 29)), [first.version, second.version]
        )
        self.assertEqual(store.latest(date(2026, 11, 29)).version, second.version)
        self.assertEqual(
            store.get(date(2026, 11, 29), first.version).version, first.version
        )

    def test_delivered_payload_is_json_serializable(self):
        facts = parse_facts(_raw())
        store = DeliveryStore()
        delivered = store.deliver(facts, date(2026, 11, 29))
        text = json.dumps(delivered.as_json(), ensure_ascii=False)
        self.assertIn("service_day", text)
        self.assertIn("evidence", text)

    def test_mutating_returned_payload_does_not_change_store(self):
        facts = parse_facts(_raw())
        store = DeliveryStore()
        delivered = store.deliver(facts, date(2026, 11, 29))
        copy = delivered.as_json()
        copy["stops"].clear()
        self.assertEqual(
            len(store.get(date(2026, 11, 29)).as_json()["stops"]),
            len(delivered.as_json()["stops"]),
        )


if __name__ == "__main__":
    unittest.main()
