# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Robust multiplicative seasonal model for daily crash counts.

A daily series ``y[t]`` is modelled as::

    y[t] ~ level[t] * weekly[weekday(t)] * cycle[t mod 28] * yearly[week(t)]

* Every seasonal factor is a *median* of ratios so the spikes we want to
  detect do not leak into the baseline.  Factors are shrunk toward 1 (or
  toward the factors of a *prior* fit, typically the channel total) when
  they are not distinguishable from noise, see :func:`_phase_factors`.
* A component is only active when the history covers enough full cycles
  (see :data:`COMPONENTS`).  For a signature, components are *borrowed*
  from the channel prior unless the signature has enough informative
  cycles of its own; the calendar 28-day cycle is constrained to carry
  no weekday effect (28 = 4 * 7 makes the two otherwise
  non-identifiable).  Counted from a version's release instead (see
  :func:`with_cycle_phase`) the cycle is the rollout ramp: releases fall
  on varying weekdays so no constraint is needed, and the floor of the
  factors is lower (release day is a few percent of a normal day).
* The level is a robust local-linear (Theil-Sen) forecast over the last
  ``level_window`` de-seasonalised days, so a steady trend is followed
  without lag while up to ~6 anomalous days are ignored.
* Residuals use the Anscombe transform ``2 (sqrt(y + 3/8) - sqrt(e + 3/8))``
  which is ~N(0, 1) for Poisson counts.  Real counts are over-dispersed
  with a relative variance ``c2`` (``Var(y) = e + c2 e^2``), estimated
  from the one-step-ahead residuals, so the score of an observation is
  ``anscombe / sqrt(1 + c2 e)``; small expectations use an exact
  negative-binomial tail instead.

Only numpy/scipy are used; everything is vectorised.
"""

import datetime

import numpy as np
from scipy import ndimage, special, stats


MAD_TO_SIGMA = 1.4826
# Monday 2020-01-06: anchor of the 28-day cycle, so that phase % 7 is the
# weekday (used by the identifiability constraint).
CYCLE_ANCHOR = datetime.date(2020, 1, 6).toordinal()
SMALL_EXPECTED = 10.0
PRIOR_K = 2.0          # cycles of own data needed to weigh 50 % vs prior
C2_K = 10.0            # days of own residuals needed to weigh 50 % vs prior
MAX_SLOPE = 0.10       # max trend per day, relative to the level


def weekday_phase(dates):
    return np.array([d.weekday() for d in dates], dtype=np.int64)


def cycle_phase(dates):
    return np.array([(d.toordinal() - CYCLE_ANCHOR) % 28 for d in dates],
                    dtype=np.int64)


def yearly_phase(dates):
    # ISO week 1..53 -> 0..52
    return np.array([d.isocalendar()[1] - 1 for d in dates], dtype=np.int64)


class Component:
    def __init__(self, name, nphases, period_days, phase, min_cycles,
                 window, smooth=0, borrow_cycles=None, floor=0.05,
                 constrain_weekday=False):
        self.name = name
        self.nphases = nphases
        self.period_days = period_days
        self.phase = phase
        self.min_cycles = min_cycles
        self.window = window
        # circular running-median half-width applied to the factors
        self.smooth = smooth
        # informative cycles a signature needs before its own estimate
        # is mixed with the prior (None: same as min_cycles)
        self.borrow_cycles = borrow_cycles or min_cycles
        # smallest factor (relative to the mean): no phase is expected
        # under this share of a normal day
        self.floor = floor
        # remove the weekday effect from the factors (the calendar
        # 28-day cycle, non-identifiable from the weekly one otherwise)
        self.constrain_weekday = constrain_weekday

    def cycles(self, ndays):
        return ndays / float(self.period_days)

    def is_active(self, ndays):
        return self.cycles(ndays) >= self.min_cycles


COMPONENTS = [
    Component('weekly', 7, 7, weekday_phase, min_cycles=3, window=7),
    Component('cycle', 28, 28, cycle_phase, min_cycles=3, window=29,
              borrow_cycles=6, constrain_weekday=True),
    Component('yearly', 53, 365.25, yearly_phase, min_cycles=2,
              window=365, smooth=1, borrow_cycles=3),
]
BY_NAME = {c.name: c for c in COMPONENTS}


RELEASE_FLOOR = 0.01


def with_cycle_phase(phase, floor=RELEASE_FLOOR):
    """The components with the 28-day cycle counted by *phase* instead of
    the calendar: ``phase(dates) -> ndarray`` of 0..27.  The ``current``
    version scope uses the days since the version's release, so the cycle
    factors describe the rollout ramp of a new version: release day is a
    few percent of a normal day (hence the lower *floor*) and, releases
    falling on varying weekdays, the factors are not constrained to be
    free of a weekday effect (that would divide the ramp by its 4-week
    column means and distort it)."""
    return [Component(c.name, c.nphases, c.period_days, phase,
                      min_cycles=c.min_cycles, window=c.window,
                      smooth=c.smooth, borrow_cycles=c.borrow_cycles,
                      floor=floor, constrain_weekday=False)
            if c.name == 'cycle' else c for c in COMPONENTS]


def component(components, name):
    return next((c for c in components if c.name == name), None)


# --------------------------------------------------------------------------
# Numeric helpers
# --------------------------------------------------------------------------

def anscombe(y, e):
    """Variance-stabilised residual of count ``y`` given expectation ``e``."""
    y = np.asarray(y, dtype=np.float64)
    e = np.asarray(e, dtype=np.float64)
    return 2.0 * (np.sqrt(np.maximum(y, 0) + 0.375) -
                  np.sqrt(np.maximum(e, 0) + 0.375))


def anscombe_inverse(e, a):
    """Count ``y`` such that ``anscombe(y, e) == a`` (clamped at 0)."""
    e = np.asarray(e, dtype=np.float64)
    root = np.sqrt(np.maximum(e, 0) + 0.375) + np.asarray(a) / 2.0
    return np.maximum(np.maximum(root, 0) ** 2 - 0.375, 0.0)


def robust_scale(a, floor=1.0):
    """``1.4826 * MAD`` of the finite values of *a*, floored.

    (``scipy.stats.median_abs_deviation`` does the same but its nan-policy
    decorator costs ~300 us per call; this is called seven times per fit.)
    """
    a = np.asarray(a, dtype=np.float64)
    a = a[np.isfinite(a)]
    if a.size < 3:
        return floor
    mad = np.median(np.abs(a - np.median(a)))
    return max(floor, MAD_TO_SIGMA * float(mad))


def scale(e, c2, extra=0.0):
    """Standard deviation of the Anscombe residual at expectation *e*.

    ``Var(y) = e + (c2 + extra) e^2`` gives ``Var(a) ~ 1 + (c2 + extra) e``.
    *extra* carries additional relative variance, e.g. of the intraday
    profile fraction.
    """
    e = np.asarray(e, dtype=np.float64)
    return np.sqrt(1.0 + np.maximum(e, 0) * max(0.0, c2 + extra))


def score(y, e, c2, extra=0.0):
    """Score a scalar observation *y* against expectation *e*.

    Large expectations use the Anscombe residual divided by
    :func:`scale`; expectations under :data:`SMALL_EXPECTED` use the exact
    mid-p tail of the negative binomial (or Poisson) distribution mapped
    to a normal quantile, which is accurate for small counts.
    """
    if e is None or not np.isfinite(e):
        return None
    e = max(float(e), 0.0)
    y = max(float(y), 0.0)
    rel = max(0.0, c2 + extra)
    if e >= SMALL_EXPECTED:
        return float(anscombe(y, e) / scale(e, rel))
    e = max(e, 0.05)
    k = int(round(y))
    # mid-p upper tail: P(Y > k) + P(Y = k) / 2
    if rel * e < 0.1:
        p_upper = stats.poisson.sf(k, e) + 0.5 * stats.poisson.pmf(k, e)
    else:
        r = 1.0 / rel
        p = r / (r + e)
        p_upper = stats.nbinom.sf(k, r, p) + 0.5 * stats.nbinom.pmf(k, r, p)
    p_upper = min(max(float(p_upper), 1e-300), 1.0 - 1e-16)
    return float(-special.ndtri(p_upper))


def band(e, k, c2):
    """Counts ``(lo, hi)`` at ``+-k`` scales around *e*."""
    s = scale(e, c2)
    return anscombe_inverse(e, -k * s), anscombe_inverse(e, k * s)


def _nanmedian_rows(a):
    """``np.nanmedian(a, axis=1)`` without numpy's masked-array slow path.

    NaN sorts last, so the median of a row is read at the middle of its
    non-NaN prefix.  A row without a value gives NaN.
    """
    s = np.sort(a, axis=1)
    cnt = np.sum(~np.isnan(a), axis=1)
    rows = np.arange(a.shape[0])
    lo = np.maximum(cnt - 1, 0) // 2
    hi = np.maximum(cnt, 1) // 2
    return np.where(cnt > 0, 0.5 * (s[rows, lo] + s[rows, hi]), np.nan)


def rolling_median(x, window, center=True):
    """NaN-aware rolling median with shrinking windows at the edges.

    With ``center=False`` the window is *trailing and exclusive*: the value
    at ``t`` is the median of ``x[t - window:t]``.
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n == 0:
        return np.full(0, np.nan)
    window = max(1, int(window))
    if center:
        half = window // 2
        pad = np.full(half, np.nan)
        padded = np.concatenate([pad, x, pad])
        width = 2 * half + 1
    else:
        padded = np.concatenate([np.full(window, np.nan), x])
        width = window
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)[:n]
    return _nanmedian_rows(windows)


def rolling_level(x, window, trend_min_level, horizon=1):
    """One-step-ahead robust level and slope from trailing windows.

    For every ``t`` the window ``x[t - window:t]`` gives a Theil-Sen slope
    (median of pairwise slopes) and the level forecast at ``t`` is the
    median of the window values projected to ``t`` along that slope.  The
    slope is clipped to ``+-MAX_SLOPE`` of the level per day and ignored
    when the level is under *trend_min_level* (too noisy to matter).

    Returns:
        (level, slope): arrays of size ``len(x) + horizon - 1`` (the last
        ``horizon`` entries are forecasts beyond the series).
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    m = n + horizon
    window = max(2, int(window))
    padded = np.concatenate([np.full(window, np.nan), x,
                             np.full(horizon, np.nan)])
    win = np.lib.stride_tricks.sliding_window_view(padded, window)[:m]
    med = _nanmedian_rows(win)
    i, j = np.triu_indices(window, k=1)
    slopes = (win[:, j] - win[:, i]) / (j - i)
    slope = _nanmedian_rows(slopes)
    slope = np.where(np.isfinite(slope), slope, 0.0)
    limit = MAX_SLOPE * np.maximum(med, 0)
    slope = np.clip(slope, -limit, limit)
    slope = np.where(med >= trend_min_level, slope, 0.0)
    # project the window values to the forecast point (offset window)
    offsets = window - np.arange(window)
    level = _nanmedian_rows(win + slope[:, None] * offsets[None, :])
    level = np.maximum(level, 0.0)
    # the first horizon-1 entries beyond t = 0 are unusable anyway
    return level, slope


def _smooth_circular(f, half):
    """Circular running median of half-width *half*."""
    if half <= 0:
        return f
    return ndimage.median_filter(f, size=2 * half + 1, mode='wrap')


def _phase_factors(ratio, phase, comp, prior=None, weight=1.0):
    """Median ratio per phase, adaptively shrunk toward the prior (or 1).

    Following Efron-Morris, each raw factor is shrunk by
    ``w = max(0, 1 - noise / spread)`` where ``noise`` is the variance of
    that phase's median (``pi/2 sigma^2 / n``, ``sigma`` the robust scale
    of the ratios around their phase medians) and ``spread`` the variance
    of the raw deviations from the prior across phases.  A component whose
    phases differ no more than the noise is flattened to the prior; a
    strong pattern is kept.  *weight* (0..1) further limits how far the
    own estimate may move away from the prior (hierarchical borrowing, in
    log space).
    """
    target = np.ones(comp.nphases) if prior is None else np.asarray(prior)
    finite = np.isfinite(ratio)
    vals, labels = ratio[finite], phase[finite]
    counts = np.bincount(labels, minlength=comp.nphases).astype(np.float64)
    seen = counts > 0
    raw = target.copy()
    if vals.size:
        # median per phase in one pass (unseen phases get a junk value)
        med = ndimage.median(vals, labels=labels,
                             index=np.arange(comp.nphases))
        raw[seen] = np.asarray(med, dtype=np.float64)[seen]
    if np.sum(seen) < 2 or weight <= 0:
        return target.copy()
    resid = vals - raw[labels]
    sigma = robust_scale(resid, floor=0.0)
    dev = raw[seen] - target[seen]
    spread = float(np.var(dev, ddof=1))
    if spread <= 0:
        return target.copy()
    noise = (np.pi / 2.0) * sigma ** 2 / np.maximum(counts, 1)
    w = np.where(seen, np.maximum(0.0, 1.0 - noise / spread), 0.0) * weight
    floor = comp.floor
    with np.errstate(divide='ignore', invalid='ignore'):
        logf = np.log(np.maximum(target, floor)) + w * (
            np.log(np.maximum(raw, floor)) - np.log(np.maximum(target, floor)))
    f = np.exp(logf)
    f = _smooth_circular(f, comp.smooth)
    f = np.maximum(f, floor)
    return f / np.mean(f)


def _constrain_cycle(f):
    """Remove any weekday effect from the 28-day cycle factors."""
    f = np.asarray(f, dtype=np.float64).reshape(4, 7)
    m = f.mean(axis=0)  # one mean per weekday
    f = (f / np.where(m > 0, m, 1.0)).ravel()
    return f / np.mean(f)


# --------------------------------------------------------------------------
# Fit
# --------------------------------------------------------------------------

class Fit:
    """Result of :func:`fit`.

    Attributes:
        dates, y: the input.
        factors (dict): ``name -> ndarray`` for the active components.
        active (dict), cycles (dict), borrowed (set): component status.
        seasonal, level, slope, expected, residual, z (ndarray): in-sample
            one-step-ahead quantities.
        next_level, next_slope (float): level/slope for the day after.
        dispersion (float): robust scale of the residuals.
        c2 (float): relative over-dispersion (``Var = e + c2 e^2``).
    """

    def __init__(self, dates, y, level_window, components=None):
        self.dates = list(dates)
        self.y = np.asarray(y, dtype=np.float64)
        self.level_window = level_window
        # the components (and their phase functions) this fit was made with
        self.components = COMPONENTS if components is None else components
        self.factors = {}
        self.active = {}
        self.cycles = {}
        self.borrowed = set()
        n = self.y.size
        self.seasonal = np.ones(n)
        self.level = np.full(n, np.nan)
        self.slope = np.zeros(n)
        self.expected = np.full(n, np.nan)
        self.residual = np.full(n, np.nan)
        self.z = np.full(n, np.nan)
        self.next_level = 0.0
        self.next_slope = 0.0
        self.dispersion = 1.0
        self.c2 = 0.0

    @property
    def ndays(self):
        return self.y.size

    def seasonal_at(self, date):
        """Seasonal factor of an arbitrary date."""
        s = 1.0
        for comp in self.components:
            if self.active.get(comp.name):
                k = comp.phase([date])[0]
                s *= self.factors[comp.name][k]
        return float(s)

    def factors_at(self, date):
        return {comp.name: round(float(self.factors[comp.name][
            comp.phase([date])[0]]), 4)
            for comp in self.components if self.active.get(comp.name)}

    def phase_of(self, name, date):
        """Phase (0-based) of *date* in component *name*."""
        comp = component(self.components, name)
        return int(comp.phase([date])[0]) if comp is not None else 0

    def forecast(self, date, horizon=1, damping=1.0):
        """Expected count for *date*, *horizon* days after the series.

        The slope is followed one step per day; with *damping* < 1 every
        further step counts *damping* times the previous one (a two-week
        forecast must not extrapolate a rollout ramp forever: with 0.8 the
        drift converges to five steps).
        """
        steps = max(0, horizon - 1)
        if damping >= 1.0 or steps == 0:
            drift = steps
        else:
            drift = (1.0 - damping ** steps) / (1.0 - damping)
        level = max(0.0, self.next_level + self.next_slope * drift)
        return level * self.seasonal_at(date)

    def level_change(self, days=28):
        """Ratio of the current level to the level *days* earlier."""
        if self.ndays <= days:
            return None
        past = self.level[self.ndays - days]
        if not np.isfinite(past) or past <= 0:
            return None
        return float(self.next_level / past)

    def band(self, k, expected=None):
        e = self.expected if expected is None else expected
        return band(e, k, self.c2)

    def score(self, observed, expected, extra=0.0):
        return score(observed, expected, self.c2, extra)

    @property
    def history_days(self):
        """Number of days with data."""
        return int(np.sum(np.isfinite(self.y)))

    def summary(self):
        return {
            'history_days': self.history_days,
            'level': round(float(self.next_level), 3),
            'trend': round(float(self.next_slope), 4),
            'dispersion': round(float(self.dispersion), 4),
            'c2': round(float(self.c2), 6),
            'components': {
                comp.name: {'active': bool(self.active.get(comp.name)),
                            'cycles': round(self.cycles.get(comp.name,
                                                            0.0), 2),
                            'min_cycles': comp.min_cycles}
                for comp in self.components},
            'factors': {name: [round(float(v), 4) for v in f]
                        for name, f in self.factors.items()},
            'borrowed': sorted(self.borrowed),
        }


def _informative_cycles(y, comp, own_min):
    finite = np.isfinite(y)
    return float(np.sum(y[finite] >= own_min)) / comp.period_days


class WeeklyPrior:
    """A prior carrying only the weekly factors of another fit, for a
    series whose own weekdays cannot be told from its release cadence (the
    strict version scope: betas ship on Monday, Wednesday and Friday,
    releases on Tuesday, so its release-day dips would pass for weekday
    effects).  Built from a :class:`Fit` or from cached factors."""

    def __init__(self, weekly, active=True, c2=0.0):
        self.active = {'weekly': bool(active) and weekly is not None}
        self.factors = {'weekly': np.asarray(weekly if weekly is not None
                                             else np.ones(7))}
        self.c2 = c2

    @classmethod
    def from_fit(cls, fit):
        return cls(fit.factors.get('weekly'), fit.active.get('weekly'),
                   fit.c2)


def fit(dates, y, level_window=14, iterations=3, prior=None,
        own_min=10, trend_min_level=50, components=None, borrow=()):
    """Fit the seasonal model.

    Args:
        dates (list[datetime.date]): consecutive days, sorted.
        y (array-like): daily counts, ``NaN`` for unknown days.
        level_window (int): days used by the robust local-linear level.
        iterations (int): back-fitting passes.
        prior (Fit): channel-level fit whose factors and dispersion are
            borrowed by / shrunk toward for a signature.
        own_min (float): a day counts as informative for the own seasonal
            estimate when it has at least this many crashes.
        trend_min_level (float): below this level the slope is ignored.
        borrow (iterable): components always taken from the prior, own
            data notwithstanding.

    Returns:
        Fit
    """
    if components is None:
        components = COMPONENTS
    res = Fit(dates, y, level_window, components)
    y = res.y
    n = y.size
    if n == 0:
        return res
    fin = np.flatnonzero(np.isfinite(y))
    finite = int(fin.size)
    # cycles are counted on the days that actually have data
    span = (dates[fin[-1]] - dates[fin[0]]).days + 1 if finite else 0
    phases = {}
    weights = {}
    for comp in components:
        name = comp.name
        res.cycles[name] = round(comp.cycles(span), 3)
        own_ok = (comp.is_active(span) and
                  finite >= comp.min_cycles * comp.nphases)
        prior_ok = prior is not None and prior.active.get(name)
        res.active[name] = bool(own_ok or prior_ok)
        if not res.active[name]:
            continue
        phases[name] = comp.phase(dates)
        if prior_ok:
            res.factors[name] = np.asarray(prior.factors[name]).copy()
            n_eff = _informative_cycles(y, comp, own_min) if own_ok else 0.0
            if name not in borrow and n_eff >= comp.borrow_cycles:
                weights[name] = n_eff / (n_eff + PRIOR_K)
            else:
                weights[name] = 0.0
                res.borrowed.add(name)
        else:
            res.factors[name] = np.ones(comp.nphases)
            weights[name] = 1.0

    active = [c for c in components if res.active[c.name]]
    estimated = [c for c in active if weights[c.name] > 0]
    with np.errstate(divide='ignore', invalid='ignore'):
        for _ in range(iterations if estimated else 0):
            for comp in estimated:
                name = comp.name
                others = np.ones(n)
                for other in active:
                    if other is not comp:
                        others *= res.factors[other.name][
                            phases[other.name]]
                partial = y / others
                # trend of the fully de-seasonalised series (with the
                # current estimate of this component), as in STL
                own = res.factors[name][phases[name]]
                trend = rolling_median(partial / own, comp.window,
                                       center=True)
                ratio = np.where(trend > 0, partial / trend, np.nan)
                # the centred window is one-sided at the end: exclude
                half = comp.window // 2
                if half > 0 and half < n:
                    ratio[-half:] = np.nan
                target = (np.asarray(prior.factors[name])
                          if prior is not None and prior.active.get(name)
                          else None)
                f = _phase_factors(ratio, phases[name], comp, target,
                                   weights[name])
                if comp.constrain_weekday:
                    f = _constrain_cycle(f)
                res.factors[name] = f
        seasonal = np.ones(n)
        for comp in active:
            seasonal *= res.factors[comp.name][phases[comp.name]]
        res.seasonal = seasonal
        deseason = y / seasonal
        level, slope = rolling_level(deseason, level_window,
                                     trend_min_level, horizon=1)
        res.level = level[:n]
        res.slope = slope[:n]
        res.next_level = float(level[n]) if np.isfinite(level[n]) else 0.0
        res.next_slope = float(slope[n])
        res.expected = res.level * seasonal
        res.residual = anscombe(y, res.expected)
        bad = ~np.isfinite(res.expected) | ~np.isfinite(y)
        res.residual[bad] = np.nan
    res.dispersion = robust_scale(res.residual)
    e_ok = res.expected[np.isfinite(res.residual)]
    e_med = float(np.median(e_ok)) if e_ok.size else 0.0
    c2_own = max(0.0, (res.dispersion ** 2 - 1.0) / e_med) if e_med > 0 \
        else 0.0
    if prior is not None:
        n_res = float(np.sum(np.isfinite(res.residual) &
                             (res.expected >= SMALL_EXPECTED)))
        res.c2 = (n_res * c2_own + C2_K * prior.c2) / (n_res + C2_K)
    else:
        res.c2 = c2_own
    with np.errstate(divide='ignore', invalid='ignore'):
        res.z = res.residual / scale(res.expected, res.c2)
    return res


def make_series(rows, start, end, fill=np.nan):
    """Build ``(dates, y)`` for consecutive days in ``[start, end]``.

    Args:
        rows (dict): ``date -> count``.
        fill: value for days absent from *rows*.
    """
    ndays = (end - start).days + 1
    dates = [start + datetime.timedelta(days=i) for i in range(ndays)]
    y = np.full(ndays, fill, dtype=np.float64)
    for i, d in enumerate(dates):
        v = rows.get(d)
        if v is not None:
            y[i] = v
    return dates, y


def aggregate_weekly(dates, observed, expected, c2, z_thresholds=(3, 5),
                     forecast_after=None):
    """Aggregate daily values into ISO weeks (Monday start).

    Returns a list of dicts with ``start``, ``observed``, ``expected``,
    ``lo<k>``/``hi<k>`` for every *k* in *z_thresholds*, ``z``, ``ndays``
    and ``future``.  The band uses the scale of the weekly sum:
    ``Var(sum) = sum(e) + c2 sum(e^2)`` so on the Anscombe scale
    ``s = sqrt(1 + c2 sum(e^2) / sum(e))``.

    A week starting after *forecast_after* with no observed day is a
    forecast week (``future``): its expectation and band cover all its
    days.  Without *forecast_after* (or before it) a week without data has
    no expectation, as a gap in the history should.
    """
    weeks = {}
    for d, o, e in zip(dates, observed, expected):
        start = d - datetime.timedelta(days=d.weekday())
        w = weeks.setdefault(start, {'o': 0.0, 'e': 0.0, 'e2': 0.0,
                                     'ef': 0.0, 'ef2': 0.0,
                                     'n': 0, 'no': 0, 'ne': 0})
        w['n'] += 1
        known = o is not None and np.isfinite(o)
        if known:
            w['o'] += float(o)
            w['no'] += 1
        if e is not None and np.isfinite(e):
            # observed, expected and band over the same (known) days...
            if known:
                w['e'] += float(e)
                w['e2'] += float(e) ** 2
                w['ne'] += 1
            # ... and the forecast of a week without any observed day
            w['ef'] += float(e)
            w['ef2'] += float(e) ** 2
    res = []
    for start in sorted(weeks):
        w = weeks[start]
        future = w['no'] == 0 and w['ef'] > 0 and \
            forecast_after is not None and start > forecast_after
        e, e2 = (w['ef'], w['ef2']) if future else (w['e'], w['e2'])
        s = np.sqrt(1.0 + c2 * e2 / e) if e > 0 else 1.0
        o = w['o'] if w['no'] else None
        # a score needs an expectation for every observed day
        scorable = o is not None and e > 0 and w['ne'] == w['no']
        item = {'start': start, 'observed': o, 'expected': e if e > 0
                else None, 'ndays': w['n'], 'future': future,
                'z': float(anscombe(o, e) / s) if scorable else None}
        for k in z_thresholds:
            item['lo%d' % k] = float(anscombe_inverse(e, -k * s)) if e > 0 \
                else None
            item['hi%d' % k] = float(anscombe_inverse(e, k * s)) if e > 0 \
                else None
        res.append(item)
    return res
