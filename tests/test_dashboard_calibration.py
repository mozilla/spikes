# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Thresholds learned from the data (calibration.py)."""

import unittest

import numpy as np

from spikes.dashboard import calibration as C


def normal_hists(nseries, ndays, scale=1.0, seed=0, tail=None):
    """Histograms of Gaussian z (scale *scale*), with an optional heavy
    tail: *tail* = (share, value) puts that share of the days at *value*."""
    rng = np.random.default_rng(seed)
    hists = []
    for _ in range(nseries):
        z = rng.normal(0.0, scale, ndays)
        if tail:
            k = int(round(tail[0] * ndays))
            z[:k] = tail[1]
        hists.append(C.histogram(z, np.full(ndays, 100.0)))
    return hists


class HistogramTest(unittest.TestCase):

    def test_bins_and_overflow(self):
        h = C.histogram([-30.0, -25.0, 0.0, 0.2, 49.9, 60.0, np.nan],
                        [10, 10, 10, 10, 10, 10, 10])
        self.assertEqual(len(h), C.NBINS + 2)
        self.assertEqual(sum(h), 6)          # NaN dropped
        self.assertEqual(h[0], 1)            # -30 below the range
        self.assertEqual(h[-1], 1)           # 60 above it
        self.assertEqual(h[1], 1)            # -25 in the first regular bin
        # tiny expectations are left out: their z is not informative
        self.assertEqual(sum(C.histogram([5.0, 5.0], [0.5, 2.0])), 1)

    def test_quantile_and_share(self):
        rng = np.random.default_rng(1)
        z = rng.normal(0, 1, 200000)
        hist = C.merge([C.histogram(z, np.full(z.size, 50.0))])
        self.assertAlmostEqual(C.quantile(hist, 0.5), 0.0, delta=0.1)
        self.assertAlmostEqual(C.quantile(hist, 0.975), 1.96, delta=0.15)
        self.assertAlmostEqual(C.quantile(hist, 0.025), -1.96, delta=0.15)
        self.assertAlmostEqual(C.share_above(hist, 2.0), 0.0228, delta=0.005)
        self.assertIsNone(C.quantile(C.merge([]), 0.5))


class CalibrateTest(unittest.TestCase):

    def test_gaussian_without_data(self):
        c = C.calibrate([])
        self.assertEqual(c['sample'], 0)
        self.assertEqual(set(c['method'].values()), {'gaussian'})
        self.assertAlmostEqual(c['rules']['watch']['z'], 2.17, places=2)
        self.assertAlmostEqual(c['rules']['spike']['z'], 2.97, places=2)
        self.assertAlmostEqual(c['rules']['major']['z'], 3.62, places=2)
        self.assertAlmostEqual(c['rules']['drop']['z'], -2.97, places=2)
        # too small a sample: Gaussian as well
        c = C.calibrate(normal_hists(2, 100))
        self.assertEqual(c['sample'], 200)
        self.assertEqual(c['method']['watch'], 'gaussian')

    def test_gaussian_data_stays_at_the_floor(self):
        c = C.calibrate(normal_hists(200, 180))
        self.assertEqual(c['sample'], 36000)
        self.assertEqual(c['method']['watch'], 'empirical')
        # the empirical quantile of a Gaussian sample is the Gaussian value
        # (never below it); the ordering of the levels is kept
        self.assertAlmostEqual(c['rules']['watch']['z'], 2.17, delta=0.2)
        self.assertGreaterEqual(c['rules']['watch']['z'], 2.17)
        self.assertGreaterEqual(c['rules']['spike']['z'],
                                c['rules']['watch']['z'] + 0.5)
        self.assertGreaterEqual(c['rules']['major']['z'],
                                c['rules']['spike']['z'] + 0.5)
        self.assertLessEqual(c['rules']['drop']['z'], -2.97)
        self.assertAlmostEqual(c['tail']['watch'], 0.015, delta=0.005)

    def test_heavy_tail_raises_the_bar(self):
        # 3 % of the days at z = 6 (bursty channel): watch must move above
        # them, since 3 % > the 1.5 % rate; a tight channel keeps 2.2
        loud = C.calibrate(normal_hists(200, 180, tail=(0.03, 6.0)))
        tight = C.calibrate(normal_hists(200, 180, scale=0.8))
        self.assertGreater(loud['rules']['watch']['z'], 5.5)
        self.assertLess(tight['rules']['watch']['z'], 2.5)
        self.assertGreater(loud['rules']['spike']['z'],
                           loud['rules']['watch']['z'])

    def test_extrapolated_tail(self):
        # 2000 series-days: the major rate (0.015 %) has 0.3 points beyond
        # it, so the tail is extrapolated from the top of the sample
        c = C.calibrate(normal_hists(20, 100, seed=3))
        self.assertEqual(c['method']['watch'], 'empirical')
        self.assertEqual(c['method']['major'], 'extrapolated')
        self.assertGreaterEqual(c['rules']['major']['z'], 3.62)
        self.assertLess(c['rules']['major']['z'], 8.0)

    def test_rates_override(self):
        c = C.calibrate(normal_hists(200, 180), {'watch': 0.05})
        self.assertAlmostEqual(c['rules']['watch']['z'], 1.64, delta=0.2)
        self.assertEqual(c['rates']['spike'], 0.0015)


class FloorsTest(unittest.TestCase):

    def test_volume_floors(self):
        self.assertEqual(C.volume_floors(20860.0, 0.001), (21, 10))
        self.assertEqual(C.volume_floors(28840.0, 0.001), (29, 14))
        self.assertEqual(C.volume_floors(973.0, 0.001), (2, 2))
        self.assertEqual(C.volume_floors(2.1, 0.001), (2, 2))
        self.assertEqual(C.volume_floors(None, 0.001), (2, 2))

    def test_storm_ratio(self):
        rng = np.random.default_rng(0)
        daily = {}
        for sid in range(30):
            rows = {}
            for d in range(28):
                crashes = int(rng.integers(20, 200))
                installs = max(1, int(crashes / rng.uniform(1.0, 1.6)))
                rows[d] = (crashes, installs, crashes)
            daily[sid] = rows
        daily[99] = {0: (500, 1, 500)}     # one machine, one day
        q = C.storm_ratio(daily, 20, 0.995)
        self.assertGreater(q, 1.5)
        self.assertLess(q, 500)
        # under the volume floor nothing counts; too few points -> None
        self.assertIsNone(C.storm_ratio({1: {0: (5, 1, 5)}}, 20, 0.995))
        self.assertIsNone(C.storm_ratio({1: {0: (50, 0, 50)}}, 20, 0.995))


if __name__ == '__main__':
    unittest.main()
