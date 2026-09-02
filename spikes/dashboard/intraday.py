# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Intraday arrival profiles: what share of a day's crashes is in by hour h.

From the exact hourly totals of past days a cumulative profile ``F(h)``
(``F(0) = 0``, ``F(24) = 1``) is estimated as the element-wise median of
the daily cumulative shares, after excluding *burst days* (an hour with
more than ``burst_factor`` times its usual share).  A per-weekday profile
is estimated from the last same-weekday days and shrunk toward the
all-days profile.  ``vF(h)`` is the robust relative variance of ``F(h)``
across days; it feeds the dispersion of intraday scores.

Measured on 28 days of Firefox release the share ranges from 2.7 % at
23:00 UTC to 5.6 % at 14:00 and ``F`` is very stable (relative sd 6 % at
03:00, 2 % at 09:00, < 1 % at 18:00); nightly channels are much burstier,
which the exclusion and the variance term take care of.
"""

import datetime

import numpy as np


HOURS = 24
SHRINK_K = 4.0


class Profile:
    """Cumulative arrival profile with per-weekday variants."""

    def __init__(self, f_all, v_all, f_weekday, v_weekday, ndays, source):
        self.f_all = np.asarray(f_all, dtype=np.float64)
        self.v_all = np.asarray(v_all, dtype=np.float64)
        self.f_weekday = {w: np.asarray(f, dtype=np.float64)
                          for w, f in f_weekday.items()}
        self.v_weekday = {w: np.asarray(v, dtype=np.float64)
                          for w, v in v_weekday.items()}
        self.ndays = ndays
        self.source = source

    def curves(self, weekday):
        f = self.f_weekday.get(weekday, self.f_all)
        v = self.v_weekday.get(weekday, self.v_all)
        return f, v

    def fraction(self, weekday, hour):
        """``(F(hour), vF(hour))`` at a fractional *hour* in ``[0, 24]``."""
        f, v = self.curves(weekday)
        return _interp(f, hour), _interp(v, hour)

    def hourly_expected(self, weekday, expected_day):
        """Expected count of each UTC hour for a day expecting
        *expected_day* crashes."""
        f, _ = self.curves(weekday)
        return [float(expected_day * (f[h + 1] - f[h])) for h in range(HOURS)]

    def summary(self):
        return {'source': self.source, 'ndays': self.ndays,
                'f_all': [round(float(x), 5) for x in self.f_all]}


def _interp(curve, hour):
    hour = min(max(float(hour), 0.0), float(HOURS))
    lo = int(np.floor(hour))
    if lo >= HOURS:
        return float(curve[HOURS])
    frac = hour - lo
    return float(curve[lo] + frac * (curve[lo + 1] - curve[lo]))


def hour_of(dt):
    """Fractional UTC hour of a naive datetime."""
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0


def _cumulative(shares):
    """Cumulative shares with a leading 0: shape (ndays, 25)."""
    cum = np.cumsum(shares, axis=1)
    return np.concatenate([np.zeros((shares.shape[0], 1)), cum], axis=1)


def _robust_curve(cum):
    """Median cumulative curve and its robust relative variance."""
    f = np.median(cum, axis=0)
    f = np.maximum.accumulate(f)
    f[0] = 0.0
    if f[-1] > 0:
        f = f / f[-1]
    mad = np.median(np.abs(cum - np.median(cum, axis=0)), axis=0)
    with np.errstate(divide='ignore', invalid='ignore'):
        rel = np.where(f > 0, 1.4826 * mad / f, 0.0)
    v = np.where(np.isfinite(rel), rel ** 2, 0.0)
    v[0] = v[1] if v.size > 1 else 0.0
    v[-1] = 0.0
    return f, v


def build_profile(rows, today, profile_days=28, weekday_days=8,
                  min_total=50, burst_factor=3.0, source='channel'):
    """Estimate a :class:`Profile` from ``{day: [24 counts]}`` rows.

    Only completed days (``day < today``) with at least *min_total* crashes
    are used.  Returns ``None`` when fewer than 3 usable days exist.
    """
    days = sorted(d for d in rows if d < today)
    recent = [d for d in days if (today - d).days <= profile_days]
    if len(recent) < 3:
        return None
    usable = []
    for d in days:
        h = np.asarray(rows[d], dtype=np.float64)
        if h.size == HOURS and h.sum() >= min_total:
            usable.append((d, h / h.sum()))
    if len(usable) < 3:
        return None
    shares = np.array([s for _, s in usable])
    med = np.median(shares, axis=0)
    with np.errstate(divide='ignore', invalid='ignore'):
        ratio = np.where(med > 0, shares / med, 0.0)
    burst = ratio.max(axis=1) > burst_factor
    if np.sum(~burst) >= 5:
        keep = ~burst
    else:
        keep = np.ones(len(usable), dtype=bool)
    all_idx = [i for i, (d, _) in enumerate(usable)
               if keep[i] and (today - d).days <= profile_days]
    if len(all_idx) < 3:
        all_idx = [i for i, (d, _) in enumerate(usable)
                   if (today - d).days <= profile_days]
        if len(all_idx) < 3:
            return None
    cum = _cumulative(shares)
    f_all, v_all = _robust_curve(cum[all_idx])
    f_weekday, v_weekday = {}, {}
    for w in range(7):
        idx = [i for i, (d, _) in enumerate(usable)
               if keep[i] and d.weekday() == w]
        idx = idx[-weekday_days:]
        if len(idx) < 2:
            continue
        f_w, v_w = _robust_curve(cum[idx])
        n = len(idx)
        shrink = n / (n + SHRINK_K)
        f = f_all + shrink * (f_w - f_all)
        f = np.maximum.accumulate(np.maximum(f, 0.0))
        f[0] = 0.0
        f[-1] = 1.0
        f_weekday[w] = f
        v_weekday[w] = np.maximum(v_all, shrink * v_w + (1 - shrink) * v_all)
    return Profile(f_all, v_all, f_weekday, v_weekday, len(all_idx), source)


def window(hourly_today, hourly_yesterday, as_of, hours):
    """Observed count in the last *hours* hours ending at *as_of*.

    Buckets of yesterday and today are concatenated (48 slots).  The
    bucket containing *as_of* is taken in full (it only holds what arrived
    before *as_of*); the bucket containing the window start contributes
    the fraction of it that lies inside the window.

    Returns:
        (observed, start_hour, end_hour) with hours in ``[0, 48)`` of the
        concatenated axis, or ``None`` when the data is missing.
    """
    if hourly_today is None:
        return None
    yesterday = hourly_yesterday if hourly_yesterday is not None \
        else [0] * HOURS
    counts = np.asarray(list(yesterday) + list(hourly_today),
                        dtype=np.float64)
    end = HOURS + hour_of(as_of)
    start = max(0.0, end - hours)
    first = int(np.floor(start))
    last = int(np.floor(end))
    if last >= 2 * HOURS:
        last = 2 * HOURS - 1
    if first == last:
        return float(counts[first]), start, end
    frac_first = 1.0 - (start - first)
    observed = counts[first] * frac_first + counts[first + 1:last].sum() + \
        counts[last]
    return float(observed), start, end


def window_expected(profile, expected_today, expected_yesterday, today,
                    start, end):
    """Expected count and relative variance for a window of
    :func:`window` (hours on the 48-slot axis)."""
    yesterday = today - datetime.timedelta(days=1)
    e = 0.0
    var = 0.0
    if start < HOURS:
        f_end, _ = profile.fraction(yesterday.weekday(), HOURS)
        f_start, v_start = profile.fraction(yesterday.weekday(), start)
        e += expected_yesterday * (f_end - f_start)
        var += v_start
        f0, v0 = 0.0, 0.0
    else:
        f0, v0 = profile.fraction(today.weekday(), start - HOURS)
    f1, v1 = profile.fraction(today.weekday(), end - HOURS)
    e += expected_today * (f1 - f0)
    var += v0 + v1
    return float(e), float(var)
