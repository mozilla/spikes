# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Thresholds learned from the data instead of hand-set.

* **Severity** (watch, spike, major, drop): quantiles of the channel's own
  one-step-ahead z-scores, pooled over its scored signatures (each fit
  caches a histogram of its z).  The config only states the false-alarm
  *rate* per series-day each level may have (``alert_rate``): with 200
  signatures, 1.5 % is three watch flags a day by chance.  A channel with
  heavy tails (Fenix) gets a higher bar than a tight one (nightly), by
  itself.  The Gaussian value for the same rate is a floor (real tails are
  never lighter), and it is used outright when the pooled sample is too
  small; a level whose tail holds fewer than ``MIN_TAIL`` points is
  extrapolated with an exponential tail fitted on the top of the sample.
  Only the series-days that could have been flagged count: a day whose
  distinct installs are known and under the install floor is gated to
  ``ok`` by the scorer whatever its z (one machine crashing in a loop),
  so its residual is left out of the pool instead of raising the bar for
  every other signature (``histogram(keep=...)``).
* **Trend**: the daily z cannot see a slow rise (the level follows it
  within its window), so every fit also scores its last two weeks against
  the two weeks ending four weeks earlier (``seasonal.Fit.trend_scores``);
  the ``trend`` rule is the quantile of that statistic pooled the same
  way, for its own rate.  A trend lasts the four weeks its windows span,
  so the rate buys far fewer episodes than a one-day flag: 0.5 % of the
  series-days is two or three rising signatures at a time on a channel
  with 400 scored signatures.
* **Volume floors** (min crashes / installs to flag a signature): a share
  of the channel's expected day (``volume_share``), installs half of it,
  at least 2.
* **Storm** (crashes per install): a quantile of the channel's own
  crashes-per-install distribution over the last weeks
  (``storm_quantile``).
"""

import math

import numpy as np
from scipy.stats import norm


class Bins:
    """Regular histogram bins of width *step* over ``[lo, hi)``, plus two
    overflow bins: index 0 below *lo*, the last at or above *hi*."""

    def __init__(self, lo, hi, step):
        self.lo, self.hi, self.step = float(lo), float(hi), float(step)
        self.n = int(round((hi - lo) / step))

    @property
    def size(self):
        return self.n + 2


Z_BINS = Bins(-25.0, 50.0, 0.5)         # one-step-ahead z of a day
TREND_BINS = Bins(-50.0, 200.0, 1.0)    # z of two weeks vs. four weeks ago
Z_LO, Z_HI, Z_STEP = Z_BINS.lo, Z_BINS.hi, Z_BINS.step
NBINS = Z_BINS.n
MIN_SAMPLE = 300   # series-days under which the Gaussian null is used
MIN_TAIL = 5       # points above a quantile for it to be trusted
LEVELS = ('watch', 'spike', 'major')
DEFAULT_RATE = {'watch': 0.015, 'spike': 0.0015, 'major': 0.00015,
                'drop': 0.0015, 'trend': 0.005}


def histogram(z, expected=None, min_expected=1.0, keep=None, bins=Z_BINS):
    """Counts of the finite *z* in the fixed *bins*: index 0 is below
    ``bins.lo``, the last at or above ``bins.hi``.  With *expected*, only
    the days whose expectation is at least *min_expected* count (a tiny
    expectation makes no informative z); *keep* is a boolean mask of the
    days to count (the days that could have been flagged)."""
    z = np.asarray(z, dtype=float)
    ok = np.isfinite(z)
    if expected is not None:
        e = np.asarray(expected, dtype=float)
        ok &= np.isfinite(e) & (e >= min_expected)
    if keep is not None:
        ok &= np.asarray(keep, dtype=bool)
    idx = np.floor((z[ok] - bins.lo) / bins.step).astype(int) + 1
    idx = np.clip(idx, 0, bins.n + 1)
    return np.bincount(idx, minlength=bins.size).astype(int).tolist()


def merge(hists, bins=Z_BINS):
    total = np.zeros(bins.size, dtype=float)
    for h in hists:
        if h and len(h) == bins.size:
            total += np.asarray(h, dtype=float)
    return total


def quantile(hist, p, bins=Z_BINS):
    """z under which a share *p* of the pooled sample lies (linear inside
    a bin; the overflow bins clamp to the range)."""
    n = hist.sum()
    if n <= 0:
        return None
    target = p * n
    cum = 0.0
    for i, c in enumerate(hist):
        if c > 0 and cum + c >= target:
            if i == 0:
                return bins.lo
            if i == bins.n + 1:
                return bins.hi
            return bins.lo + (i - 1 + (target - cum) / c) * bins.step
        cum += c
    return bins.hi


def share_above(hist, z, bins=Z_BINS):
    """Share of the sample at or above *z* (linear inside its bin)."""
    n = hist.sum()
    if n <= 0:
        return None
    pos = (z - bins.lo) / bins.step
    i = int(np.floor(pos)) + 1
    if i <= 0:
        return 1.0
    if i >= bins.n + 1:
        return float(hist[bins.n + 1] / n)
    above = 1.0 - (pos - (i - 1))  # part of bin i above z
    return float((hist[i + 1:].sum() + hist[i] * above) / n)


def gaussian(p):
    return float(norm.ppf(p))


def _tail(hist, n, rate, upper, bins=Z_BINS):
    """Threshold for a false-alarm *rate* in the upper (or lower) tail:
    the empirical quantile when at least ``MIN_TAIL`` points lie beyond it,
    else an exponential tail through the last two trusted quantiles; never
    inside the Gaussian value for the same rate.  Returns (z, method)."""
    g = gaussian(1 - rate) if upper else gaussian(rate)
    outer = max if upper else min
    if n < MIN_SAMPLE:
        return g, 'gaussian'
    if rate * n >= MIN_TAIL:
        q = quantile(hist, 1 - rate, bins) if upper else \
            quantile(hist, rate, bins)
        return outer(q, g), 'empirical'
    p1, p2 = 4.0 * MIN_TAIL / n, MIN_TAIL / n
    if upper:
        z1, z2 = quantile(hist, 1 - p1, bins), quantile(hist, 1 - p2, bins)
    else:
        z1, z2 = quantile(hist, p1, bins), quantile(hist, p2, bins)
    if z1 is None or z2 is None or abs(z2 - z1) < 1e-9:
        return outer(z2 if z2 is not None else g, g), 'gaussian'
    slope = (math.log(p2) - math.log(p1)) / (z2 - z1)
    z = z2 + (math.log(rate) - math.log(p2)) / slope
    return outer(z, g), 'extrapolated'


def calibrate(hists, rates=None, trend_hists=()):
    """Severity rules from the pooled z histograms of a channel's series,
    and the trend rule from their pooled trend histograms
    (``TREND_BINS``).

    Returns ``{'rules': {level: {'z'}}, 'method': {level: how}, 'sample',
    'series', 'trend_sample', 'rates', 'gaussian': {level: z}, 'tail':
    {level: share of the sample beyond the threshold}}``.
    """
    rates = dict(DEFAULT_RATE, **(rates or {}))
    hist = merge(hists)
    n = int(hist.sum())
    rules, method, tail = {}, {}, {}
    prev = None
    for level in LEVELS:
        z, how = _tail(hist, n, rates[level], upper=True)
        if prev is not None and z < prev + 0.5:  # keep the levels apart
            z = prev + 0.5
        rules[level] = {'z': round(z, 2)}
        method[level] = how
        tail[level] = share_above(hist, z)
        prev = z
    z, how = _tail(hist, n, rates['drop'], upper=False)
    rules['drop'] = {'z': round(z, 2)}
    method['drop'] = how
    tail['drop'] = None if n == 0 else 1.0 - (share_above(hist, z) or 0.0)
    thist = merge(trend_hists, TREND_BINS)
    tn = int(thist.sum())
    z, how = _tail(thist, tn, rates['trend'], upper=True, bins=TREND_BINS)
    rules['trend'] = {'z': round(z, 2)}
    method['trend'] = how
    tail['trend'] = share_above(thist, z, TREND_BINS)
    return {'rules': rules, 'method': method, 'sample': n,
            'series': sum(1 for h in hists if h), 'trend_sample': tn,
            'rates': rates,
            'gaussian': {lvl: round(gaussian(1 - rates[lvl]), 2)
                         for lvl in LEVELS + ('trend',)} | {
                             'drop': round(gaussian(rates['drop']), 2)},
            'tail': tail}


def volume_floors(expected_day, share):
    """(min crashes, min installs) for a signature to be flagged: a share
    of the channel's expected day, installs half of it, at least 2."""
    crashes = max(2, int(round(share * max(0.0, expected_day or 0.0))))
    return crashes, max(2, crashes // 2)


def storm_ratio(daily, min_crashes, q, min_points=20):
    """Crashes-per-install ratio above which a signature is a storm: the
    *q* quantile of the channel's ratios over the series-days in *daily*
    (``{series: {day: (crashes, installs, ...)}}``) with at least
    *min_crashes* crashes; None with too few points."""
    ratios = [row[0] / float(row[1]) for rows in daily.values()
              for row in rows.values()
              if row[1] and row[0] >= min_crashes]
    if len(ratios) < min_points:
        return None
    return float(np.quantile(ratios, q))
