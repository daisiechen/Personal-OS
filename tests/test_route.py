"""今天的路线串起来（一点十分出发去 Sunday Roast，两点十分为什么还有一个出门？到家比训练结束还早，乱七八糟）。"""

import unittest

from ethan import route

PLACES = {"家": {"postcode": "NW1 1AA"}, "健身房": {"postcode": "AB1 2CD"}}


def mins(table):
    def f(a, b, arrive):
        key = (a if isinstance(a, str) else a.get("postcode") or a.get("name"), b if isinstance(b, str) else b.get("postcode") or b.get("name"))
        return table.get(key, 30)
    return f


class RouteTest(unittest.TestCase):
    def test_one_trip_out_then_conflict_then_home(self):
        busy = [("15:00", "17:00", "Sunday Roast", {"location": "Denman St W1D 7HN London"})]
        items = [{"kind": "event", "start": "17:00", "end": "18:15", "title": "训练：上肢", "place": "健身房"}]
        st = route.stops(busy, items, PLACES)
        out = route.plan(st, mins({("家", "W1D 7HN"): 30, ("W1D 7HN", "健身房"): 25, ("健身房", "家"): 20}))
        texts = [(r["at"], r["text"]) for r in out["rows"]]
        self.assertEqual(sum("出门" in t for _, t in texts), 1)  # 只从家出一次门
        self.assertIn(("14:10", "出门 → Sunday Roast（路上约 30 分钟）"), texts)
        # 17:00 吃完赶 17:00 的训练赶不上：吃饭能早走，提醒几点吃完就走
        self.assertIn(("16:25", "「Sunday Roast」16:25 吃完就走，直接去「训练：上肢」（路上约 25 分钟）"), texts)
        self.assertEqual(texts[-1], ("18:35", "到家"))  # 最后一站结束后才到家
        self.assertTrue(out["conflicts"])

    def test_goes_home_between_when_gap_is_long(self):
        busy = [("12:00", "13:00", "午饭", {"location": "W1D 7HN"})]
        items = [{"kind": "event", "start": "18:00", "end": "19:15", "title": "训练", "place": "健身房"}]
        out = route.plan(route.stops(busy, items, PLACES), mins({}))
        texts = [r["text"] for r in out["rows"]]
        self.assertEqual(texts.count("到家"), 2)  # 午饭后回家，训练后再回家
        self.assertEqual(sum(t.startswith("出门") for t in texts), 2)
        self.assertEqual(out["conflicts"], [])

    def test_goes_direct_when_gap_is_short(self):
        busy = [("15:00", "16:30", "Sunday Roast", {"location": "W1D 7HN"})]
        items = [{"kind": "event", "start": "17:30", "end": "18:45", "title": "训练", "place": "健身房"}]
        out = route.plan(route.stops(busy, items, PLACES), mins({}))
        texts = [r["text"] for r in out["rows"]]
        self.assertTrue(any("直接去 训练" in t for t in texts))
        self.assertEqual(texts.count("到家"), 1)


    def test_fixed_meeting_moves_training(self):
        busy = [("15:00", "17:00", "小组会", {"location": "W1D 7HN"})]
        items = [{"kind": "event", "start": "17:00", "end": "18:15", "title": "训练", "place": "健身房"}]
        out = route.plan(route.stops(busy, items, PLACES), mins({}))
        self.assertTrue(any("建议「训练」挪到 17:45" in c for c in out["conflicts"]))  # 会改不了：训练往后挪


if __name__ == "__main__":
    unittest.main()
