"""运营日归属、跨零点与本地时间歧义的测试。"""
import unittest
from datetime import UTC, date, datetime, time

from serviceday.model import Call, ServiceCalendar, TimeAmbiguityError, Trip
from serviceday.service_day import call_times, resolve_local, service_day_of


def _calendar(operator="A", timezone="Asia/Shanghai", start="03:00"):
    return ServiceCalendar(operator=operator, timezone=timezone, service_day_starts=start)


class ServiceDayTests(unittest.TestCase):
    def test_after_midnight_call_stays_on_previous_service_day(self):
        calendar = _calendar()
        trip = Trip(
            id="A-1",
            operator="A",
            service_date=date(2026, 11, 29),
            calls=(
                Call("OLD", "23:45", "23:50"),
                Call("NEW", "00:35", "00:40", next_day=True),
            ),
        )
        rows = call_times(trip, calendar)
        self.assertEqual(rows[0][1], datetime.fromisoformat("2026-11-29T23:45:00+08:00"))
        self.assertEqual(rows[1][1], datetime.fromisoformat("2026-11-30T00:35:00+08:00"))
        self.assertEqual(service_day_of(rows[1][1], calendar), date(2026, 11, 29))

    def test_each_operator_uses_own_day_boundary(self):
        a = _calendar("A", start="03:00")
        b = _calendar("B", start="04:00")
        moment = datetime.fromisoformat("2026-11-30T02:30:00+08:00")
        self.assertEqual(service_day_of(moment, a), date(2026, 11, 29))
        self.assertEqual(service_day_of(moment, b), date(2026, 11, 29))
        moment = datetime.fromisoformat("2026-11-30T03:30:00+08:00")
        self.assertEqual(service_day_of(moment, a), date(2026, 11, 30))
        self.assertEqual(service_day_of(moment, b), date(2026, 11, 29))

    def test_call_time_is_localized(self):
        calendar = _calendar()
        moment = resolve_local(date(2026, 11, 29), time(23, 45), calendar)
        self.assertEqual(moment.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(moment.hour, 23)

    def test_duplicate_local_time_is_isolated(self):
        # 美国东部 2026-11-01 01:30 在回拨时出现两次。运营日 03:00 起算，
        # 该时刻落在运营日 2026-10-31 的后半夜。
        calendar = _calendar("US", "America/New_York", "03:00")
        with self.assertRaises(TimeAmbiguityError):
            resolve_local(date(2026, 10, 31), time(1, 30), calendar)
        # 显式给出 fold 时按资料选择第一次或第二次出现。
        first = resolve_local(date(2026, 10, 31), time(1, 30), calendar, fold=0)
        second = resolve_local(date(2026, 10, 31), time(1, 30), calendar, fold=1)
        self.assertNotEqual(first.utcoffset(), second.utcoffset())
        self.assertLess(first.astimezone(UTC), second.astimezone(UTC))

    def test_nonexistent_local_time_is_isolated(self):
        # 2026-03-08 02:30 在前拨时不存在，落在运营日 2026-03-07 的后半夜。
        calendar = _calendar("US", "America/New_York", "03:00")
        with self.assertRaises(TimeAmbiguityError):
            resolve_local(date(2026, 3, 7), time(2, 30), calendar)

    def test_trip_with_ambiguous_call_raises(self):
        calendar = _calendar("US", "America/New_York", "03:00")
        trip = Trip(
            id="US-1",
            operator="US",
            service_date=date(2026, 10, 31),
            calls=(Call("S1", "01:30", "01:35"),),
        )
        with self.assertRaises(TimeAmbiguityError):
            call_times(trip, calendar)

    def test_fold_in_call_resolves_duplicate(self):
        calendar = _calendar("US", "America/New_York", "03:00")
        trip = Trip(
            id="US-1",
            operator="US",
            service_date=date(2026, 10, 31),
            calls=(Call("S1", "01:30", "01:35", fold=1),),
        )
        rows = call_times(trip, calendar)
        self.assertEqual(rows[0][1].utcoffset().total_seconds(), -5 * 3600)


if __name__ == "__main__":
    unittest.main()
