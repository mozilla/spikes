# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import unittest

import numpy as np

from spikes.dashboard import seasonal as S


WEEKLY = np.array([1.25, 1.2, 1.15, 1.1, 1.0, 0.7, 0.6])
WEEKLY = WEEKLY / WEEKLY.mean()
START = datetime.date(2026, 3, 2)  # a Monday


def simulate(ndays=180, base=1000.0, r=50, seed=0, cycle=None, trend=0.0):
    rng = np.random.default_rng(seed)
    dates = [START + datetime.timedelta(days=i) for i in range(ndays)]
    level = base * (1 + trend * np.arange(ndays))
    mu = level * WEEKLY[[d.weekday() for d in dates]]
    if cycle is not None:
        mu = mu * cycle[S.cycle_phase(dates)]
    y = rng.negative_binomial(r, r / (r + mu)).astype(float)
    return dates, y


class HelpersTest(unittest.TestCase):

    def test_anscombe_roundtrip(self):
        e = np.array([0.0, 1.0, 10.0, 1000.0])
        a = S.anscombe(np.array([3.0, 4.0, 20.0, 1200.0]), e)
        back = S.anscombe_inverse(e, a)
        np.testing.assert_allclose(back, [3.0, 4.0, 20.0, 1200.0])

    def test_rolling_median(self):
        x = np.array([1.0, 2, 3, 4, 5, 6, 7])
        np.testing.assert_allclose(S.rolling_median(x, 3),
                                   [1.5, 2, 3, 4, 5, 6, 6.5])
        trailing = S.rolling_median(x, 3, center=False)
        self.assertTrue(np.isnan(trailing[0]))
        np.testing.assert_allclose(trailing[1:], [1, 1.5, 2, 3, 4, 5])

    def test_rolling_level_follows_trend(self):
        x = 100.0 + 5.0 * np.arange(30)
        level, slope = S.rolling_level(x, 14, trend_min_level=50)
        self.assertEqual(level.size, 31)
        self.assertAlmostEqual(level[30], 250.0, places=6)
        self.assertAlmostEqual(slope[30], 5.0, places=6)
        # below the trend threshold the slope is ignored
        level, slope = S.rolling_level(x / 10, 14, trend_min_level=50)
        self.assertEqual(slope[30], 0.0)
        self.assertAlmostEqual(level[30], np.median(x[-14:] / 10))

    def test_rolling_level_robust_to_outliers(self):
        x = np.full(30, 100.0)
        x[-3:] = 1000.0  # a 3-day spike must not move the level much
        level, _ = S.rolling_level(x, 14, trend_min_level=50)
        self.assertLess(level[30], 130.0)

    def test_score_small_counts(self):
        self.assertAlmostEqual(S.score(1, 1.0, 0.0), 0.13, places=1)
        self.assertGreater(S.score(7, 1.0, 0.0), 3.5)
        self.assertLess(S.score(0, 5.0, 0.0), -2.0)
        self.assertGreater(S.score(20, 0.0, 0.0), 8.0)
        self.assertIsNone(S.score(3, None, 0.0))

    def test_score_scales_with_dispersion(self):
        z0 = S.score(1500, 1000.0, 0.0)
        z1 = S.score(1500, 1000.0, 0.02)
        self.assertGreater(z0, z1)
        self.assertGreater(z1, 3.0)

    def test_band(self):
        lo, hi = S.band(np.array([100.0]), 3, 0.0)
        self.assertLess(lo[0], 100.0)
        self.assertGreater(hi[0], 100.0)
        # z of the band edge is +-3
        self.assertAlmostEqual(S.score(hi[0], 100.0, 0.0), 3.0, places=5)

    def test_constrain_cycle(self):
        f = np.ones(28)
        f[::7] = 2.0  # a pure Monday effect
        c = S._constrain_cycle(f)
        np.testing.assert_allclose(c, np.ones(28))


class FitTest(unittest.TestCase):

    def test_recovers_weekly_pattern(self):
        dates, y = simulate()
        f = S.fit(dates, y)
        self.assertTrue(f.active['weekly'])
        self.assertTrue(f.active['cycle'])
        self.assertFalse(f.active['yearly'])
        np.testing.assert_allclose(f.factors['weekly'], WEEKLY, atol=0.08)
        # no real 28-day cycle in the data: the factors stay near 1
        self.assertLess(f.factors['cycle'].std(), 0.06)
        self.assertGreater(f.dispersion, 1.5)
        self.assertGreater(f.c2, 0.005)

    def test_monday_is_not_a_spike(self):
        dates, y = simulate()
        f = S.fit(dates, y)
        monday = dates[-1] + datetime.timedelta(days=1)
        while monday.weekday() != 0:
            monday += datetime.timedelta(days=1)
        horizon = (monday - dates[-1]).days
        e = f.forecast(monday, horizon=horizon)
        normal = 1000.0 * WEEKLY[0]
        self.assertLess(abs(f.score(normal, e)), 1.5)
        self.assertGreater(f.score(1.6 * normal, e), 3.0)
        self.assertGreater(f.score(2.0 * normal, e), 5.0)
        self.assertLess(f.score(0.5 * normal, e), -4.0)

    def test_recovers_cycle(self):
        cycle = np.ones(28)
        cycle[:7] = 1.3
        cycle[21:] = 0.8
        cycle /= cycle.mean()
        dates, y = simulate(cycle=cycle)
        f = S.fit(dates, y)
        self.assertGreater(f.factors['cycle'][:7].mean(), 1.15)
        self.assertLess(f.factors['cycle'][21:].mean(), 0.9)

    def test_follows_trend(self):
        dates, y = simulate(trend=0.01)
        f = S.fit(dates, y)
        true_level = 1000.0 * (1 + 0.01 * len(dates))
        self.assertLess(abs(f.next_level - true_level) / true_level, 0.1)

    def test_prior_is_borrowed_by_low_volume_series(self):
        dates, y = simulate()
        prior = S.fit(dates, y)
        _, small = simulate(ndays=35, base=8.0, r=20, seed=3)
        f = S.fit(dates[-35:], small, prior=prior)
        self.assertIn('weekly', f.borrowed)
        self.assertIn('cycle', f.borrowed)
        np.testing.assert_allclose(f.factors['weekly'],
                                   prior.factors['weekly'])
        # low-volume series without a prior: only 5 weeks, cycle inactive
        g = S.fit(dates[-35:], small)
        self.assertTrue(g.active['weekly'])
        self.assertFalse(g.active['cycle'])

    def test_high_volume_series_keeps_own_pattern(self):
        dates, y = simulate()
        prior = S.fit(dates, y)
        rng = np.random.default_rng(5)
        flat = rng.negative_binomial(50, 50 / 350.0, size=90).astype(float)
        f = S.fit(dates[-90:], flat, prior=prior)
        self.assertNotIn('weekly', f.borrowed)
        # own (flat) pattern dominates the channel's strong weekly pattern
        self.assertLess(f.factors['weekly'].max() - 1, 0.12)

    def test_edge_cases(self):
        dates, y = simulate()
        f0 = S.fit(dates, np.zeros(len(dates)))
        self.assertEqual(f0.next_level, 0.0)
        self.assertGreater(f0.score(20, f0.forecast(dates[-1]), 0), 8)
        f1 = S.fit(dates[:5], y[:5])
        self.assertFalse(any(f1.active.values()))
        self.assertGreater(f1.next_level, 0)
        y2 = y.copy()
        y2[50:60] = np.nan
        f2 = S.fit(dates, y2)
        self.assertTrue(np.isfinite(f2.next_level))
        f3 = S.fit([], [])
        self.assertEqual(f3.ndays, 0)

    def test_make_series_and_weekly_aggregation(self):
        start = datetime.date(2026, 8, 31)  # Monday
        rows = {start + datetime.timedelta(days=i): 10 for i in range(10)}
        del rows[start + datetime.timedelta(days=3)]
        dates, y = S.make_series(rows, start, start + datetime.timedelta(9))
        self.assertEqual(len(dates), 10)
        self.assertTrue(np.isnan(y[3]))
        agg = S.aggregate_weekly(dates, y, np.full(10, 10.0), 0.0)
        self.assertEqual(len(agg), 2)
        self.assertEqual(agg[0]['ndays'], 7)
        self.assertEqual(agg[0]['observed'], 60.0)
        # the unknown day is left out of the expectation as well
        self.assertEqual(agg[0]['expected'], 60.0)
        self.assertAlmostEqual(agg[0]['z'], 0.0)
        empty = S.aggregate_weekly(dates[:7], [np.nan] * 7,
                                   np.full(7, 10.0), 0.0)
        self.assertIsNone(empty[0]['observed'])
        self.assertIsNone(empty[0]['lo3'])
        self.assertLess(agg[0]['lo3'], 70.0)
        self.assertGreater(agg[0]['hi3'], 70.0)
        self.assertEqual(agg[1]['ndays'], 3)

    def test_summary(self):
        dates, y = simulate()
        s = S.fit(dates, y).summary()
        self.assertEqual(s['history_days'], 180)
        self.assertIn('weekly', s['factors'])
        self.assertEqual(len(s['factors']['weekly']), 7)
        self.assertFalse(s['components']['yearly']['active'])


class ForecastTest(unittest.TestCase):

    def test_damped_trend(self):
        # a 1 %/day ramp: the forecast follows it, less and less with damping
        dates, y = simulate(ndays=90, trend=0.01, r=500)
        fit = S.fit(dates, y)
        self.assertGreater(fit.next_slope, 0)
        far = dates[-1] + datetime.timedelta(days=14)
        plain = fit.forecast(far, 14) / fit.seasonal_at(far)
        damped = fit.forecast(far, 14, 0.8) / fit.seasonal_at(far)
        tomorrow = fit.forecast(dates[-1] + datetime.timedelta(days=1))
        tomorrow /= fit.seasonal_at(dates[-1] + datetime.timedelta(days=1))
        self.assertGreater(plain, damped)
        self.assertGreater(damped, tomorrow)
        # 13 damped steps: (1 - 0.8^13) / (1 - 0.8) of them, converging to 5
        self.assertAlmostEqual(damped - tomorrow,
                               fit.next_slope * (1 - 0.8 ** 13) / 0.2,
                               places=6)
        # horizon 1 is unchanged by damping
        self.assertEqual(fit.forecast(far, 1, 0.8), fit.forecast(far, 1))

    def test_weekly_forecast_weeks(self):
        dates = [START + datetime.timedelta(days=i) for i in range(21)]
        observed = [100.0] * 10 + [None] * 11  # the last 11 days lie ahead
        expected = [100.0] * 21
        today = dates[9]
        agg = S.aggregate_weekly(dates, observed, expected, 0.02,
                                 forecast_after=today)
        self.assertEqual([a['future'] for a in agg], [False, False, True])
        # without a forecast boundary a week without data is a gap
        gap = S.aggregate_weekly(dates, observed, expected, 0.02)
        self.assertFalse(gap[2]['future'])
        self.assertIsNone(gap[2]['expected'])
        # the week in progress: observed and expected over its known days
        self.assertEqual(agg[1]['observed'], 300.0)
        self.assertEqual(agg[1]['expected'], 300.0)
        # the forecast week: no observed, the expectation of its 7 days
        self.assertIsNone(agg[2]['observed'])
        self.assertEqual(agg[2]['expected'], 700.0)
        self.assertIsNone(agg[2]['z'])
        self.assertLess(agg[2]['lo3'], 700.0)
        self.assertGreater(agg[2]['hi5'], 700.0)


if __name__ == '__main__':
    unittest.main()
