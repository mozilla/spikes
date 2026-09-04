# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Score the series of a channel against their seasonal expectation.

For every channel the run:

1. fits (or reuses the cached fit of) the channel total and builds the
   intraday arrival profile from its hourly history;
2. selects the candidate signatures (enough crashes today, yesterday or
   recently), refreshes the stale cached fits oldest-first within
   ``max_fits_per_run`` (fits borrow the channel's seasonal factors when
   the signature has too little volume of its own);
3. scores *today so far* (cumulative, against ``E_day * F(as_of)`` blended
   with the channel's realised pace) and the *last few hours*, projects the
   end of day, gates the severity with the crash and distinct-install
   counts of the last 24 hours (a full day's worth at any hour, so the
   per-day floors do not hide a spike at 06:00 UTC), applies hysteresis
   and tracks the peak of the day;
4. scores *yesterday* as a complete day;
5. computes the drivers of the channel total's deviation.

Scores are upserted in place in ``dashboard_scores``.
"""

import datetime

import numpy as np

from spikes.logger import logger
from . import calibration, config, intraday, models, seasonal, versions


RANK = {'ok': 0, 'drop': 1, 'watch': 2, 'spike': 3, 'major': 4}
UPWARD = ('watch', 'spike', 'major')
PROFILE_HISTORY_DAYS = 56


def severity_of(z, ratio, rules, expected=None, min_expected_drop=0.0):
    """Severity label from a score.

    *rules* are the channel's calibrated thresholds ``{'watch': {'z'},
    'spike': {'z'}, 'major': {'z'}, 'drop': {'z'}}`` (calibration.py).
    *ratio* is accepted for compatibility and ignored: the over-dispersion
    term of the score already grows with the count.
    """
    if z is None or not np.isfinite(z):
        return 'ok'
    for label in ('major', 'spike', 'watch'):
        rule = rules.get(label)
        if rule and z >= rule['z']:
            return label
    rule = rules.get('drop')
    if rule and z <= rule['z'] and \
            (expected is None or expected >= min_expected_drop):
        return 'drop'
    return 'ok'


def worst(*labels):
    return max(labels, key=lambda s: RANK.get(s, 0))


def confidence(z):
    if z is None or not np.isfinite(z):
        return 0
    return sum(1 for t in (3.0, 5.0, 8.0) if abs(z) >= t)


def seasonal_at(factors, date, components=None):
    """Seasonal factor of *date* from a ``{name: [values]}`` dict, with
    the phases of *components* (the calendar ones by default)."""
    s = 1.0
    for name, values in (factors or {}).items():
        comp = seasonal.component(components or seasonal.COMPONENTS, name)
        if comp is not None and values:
            s *= float(values[comp.phase([date])[0]])
    return s


class Cached:
    """Model parameters of a series (from a Fit or a Model row)."""

    def __init__(self, level, trend, c2, dispersion, factors, borrowed,
                 components, install_share, level_change_28, last_day,
                 history_days, recent_days_seen, fitted_at, z_hist=None,
                 seasonal_components=None):
        # the seasonal components (phase functions) the factors refer to:
        # the release-phase cycle for the ``current`` scope (versions.py)
        self.seasonal_components = seasonal_components or \
            seasonal.COMPONENTS
        self.level = level
        self.trend = trend
        self.c2 = c2
        self.dispersion = dispersion
        self.factors = factors or {}
        self.borrowed = borrowed or []
        self.components = components or {}
        self.install_share = install_share
        self.level_change_28 = level_change_28
        self.last_day = last_day
        self.history_days = history_days
        self.recent_days_seen = recent_days_seen
        self.fitted_at = fitted_at
        # histogram of the one-step-ahead z of the history: pooled over the
        # channel's series to learn its severity thresholds
        self.z_hist = z_hist

    @classmethod
    def from_fit(cls, fit, last_day, install_share, recent_days_seen,
                 fitted_at):
        s = fit.summary()
        return cls(fit.next_level, fit.next_slope, fit.c2, fit.dispersion,
                   s['factors'], s['borrowed'], s['components'],
                   install_share, fit.level_change(28), last_day,
                   s['history_days'], recent_days_seen, fitted_at,
                   calibration.histogram(fit.z, fit.expected),
                   seasonal_components=fit.components)

    @classmethod
    def from_row(cls, row, components=None):
        details = row.components or {}
        return cls(row.level, row.trend, row.c2, row.dispersion,
                   row.factors, row.borrowed, details.get('components'),
                   row.install_share, row.level_change_28, row.last_day,
                   row.history_days, details.get('recent_days_seen', 1),
                   row.fitted_at, details.get('z_hist'),
                   seasonal_components=components)

    def to_row(self, series_id):
        return {'series_id': series_id, 'fitted_at': self.fitted_at,
                'last_day': self.last_day,
                'history_days': int(self.history_days),
                'level': float(self.level), 'trend': float(self.trend),
                'dispersion': float(self.dispersion), 'c2': float(self.c2),
                'install_share': self.install_share,
                'factors': self.factors, 'borrowed': list(self.borrowed),
                'components': {'components': self.components,
                               'recent_days_seen': self.recent_days_seen,
                               'z_hist': self.z_hist},
                'level_change_28': self.level_change_28}

    def expected(self, date):
        """Expected full-day count for *date* (after ``last_day``)."""
        if self.last_day is None:
            horizon = 1
        else:
            horizon = max(1, (date - self.last_day).days)
        level = max(0.0, self.level + self.trend * (horizon - 1))
        return level * seasonal_at(self.factors, date,
                                   self.seasonal_components)


# --------------------------------------------------------------------------
# History loading and fitting
# --------------------------------------------------------------------------

def censored_value(day_row):
    """Imputed count of a signature absent from a day's top list (NaN
    for a day never fetched or that Socorro has no data for)."""
    if day_row is None or day_row.crashes is None:
        return np.nan
    if day_row.cutoff is None:
        return 0.0
    return day_row.cutoff / 2.0


def build_history(daily, day_rows, start, end):
    """``(dates, y, installs)`` for one series over ``[start, end]``.

    *daily* is ``{day: (crashes, installs)}``; days without a row are
    censored at the channel's cutoff, or NaN when the channel has no data.
    """
    ndays = (end - start).days + 1
    dates = [start + datetime.timedelta(days=i) for i in range(ndays)]
    y = np.empty(ndays)
    installs = {}
    for i, d in enumerate(dates):
        row = daily.get(d)
        if row is not None:
            y[i] = row[0]
            if row[1] is not None and row[0] > 0:
                installs[d] = row[1] / float(row[0])
        else:
            y[i] = censored_value(day_rows.get(d))
    return dates, y, installs


def fit_series(daily, day_rows, start, end, prior, min_crashes, now,
               components=None):
    """Fit one series and return a :class:`Cached`."""
    dates, y, installs = build_history(daily, day_rows, start, end)
    fit = seasonal.fit(dates, y, level_window=config.get('level_window', 14),
                       prior=prior,
                       own_min=config.get('own_factors_min_crashes', 10),
                       trend_min_level=config.get('trend_min_level', 50),
                       components=components)
    recent = [v for v in y[-config.get('new_days', 14):] if np.isfinite(v)]
    seen = sum(1 for v in recent if v >= max(3, min_crashes / 2.0))
    share = None
    if installs:
        vals = sorted(installs.items())[-config.get('install_share_days',
                                                    28):]
        share = float(np.median([v for _, v in vals]))
    return fit, Cached.from_fit(fit, end, share, seen, now)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

class Context:
    """Everything needed to score the series of one channel."""

    def __init__(self, product, channel, today, as_of, profile, rules,
                 min_crashes, min_installs, storm_ratio=None,
                 installs_as_of=None):
        self.product = product
        self.channel = channel
        self.today = today
        self.yesterday = today - datetime.timedelta(days=1)
        self.as_of = as_of
        # distinct installs are refreshed less often than the counts
        self.installs_as_of = installs_as_of or as_of
        self.profile = profile
        # all learned from the channel's own data (calibration.py)
        self.rules = rules
        self.min_crashes = min_crashes
        self.min_installs = min_installs
        self.storm_ratio = storm_ratio
        self.pace = None
        self.hour = intraday.hour_of(as_of)


def _projection(observed, elapsed, c2):
    if elapsed is None or elapsed < 0.25 or observed <= 0:
        return None, None, None
    s = seasonal.scale(observed, c2)
    lo = float(seasonal.anscombe_inverse(observed, -2 * s))
    hi = float(seasonal.anscombe_inverse(observed, 2 * s))
    return observed / elapsed, lo / elapsed, hi / elapsed


def _recent(ctx, cached, hourly_today, hourly_yesterday, e_today, e_yday):
    """Best recent window: the shortest with enough expected crashes."""
    if ctx.profile is None or hourly_today is None:
        return None, 'no hourly data'
    min_e = config.get('min_expected_recent', 10)
    last = None
    for hours in config.get('recent_hours', [3, 6, 12]):
        win = intraday.window(hourly_today, hourly_yesterday, ctx.as_of,
                              hours)
        if win is None:
            return None, 'no hourly data'
        observed, start, end = win
        expected, var = intraday.window_expected(
            ctx.profile, e_today, e_yday, ctx.today, start, end)
        last = (hours, expected)
        if expected >= min_e:
            z = seasonal.score(observed, expected, cached.c2, var)
            return {'hours': hours, 'observed': int(round(observed)),
                    'expected': expected, 'z': z,
                    'excess': observed - expected,
                    'ratio': observed / expected if expected > 0 else None}, \
                None
    if last is None:
        return None, 'no window'
    # the longest window has too few expected crashes: score it anyway
    # when it holds enough observed crashes (exact small-count tails make
    # the score valid), else just report what happened in it
    if observed >= ctx.min_crashes:
        z = seasonal.score(observed, expected, cached.c2, var)
        reason = None
    else:
        z = None
        reason = 'quiet: {} observed, {:.1f} expected ({}h)'.format(
            int(round(observed)), expected, hours)
    return {'hours': hours, 'observed': int(round(observed)),
            'expected': expected, 'z': z, 'excess': observed - expected,
            'ratio': observed / expected if expected > 0 else None}, reason


def trailing_day(ctx, observed, installs, daily_yesterday, hourly_yesterday):
    """Crashes and distinct installs of the last 24 hours, for the gates.

    Today's counts plus the part of yesterday after this hour, so
    ``min_crashes`` / ``min_installs`` keep their per-day meaning at any
    hour of the day (at 06:00 UTC a fifth of the day is in: a spike that
    already scores high would otherwise be hidden until the afternoon).
    Yesterday's crashes come from its hourly split (the bucket containing
    this hour pro rata); its installs are not additive across days and are
    scaled by the share of its crashes usually after this hour, an
    estimate that counts an install seen on both days twice.
    """
    y_crashes = daily_yesterday[0] if daily_yesterday else 0
    y_installs = daily_yesterday[1] if daily_yesterday else None
    if ctx.profile is not None:
        f = ctx.profile.fraction(ctx.yesterday.weekday(), ctx.hour)[0]
    else:
        f = min(ctx.hour / 24.0, 1.0)
    share = max(0.0, 1.0 - f)
    if hourly_yesterday is not None:
        carried = intraday.tail(hourly_yesterday, ctx.hour)
    else:
        carried = y_crashes * share
    crashes = observed + carried
    if installs is None:
        installs_24 = None
    else:
        installs_24 = installs + (y_installs or 0) * share
    return crashes, installs_24


def score_today(ctx, series, cached, daily_today, hourly_today,
                hourly_yesterday, previous, is_total, daily_yesterday=None):
    """Score one series for the current (partial) day."""
    observed = daily_today[0] if daily_today else 0
    installs = daily_today[1] if daily_today else None
    gate_crashes, gate_installs = trailing_day(ctx, observed, installs,
                                               daily_yesterday,
                                               hourly_yesterday)
    # crash count fetched together with the installs (the ratio needs a
    # matched pair; `observed` may be a few minutes fresher)
    installs_crashes = daily_today[2] if daily_today and \
        len(daily_today) > 2 and daily_today[2] is not None else observed
    e_day = cached.expected(ctx.today)
    e_yday = cached.expected(ctx.yesterday)
    weekday = ctx.today.weekday()
    if ctx.profile is not None:
        elapsed, var_f = ctx.profile.fraction(weekday, ctx.hour)
    else:
        elapsed, var_f = min(ctx.hour / 24.0, 1.0), 0.05
    fraction = elapsed
    if not is_total and ctx.pace is not None:
        lam = config.get('pace_blend', 0.5)
        fraction = (1 - lam) * elapsed + lam * ctx.pace
    expected = e_day * fraction
    z = seasonal.score(observed, expected, cached.c2, var_f)
    ratio = observed / expected if expected > 0 else None
    projected, lo, hi = _projection(observed, elapsed, cached.c2)
    recent, reason = _recent(ctx, cached, hourly_today, hourly_yesterday,
                             e_day, e_yday)
    # a drop needs enough expected crashes to be a drop of something
    sev = severity_of(z, ratio, ctx.rules, expected, ctx.min_crashes)
    z_recent = recent['z'] if recent else None
    if z_recent is not None:
        sev_recent = severity_of(z_recent, recent['ratio'], ctx.rules)
        if sev_recent in UPWARD:
            sev = worst(sev, sev_recent)
    # hysteresis on the ungated crash severity: only step down once z is
    # clearly under the threshold (the gates below always have the last
    # word)
    if previous is not None and RANK.get(previous.severity, 0) > RANK[sev] \
            and previous.severity in UPWARD:
        rule = ctx.rules.get(previous.severity)
        zz = max(z if z is not None else -99,
                 z_recent if z_recent is not None else -99)
        if rule and zz >= rule['z'] - config.get('hysteresis', 1.0):
            sev = previous.severity
    # Distinct installs are first class: one machine crashing a thousand
    # times is one machine.  An upward severity needs (1) at least
    # `min_installs` distinct installs today and (2) the install count to
    # deviate from its own expectation as much as the crash count does.
    # A storm (a handful of installs, or dozens of crashes per install) is
    # a badge, not an alert.
    storm = False
    e_inst = z_inst = None
    per_install = None
    if installs is not None and observed >= ctx.min_crashes:
        per_install = installs_crashes / float(max(installs, 1))
        # storm: crashes per install in the channel's own extreme tail
        if ctx.storm_ratio is not None and per_install >= ctx.storm_ratio:
            storm = True
    if installs is not None and cached.install_share:
        # the install count is as of its own fetch time
        if ctx.profile is not None:
            f_inst, var_inst = ctx.profile.fraction(
                weekday, intraday.hour_of(ctx.installs_as_of))
        else:
            f_inst, var_inst = min(intraday.hour_of(
                ctx.installs_as_of) / 24.0, 1.0), 0.05
        e_inst = e_day * f_inst * cached.install_share
        z_inst = seasonal.score(installs, e_inst, cached.c2, var_inst)
    # the absolute floors are per day: checked on the last 24 hours
    if sev in UPWARD and installs is not None:
        if gate_installs < ctx.min_installs:
            sev = 'ok'
        elif z_inst is not None:
            r_inst = installs / e_inst if e_inst > 0 else None
            sev_inst = severity_of(z_inst, r_inst, ctx.rules)
            if RANK[sev_inst] < RANK[sev]:
                sev = sev_inst if sev_inst in UPWARD else 'ok'
    elif sev in UPWARD and installs is None:
        # installs unknown (not fetched yet): they cannot pass the gate
        sev = 'ok'
    if gate_crashes < ctx.min_crashes and sev in UPWARD:
        sev = 'ok'
    is_new = bool(not is_total and cached.recent_days_seen == 0 and
                  gate_crashes >= ctx.min_crashes and
                  gate_installs is not None and
                  gate_installs >= ctx.min_installs)
    row = {
        'series_id': series.id, 'day': ctx.today, 'as_of': ctx.as_of,
        'partial': True, 'elapsed': elapsed, 'observed': int(observed),
        'installs': installs, 'expected_installs': e_inst,
        'z_installs': z_inst,
        'expected_day': e_day, 'expected': expected,
        'z': z, 'ratio': ratio, 'excess': observed - expected,
        'projected': projected, 'projected_lo': lo, 'projected_hi': hi,
        'recent_hours': recent['hours'] if recent else None,
        'observed_recent': recent['observed'] if recent else None,
        'expected_recent': recent['expected'] if recent else None,
        'z_recent': recent['z'] if recent else None,
        'recent_reason': reason, 'severity': sev, 'is_new': is_new,
        'storm': storm,
        'first_flagged_at': previous.first_flagged_at if previous else None,
        'last_flagged_at': ctx.as_of if sev != 'ok' else (
            previous.last_flagged_at if previous else None),
        'peak_severity': previous.peak_severity if previous else None,
        'peak_z': previous.peak_z if previous else None,
        'peak_excess': previous.peak_excess if previous else None,
        'peak_at': previous.peak_at if previous else None,
        'details': {'level': cached.level, 'dispersion': cached.dispersion,
                    'c2': cached.c2,
                    'level_change_28': cached.level_change_28,
                    'profile': ctx.profile.source if ctx.profile else None,
                    'pace': ctx.pace if not is_total else None,
                    'installs_ratio': per_install,
                    'installs_as_of': ctx.installs_as_of.isoformat()
                    if ctx.installs_as_of else None,
                    'last24': {'crashes': int(round(gate_crashes)),
                               'installs': int(round(gate_installs))
                               if gate_installs is not None else None}},
    }
    if sev != 'ok' and sev != 'drop':
        if row['first_flagged_at'] is None:
            row['first_flagged_at'] = ctx.as_of
        zpeak = max(z or 0, z_recent if z_recent is not None else 0)
        if row['peak_severity'] is None or \
                RANK[sev] > RANK.get(row['peak_severity'], 0) or \
                (RANK[sev] == RANK.get(row['peak_severity'], 0) and
                 zpeak > (row['peak_z'] or 0)):
            row['peak_severity'] = sev
            row['peak_z'] = zpeak
            row['peak_excess'] = row['excess']
            row['peak_at'] = ctx.as_of
    return row


def score_yesterday(ctx, series, cached, fit, daily_yesterday, previous):
    """Score one series for the previous (complete) day."""
    if daily_yesterday is None and fit is None:
        return None
    observed = daily_yesterday[0] if daily_yesterday else 0
    installs = daily_yesterday[1] if daily_yesterday else None
    if fit is not None and fit.ndays and fit.dates[-1] == ctx.yesterday:
        expected = float(fit.expected[-1])
        if not np.isfinite(expected):
            expected = cached.expected(ctx.yesterday)
    else:
        expected = cached.expected(ctx.yesterday)
    z = seasonal.score(observed, expected, cached.c2)
    ratio = observed / expected if expected > 0 else None
    sev = severity_of(z, ratio, ctx.rules, expected, ctx.min_crashes)
    e_inst = z_inst = None
    if installs is not None and cached.install_share:
        e_inst = expected * cached.install_share
        z_inst = seasonal.score(installs, e_inst, cached.c2)
    if sev in UPWARD and installs is not None:
        if installs < ctx.min_installs:
            sev = 'ok'
        elif z_inst is not None:
            r_inst = installs / e_inst if e_inst > 0 else None
            sev_inst = severity_of(z_inst, r_inst, ctx.rules)
            if RANK[sev_inst] < RANK[sev]:
                sev = sev_inst if sev_inst in UPWARD else 'ok'
    if observed < ctx.min_crashes and sev in UPWARD:
        sev = 'ok'
    row = {
        'series_id': series.id, 'day': ctx.yesterday, 'as_of': ctx.as_of,
        'partial': False, 'elapsed': 1.0, 'observed': int(observed),
        'installs': installs, 'expected_installs': e_inst,
        'z_installs': z_inst, 'expected_day': expected,
        'expected': expected, 'z': z, 'ratio': ratio,
        'excess': observed - expected, 'severity': sev,
        'is_new': previous.is_new if previous else False,
        'storm': previous.storm if previous else False,
        'recent_reason': None,
        'details': {'level': cached.level, 'dispersion': cached.dispersion,
                    'c2': cached.c2},
    }
    if previous is not None:
        for k in ('first_flagged_at', 'last_flagged_at', 'peak_severity',
                  'peak_z', 'peak_excess', 'peak_at', 'projected',
                  'projected_lo', 'projected_hi', 'recent_hours',
                  'observed_recent', 'expected_recent', 'z_recent'):
            row[k] = getattr(previous, k)
    if sev in UPWARD and row.get('peak_severity') is None:
        row['peak_severity'] = sev
        row['peak_z'] = z
        row['peak_excess'] = row['excess']
        row['peak_at'] = ctx.as_of
        row['first_flagged_at'] = row.get('first_flagged_at') or ctx.as_of
    return row


def drivers(total_row, sig_rows, series_by_id, n=5):
    """Signatures explaining the total's deviation."""
    excess = total_row.get('excess') or 0.0
    if abs(excess) < 1:
        return []
    sign = 1 if excess > 0 else -1
    items = []
    for r in sig_rows:
        e = (r.get('excess') or 0.0) * sign
        if e > 0:
            items.append((e, r))
    items.sort(key=lambda p: -p[0])
    res = []
    for e, r in items[:n]:
        s = series_by_id[r['series_id']]
        res.append({'signature': s.signature, 'excess': round(e * sign),
                    'share': round(min(1.0, e / abs(excess)), 3),
                    'severity': r['severity'], 'noise': bool(s.noise),
                    'installs': r.get('installs'), 'storm': bool(r['storm'])})
    return res


def storm_share(total_row, sig_rows):
    """Share of the total's excess coming from storm signatures."""
    excess = total_row.get('excess') or 0.0
    if excess <= 0:
        return 0.0
    from_storms = sum((r.get('excess') or 0.0) for r in sig_rows
                      if r['storm'] and (r.get('excess') or 0.0) > 0)
    return min(1.0, from_storms / excess)


# --------------------------------------------------------------------------
# Channel
# --------------------------------------------------------------------------

def score_channel(product, channel, today, now, fits_budget=None,
                  stale_fits=False):
    """Score a channel.  Returns a summary dict (or None without data).

    Args:
        fits_budget (int): max *refits* of stale cached models this run
            (series without a cached model are always fitted).
        stale_fits (bool): the history is still being backfilled: cache
            the fits but mark them stale so they are redone next run.
    """
    day_row = models.get_day(product, channel, today)
    if day_row is None or day_row.as_of is None:
        return None
    as_of = day_row.as_of
    # the versioned scopes count the cycle from the version's release
    scope = config.split_channel(channel)[1]
    comps = versions.components_for(product, channel)
    history_days = config.get('history_days', 180)
    start = today - datetime.timedelta(days=history_days)
    # the total is fitted on everything stored (up to 3 years) so the
    # yearly component can activate; signatures borrow it (their own
    # fit needs 180 days at most)
    start_total = today - datetime.timedelta(days=config.fit_history_days())
    yesterday = today - datetime.timedelta(days=1)
    day_rows = {r.day: r
                for r in models.load_days(product, channel, start_total)}
    total_id = models.total_series(product, channel)
    series_by_id = {}
    refit_after = now - datetime.timedelta(
        hours=config.get('refit_hours', 6))
    if fits_budget is None:
        fits_budget = config.get('max_fits_per_run', 800)
    fits = 0

    # -- channel total: fit (refresh when stale) and intraday profile
    models_by_id = models.load_models([total_id])
    total_daily = models.load_daily([total_id], start_total).get(total_id,
                                                                 {})
    cached_total = None
    total_fit = None
    row = models_by_id.get(total_id)
    if row is not None and row.fitted_at >= refit_after and \
            row.last_day == yesterday:
        cached_total = Cached.from_row(row, comps)
    if cached_total is None or True:
        # the channel fit is cheap and is the prior of every signature:
        # always recompute it so borrowed factors are fresh
        total_fit, cached_total = fit_series(
            total_daily, day_rows, start_total, yesterday, None, 1, now,
            components=comps)
        models.upsert(models.Model, [cached_total.to_row(total_id)],
                      ['series_id'])
        if stale_fits:
            cached_total.fitted_at = now - datetime.timedelta(
                hours=config.get('refit_hours', 6))
    # volume floors: a share of the channel's expected day; in the
    # versioned scopes of the de-seasonalised level (the day after a
    # release expects a few percent of a normal day: a floor taken from it
    # would flag every two-crash signature)
    min_crashes, min_installs = calibration.volume_floors(
        cached_total.level if scope != config.SCOPE_ALL
        else cached_total.expected(today), config.volume_share())
    hourly_total = models.load_hourly(
        [total_id], [today - datetime.timedelta(days=i)
                     for i in range(PROFILE_HISTORY_DAYS + 1)]
    ).get(total_id, {})
    profile = intraday.build_profile(
        hourly_total, today,
        profile_days=config.get('profile_days', 28),
        weekday_days=config.get('profile_weekday_days', 8))
    # -- candidates
    recent = models.recent_max(product, channel,
                               today - datetime.timedelta(days=28))
    both = models.channel_daily(product, channel, [today, yesterday])
    candidates = set()
    for sid, days in both.items():
        if sid == total_id:
            continue
        t = days.get(today, (0, None))[0]
        y = days.get(yesterday, (0, None))[0]
        if t >= min_crashes or y >= min_crashes or \
                recent.get(sid, 0) >= min_crashes:
            candidates.add(sid)
    for sid, mx in recent.items():
        if mx >= min_crashes and sid != total_id:
            candidates.add(sid)
    candidates = sorted(candidates)
    series_by_id = models.load_series(candidates + [total_id])
    models_by_id = models.load_models(candidates)

    # -- fit the series without a cached model, refresh the stale cached
    #    ones oldest first within the budget (one batched history load)
    new_ids = [sid for sid in candidates if sid not in models_by_id]
    stale = [sid for sid in candidates if sid in models_by_id and
             (models_by_id[sid].fitted_at < refit_after or
              models_by_id[sid].last_day != yesterday)]
    stale.sort(key=lambda sid: models_by_id[sid].fitted_at)
    to_fit = new_ids + stale[:max(0, fits_budget)]
    fits_by_id = {}
    cached = {}
    if to_fit:
        histories = models.load_daily(to_fit, start, yesterday)
        fitted_at = now
        if stale_fits:
            fitted_at = now - datetime.timedelta(
                hours=config.get('refit_hours', 6))
        rows = []
        for sid in to_fit:
            fit, c = fit_series(histories.get(sid, {}), day_rows, start,
                                yesterday, total_fit, min_crashes, fitted_at,
                                components=comps)
            fits_by_id[sid] = fit
            cached[sid] = c
            rows.append(c.to_row(sid))
            fits += 1
        models.upsert(models.Model, rows, ['series_id'])
    for sid in candidates:
        if sid not in cached:
            cached[sid] = Cached.from_row(models_by_id[sid], comps)

    # -- thresholds learned from the channel's own data: the severity
    #    levels from the pooled one-step-ahead z of the candidates' fits
    #    (histograms cached with the models), the storm ratio from its
    #    crashes-per-install distribution
    calib = calibration.calibrate([cached[sid].z_hist for sid in candidates],
                                  config.alert_rate())
    rules = calib['rules']
    ratios = models.load_daily(candidates, today - datetime.timedelta(
        days=config.get('install_share_days', 28)), yesterday)
    calib['storm_ratio'] = calibration.storm_ratio(
        ratios, min_crashes, config.storm_quantile())
    calib['min_crashes'], calib['min_installs'] = min_crashes, min_installs
    calib['volume_share'] = config.volume_share()
    calib['storm_quantile'] = config.storm_quantile()
    ctx = Context(product, channel, today, as_of, profile, rules,
                  min_crashes, min_installs, storm_ratio=calib['storm_ratio'],
                  installs_as_of=day_row.installs_as_of)

    # -- realised pace of the channel (robust to processing lag)
    weekday = today.weekday()
    if profile is not None:
        elapsed = profile.fraction(weekday, ctx.hour)[0]
    else:
        elapsed = min(ctx.hour / 24.0, 1.0)
    paces = []
    ranked = sorted(candidates,
                    key=lambda sid: -cached[sid].expected(today))
    for sid in ranked[:50]:
        s = series_by_id[sid]
        e = cached[sid].expected(today)
        if s.noise or e < 50:
            continue
        obs = both.get(sid, {}).get(today, (0, None))[0]
        paces.append(obs / e)
    # the median of the top signatures is robust to a few spiking ones as
    # long as there are enough of them; a lag cannot plausibly move the
    # realised share by more than half the calendar share
    if len(paces) >= config.get('pace_min_signatures', 10) and \
            elapsed > 0.02:
        pace = float(np.median(paces))
        ctx.pace = min(max(pace, 0.5 * elapsed), 1.5 * elapsed)

    # -- previous scores (hysteresis, peaks)
    previous = {(sc.series_id, sc.day): sc
                for sc, _ in models.load_scores(product, channel,
                                                [today, yesterday])}
    hourly = models.load_hourly(candidates + [total_id], [today, yesterday])

    def hourly_of(sid, day):
        return hourly.get(sid, {}).get(day)

    rows = []
    sig_today = []
    for sid in candidates:
        s = series_by_id[sid]
        c = cached[sid]
        days = both.get(sid, {})
        r = score_today(ctx, s, c, days.get(today), hourly_of(sid, today),
                        hourly_of(sid, yesterday),
                        previous.get((sid, today)), False,
                        daily_yesterday=days.get(yesterday))
        rows.append(r)
        sig_today.append(r)
        ry = score_yesterday(ctx, s, c, fits_by_id.get(sid),
                             days.get(yesterday), previous.get((sid,
                                                                yesterday)))
        if ry is not None:
            rows.append(ry)
    total_days = both.get(total_id, {})
    total_row = score_today(ctx, series_by_id[total_id], cached_total,
                            total_days.get(today), hourly_of(total_id, today),
                            hourly_of(total_id, yesterday),
                            previous.get((total_id, today)), True,
                            daily_yesterday=total_days.get(yesterday))
    total_row['details']['drivers'] = drivers(total_row, sig_today,
                                              series_by_id)
    share = storm_share(total_row, sig_today)
    total_row['details']['storm_share'] = round(share, 3)
    watch_z = rules.get('watch', {}).get('z', 3.0)
    if share >= 0.5 and total_row['z'] is not None and \
            total_row['z'] >= watch_z:
        # the channel-level crash excess is mostly a few machines crashing
        # in a loop: not a spike of the channel (explains why the total is
        # not flagged although its crash count is)
        total_row['details']['storm_driven'] = True
        if total_row['severity'] in UPWARD:
            total_row['severity'] = 'ok'
            prev = previous.get((total_id, today))
            for k in ('first_flagged_at', 'peak_severity', 'peak_z',
                      'peak_excess', 'peak_at'):
                total_row[k] = getattr(prev, k) if prev is not None \
                    else None
    total_row['details']['components'] = cached_total.components
    total_row['details']['calibration'] = calib
    total_row['details']['profile_days'] = profile.ndays if profile else 0
    rows.append(total_row)
    ty = score_yesterday(ctx, series_by_id[total_id], cached_total, total_fit,
                         total_days.get(yesterday),
                         previous.get((total_id, yesterday)))
    if ty is not None:
        rows.append(ty)
    models.upsert(models.Score, rows, ['series_id', 'day'])

    counts = {k: 0 for k in ('major', 'spike', 'watch', 'drop', 'new',
                             'storm', 'noise')}
    for r in sig_today:
        s = series_by_id[r['series_id']]
        if s.noise:
            counts['noise'] += 1
            continue
        if r['severity'] in counts:
            counts[r['severity']] += 1
        if r['is_new']:
            counts['new'] += 1
        if r['storm']:
            counts['storm'] += 1
    counts['scored'] = len(sig_today)
    logger.info('Dashboard: %s/%s scored %d series (%d fits), total %s',
                product, channel, len(sig_today), fits, total_row['severity'])
    return {'product': product, 'channel': channel, 'scored': len(sig_today),
            'fits': fits, 'counts': counts, 'total': total_row,
            'pending_fits': max(0, len(stale) - max(0, fits_budget))}
