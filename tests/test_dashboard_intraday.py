# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import unittest

import numpy as np

from spikes.dashboard import intraday as I


TODAY = datetime.date(2026, 9, 2)
# a day-time heavy profile: share of hour h
SHAPE = np.array([2, 2, 2, 2, 2, 3, 4, 5, 5, 5, 5, 5, 6, 6, 6, 6, 5, 5,
                  5, 4, 4, 3, 2, 2], dtype=float)
SHAPE = SHAPE / SHAPE.sum()


def make_rows(ndays=28, total=20000, seed=0, bursts=()):
    rng = np.random.default_rng(seed)
    rows = {}
    for i in range(1, ndays + 1):
        day = TODAY - datetime.timedelta(days=i)
        hourly = rng.poisson(SHAPE * total)
        if i in bursts:
            hourly[13] += total
        rows[day] = [int(x) for x in hourly]
    return rows


class ProfileTest(unittest.TestCase):

    def test_profile_matches_shape(self):
        p = I.build_profile(make_rows(), TODAY)
        self.assertEqual(p.ndays, 28)
        f, v = p.fraction(0, 12.0)
        self.assertAlmostEqual(f, SHAPE[:12].sum(), places=2)
        self.assertLess(v, 0.01)
        self.assertEqual(p.fraction(0, 0.0)[0], 0.0)
        self.assertEqual(p.fraction(0, 24.0)[0], 1.0)
        # interpolation inside an hour
        f_half = p.fraction(0, 12.5)[0]
        self.assertGreater(f_half, f)
        self.assertLess(f_half, p.fraction(0, 13.0)[0])
        exp = p.hourly_expected(0, 1000.0)
        self.assertEqual(len(exp), 24)
        self.assertAlmostEqual(sum(exp), 1000.0)
        self.assertEqual(len(p.f_weekday), 7)

    def test_burst_days_are_excluded(self):
        clean = I.build_profile(make_rows(), TODAY)
        bursty = I.build_profile(make_rows(bursts=(2, 5, 9)), TODAY)
        self.assertEqual(bursty.ndays, 25)
        self.assertAlmostEqual(bursty.fraction(0, 14.0)[0],
                               clean.fraction(0, 14.0)[0], places=2)

    def test_not_enough_days(self):
        rows = make_rows(ndays=2)
        self.assertIsNone(I.build_profile(rows, TODAY))
        # days too small are ignored
        rows = make_rows(total=10)
        self.assertIsNone(I.build_profile(rows, TODAY, min_total=50))

    def test_window_within_day(self):
        today = [10] * 24
        yesterday = [100] * 24
        as_of = datetime.datetime(2026, 9, 2, 13, 20)
        obs, start, end = I.window(today, yesterday, as_of, 3)
        # 10:20 -> 13:20: 2/3 of hour 10, hours 11, 12 and the current 13
        self.assertAlmostEqual(obs, 10 * 2 / 3 + 30)
        self.assertAlmostEqual(start, 24 + 10 + 1 / 3)
        self.assertAlmostEqual(end, 24 + 13 + 1 / 3)

    def test_window_across_midnight(self):
        today = [10] * 24
        yesterday = [100] * 24
        as_of = datetime.datetime(2026, 9, 2, 1, 30)
        obs, start, end = I.window(today, yesterday, as_of, 3)
        # 22:30 -> 01:30: half of 22, hour 23, hours 0 and 1 of today
        self.assertAlmostEqual(obs, 50 + 100 + 20)
        self.assertAlmostEqual(start, 22.5)
        self.assertIsNone(I.window(None, yesterday, as_of, 3))

    def test_window_expected(self):
        p = I.build_profile(make_rows(), TODAY)
        as_of = datetime.datetime(2026, 9, 2, 1, 30)
        _, start, end = I.window([0] * 24, [0] * 24, as_of, 3)
        e, var = I.window_expected(p, 1000.0, 1000.0, TODAY, start, end)
        f = p.fraction(TODAY.weekday(), 1.5)[0]
        yf = p.fraction((TODAY - datetime.timedelta(1)).weekday(), 22.5)[0]
        self.assertAlmostEqual(e, 1000.0 * f + 1000.0 * (1 - yf))
        self.assertGreaterEqual(var, 0.0)


if __name__ == '__main__':
    unittest.main()
