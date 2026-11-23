"""临时指令：增开、取消、单停靠修改与取代链。"""
import unittest
from datetime import date, datetime

from serviceday.instructions import InstructionConflict, effective_trip
from serviceday.model import Call, Instruction, ServiceCalendar, Trip

CAL = ServiceCalendar("A", "Asia/Shanghai", "03:00")


def _trip(trip_id="A-1"):
    return Trip(
        id=trip_id,
        operator="A",
        service_date=date(2026, 11, 29),
        calls=(
            Call("S1", "10:00", "10:05"),
            Call("S2", "11:00", "11:05"),
        ),
    )


def _instruction(**kwargs):
    base = {
        "id": "I",
        "trip": "A-1",
        "action": "cancel_call",
        "effective": date(2026, 11, 29),
    }
    base.update(kwargs)
    return Instruction(**base)


class InstructionTests(unittest.TestCase):
    def test_cancel_single_call_keeps_rest(self):
        result = effective_trip(
            _trip(), [_instruction(stop="S1")], CAL, date(2026, 11, 29)
        )
        self.assertEqual([c.stop for c in result.calls], ["S2"])

    def test_replace_stop_keeps_ticket_origin(self):
        result = effective_trip(
            _trip(),
            [_instruction(action="replace_stop", stop="S1", new_stop="S9")],
            CAL,
            date(2026, 11, 29),
        )
        self.assertEqual([c.stop for c in result.calls], ["S9", "S2"])
        self.assertEqual(result.origin_codes, ("S1", "S2"))

    def test_retime_only_touches_named_call(self):
        result = effective_trip(
            _trip(),
            [
                _instruction(
                    action="retime",
                    stop="S2",
                    arrival="11:30",
                    departure="11:35",
                )
            ],
            CAL,
            date(2026, 11, 29),
        )
        self.assertEqual(result.calls[0].arrival, "10:00")
        self.assertEqual(result.calls[1].arrival, "11:30")

    def test_add_call_appends_stop(self):
        result = effective_trip(
            _trip(),
            [
                _instruction(
                    action="add_call",
                    new_stop="S8",
                    arrival="12:00",
                    departure="12:05",
                )
            ],
            CAL,
            date(2026, 11, 29),
        )
        self.assertEqual([c.stop for c in result.calls], ["S1", "S2", "S8"])
        self.assertEqual(result.origin_codes[-1], None)

    def test_cancel_service_removes_whole_trip(self):
        result = effective_trip(
            _trip(),
            [_instruction(action="cancel_service", stop=None)],
            CAL,
            date(2026, 11, 29),
        )
        self.assertTrue(result.cancelled)

    def test_revision_supersede_does_not_restore_old_change(self):
        trip = _trip()
        base = {
            "trip": "A-1",
            "action": "replace_stop",
            "stop": "S1",
            "new_stop": "S9",
            "effective": date(2026, 11, 29),
        }
        old = Instruction(id="I-1", revision=1, **base)
        late = Instruction(
            id="I-2",
            revision=2,
            received_at=datetime.fromisoformat("2026-11-30T02:00:00+08:00"),
            supersedes=("I-1",),
            **base,
        )
        result = effective_trip(trip, [old, late], CAL, date(2026, 11, 29))
        self.assertEqual([c.stop for c in result.calls], ["S9", "S2"])
        self.assertEqual([c.instruction_id for c in result.applied], ["I-2"])

    def test_later_instruction_wins_without_explicit_supersede(self):
        trip = _trip()
        first = Instruction(
            id="I-1",
            trip="A-1",
            action="replace_stop",
            stop="S1",
            new_stop="S9",
            effective=date(2026, 11, 29),
            received_at=datetime.fromisoformat("2026-11-28T10:00:00+08:00"),
        )
        second = Instruction(
            id="I-2",
            trip="A-1",
            action="replace_stop",
            stop="S1",
            new_stop="S7",
            effective=date(2026, 11, 29),
            received_at=datetime.fromisoformat("2026-11-29T10:00:00+08:00"),
        )
        result = effective_trip(trip, [first, second], CAL, date(2026, 11, 29))
        self.assertEqual([c.stop for c in result.calls], ["S7", "S2"])

    def test_instruction_only_applies_on_its_date(self):
        result = effective_trip(
            _trip(),
            [_instruction(stop="S1", effective=date(2026, 11, 30))],
            CAL,
            date(2026, 11, 29),
        )
        self.assertEqual([c.stop for c in result.calls], ["S1", "S2"])

    def test_time_range_effective(self):
        window = (
            datetime.fromisoformat("2026-11-29T23:00:00+08:00"),
            datetime.fromisoformat("2026-11-30T01:00:00+08:00"),
        )
        result = effective_trip(
            _trip(), [_instruction(stop="S1", effective=window)], CAL, date(2026, 11, 29)
        )
        self.assertEqual([c.stop for c in result.calls], ["S2"])

    def test_conflicting_instructions_are_isolated(self):
        trip = _trip()
        same_rev_first = Instruction(
            id="I-1",
            trip="A-1",
            action="replace_stop",
            stop="S1",
            new_stop="S9",
            effective=date(2026, 11, 29),
        )
        same_rev_second = Instruction(
            id="I-2",
            trip="A-1",
            action="replace_stop",
            stop="S1",
            new_stop="S7",
            effective=date(2026, 11, 29),
        )
        with self.assertRaises(InstructionConflict):
            effective_trip(trip, [same_rev_first, same_rev_second], CAL, date(2026, 11, 29))

    def test_replacing_an_existing_stop_merges_without_duplicate(self):
        result = effective_trip(
            _trip(),
            [_instruction(action="replace_stop", stop="S1", new_stop="S2")],
            CAL,
            date(2026, 11, 29),
        )
        self.assertEqual([c.stop for c in result.calls], ["S2"])
        self.assertEqual(result.origin_codes, ("S1",))

    def test_unconfirmed_instruction_marks_change(self):
        result = effective_trip(
            _trip(),
            [
                _instruction(
                    action="replace_stop",
                    stop="S1",
                    new_stop="S9",
                    confirmed=False,
                )
            ],
            CAL,
            date(2026, 11, 29),
        )
        self.assertTrue(result.unconfirmed_change)


if __name__ == "__main__":
    unittest.main()
