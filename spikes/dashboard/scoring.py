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
   end of day, gates the severity with the distinct-install counts, applies
   hysteresis and tracks the peak of the day;
4. scores *yesterday* as a complete day;
5. computes the drivers of the channel total's deviation.

Scores are upserted in place in ``dashboard_scores``.
"""

import datetime

import numpy as np

from spikes.logger import logger
from . import config, intraday, models, seasonal


RANK = {'ok': 0, 'drop': 1, 'watch': 2, 'spike': 3, 'major': 4}
UPWARD = ('watch', 'spike', 'major')
PROFILE_HISTORY_DAYS = 56


def severity_of(z, ratio, rules, expected=None, min_expected_drop=0.0):
    """Severity label from a score and the observed/expected ratio."""
    if z is None or not np.isfinite(z):
        return 'ok'
    ratio = float('inf') if ratio is None else ratio
    for label in ('major', 'spike', 'watch'):
        rule = rules.get(label)
        if rule and z >= rule['z'] and ratio >= rule['ratio']:
            return label
    rule = rules.get('drop')
    if rule and z <= rule['z'] and ratio <= rule['ratio'] and \
            (expected is None or expected >= min_expected_drop):
        return 'drop'
    return 'ok'


def worst(*labels):
    return max(labels, key=lambda s: RANK.get(s, 0))


def confidence(z):
    if z is None or not np.isfinite(z):
        return 0
    return sum(1 for t in (3.0, 5.0, 8.0) if abs(z) >= t)


def seasonal_at(factors, date):
    """Seasonal factor of *date* from a ``{name: [values]}`` dict."""
    s = 1.0
    for name, values in (factors or {}).items():
        comp = seasonal.BY_NAME.get(name)
        if comp is not None and values:
            s *= float(values[comp.phase([date])[0]])
    return s


class Cached:
    """Model parameters of a series (from a Fit or a Model row)."""

    def __init__(self, level, trend, c2, dispersion, factors, borrowed,
                 components, install_share, level_change_28, last_day,
                 history_days, recent_days_seen, fitted_at):
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

    @classmethod
    def from_fit(cls, fit, last_day, install_share, recent_days_seen,
                 fitted_at):
        s = fit.summary()
        return cls(fit.next_level, fit.next_slope, fit.c2, fit.dispersion,
                   s['factors'], s['borrowed'], s['components'],
                   install_share, fit.level_change(28), last_day,
                   s['history_days'], recent_days_seen, fitted_at)

    @classmethod
    def from_row(cls, row):
        details = row.components or {}
        return cls(row.level, row.trend, row.c2, row.dispersion,
                   row.factors, row.borrowed, details.get('components'),
                   row.install_share, row.level_change_28, row.last_day,
                   row.history_days, details.get('recent_days_seen', 1),
                   row.fitted_at)

    def to_row(self, series_id):
        return {'series_id': series_id, 'fitted_at': self.fitted_at,
                'last_day': self.last_day,
                'history_days': int(self.history_days),
                'level': float(self.level), 'trend': float(self.trend),
                'dispersion': float(self.dispersion), 'c2': float(self.c2),
                'install_share': self.install_share,
                'factors': self.factors, 'borrowed': list(self.borrowed),
                'components': {'components': self.components,
                               'recent_days_seen': self.recent_days_seen},
                'level_change_28': self.level_change_28}

    def expected(self, date):
        """Expected full-day count for *date* (after ``last_day``)."""
        if self.last_day is None:
            horizon = 1
        else:
            horizon = max(1, (date - self.last_day).days)
        level = max(0.0, self.level + self.trend * (horizon - 1))
        return level * seasonal_at(self.factors, date)


# --------------------------------------------------------------------------
# History loading and fitting
# --------------------------------------------------------------------------

def censored_value(day_row):
    """Imputed count of a signature absent from a day's top list."""
    if day_row is None:
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


def fit_series(daily, day_rows, start, end, prior, min_crashes, now):
    """Fit one series and return a :class:`Cached`."""
    dates, y, installs = build_history(daily, day_rows, start, end)
    fit = seasonal.fit(dates, y, level_window=config.get('level_window', 14),
                       prior=prior,
                       own_min=config.get('own_factors_min_crashes', 10),
                       trend_min_level=config.get('trend_min_level', 50))
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
                 installs_as_of=None):
        self.product = product
        self.channel = channel
        self.today = today
        self.yesterday = today - datetime.timedelta(days=1)
        self.as_of = as_of
        # distinct installs are refreshed less often than the counts
        self.installs_as_of = installs_as_of or as_of
        self.profile = profile
        self.rules = rules
        self.min_crashes = config.min_crashes(channel, product)
        self.min_installs = config.min_installs(channel, product)
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


def score_today(ctx, series, cached, daily_today, hourly_today,
                hourly_yesterday, previous, is_total):
    """Score one series for the current (partial) day."""
    observed = daily_today[0] if daily_today else 0
    installs = daily_today[1] if daily_today else None
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
    min_drop = config.get('min_expected_drop', 20)
    sev = severity_of(z, ratio, ctx.rules, expected, min_drop)
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
        if (installs <= config.get('storm_max_installs', 5) and
                per_install >= config.get('storm_min_ratio', 5)) or \
                per_install >= config.get('storm_loop_ratio', 20):
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
    if sev in UPWARD and installs is not None:
        if installs < ctx.min_installs:
            sev = 'ok'
        elif z_inst is not None:
            r_inst = installs / e_inst if e_inst > 0 else None
            sev_inst = severity_of(z_inst, r_inst, ctx.rules)
            if RANK[sev_inst] < RANK[sev]:
                sev = sev_inst if sev_inst in UPWARD else 'ok'
    elif sev in UPWARD and installs is None:
        # installs unknown (not fetched yet): they cannot pass the gate
        sev = 'ok'
    if observed < ctx.min_crashes and sev in UPWARD:
        sev = 'ok'
    is_new = bool(not is_total and cached.recent_days_seen == 0 and
                  observed >= ctx.min_crashes and
                  installs is not None and installs >= ctx.min_installs)
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
                    if ctx.installs_as_of else None},
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
    sev = severity_of(z, ratio, ctx.rules, expected,
                      config.get('min_expected_drop', 20))
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
        for k in ('first_flagged_at', 'peak_severity', 'peak_z',
                  'peak_excess', 'peak_at', 'projected', 'projected_lo',
                  'projected_hi', 'recent_hours', 'observed_recent',
                  'expected_recent', 'z_recent'):
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
    rules = config.severity_rules()
    day_row = models.get_day(product, channel, today)
    if day_row is None or day_row.as_of is None:
        return None
    as_of = day_row.as_of
    history_days = config.get('history_days', 180)
    start = today - datetime.timedelta(days=history_days)
    yesterday = today - datetime.timedelta(days=1)
    day_rows = {r.day: r for r in models.load_days(product, channel, start)}
    total_id = models.total_series(product, channel)
    series_by_id = {}
    refit_after = now - datetime.timedelta(
        hours=config.get('refit_hours', 6))
    if fits_budget is None:
        fits_budget = config.get('max_fits_per_run', 800)
    fits = 0

    # -- channel total: fit (refresh when stale) and intraday profile
    models_by_id = models.load_models([total_id])
    total_daily = models.load_daily([total_id], start).get(total_id, {})
    cached_total = None
    total_fit = None
    row = models_by_id.get(total_id)
    if row is not None and row.fitted_at >= refit_after and \
            row.last_day == yesterday:
        cached_total = Cached.from_row(row)
    if cached_total is None or True:
        # the channel fit is cheap and is the prior of every signature:
        # always recompute it so borrowed factors are fresh
        total_fit, cached_total = fit_series(
            total_daily, day_rows, start, yesterday, None,
            config.min_crashes(channel, product), now)
        models.upsert(models.Model, [cached_total.to_row(total_id)],
                      ['series_id'])
        if stale_fits:
            cached_total.fitted_at = now - datetime.timedelta(
                hours=config.get('refit_hours', 6))
    hourly_total = models.load_hourly(
        [total_id], [today - datetime.timedelta(days=i)
                     for i in range(PROFILE_HISTORY_DAYS + 1)]
    ).get(total_id, {})
    profile = intraday.build_profile(
        hourly_total, today,
        profile_days=config.get('profile_days', 28),
        weekday_days=config.get('profile_weekday_days', 8))
    ctx = Context(product, channel, today, as_of, profile, rules,
                  installs_as_of=day_row.installs_as_of)

    # -- candidates
    min_crashes = ctx.min_crashes
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
                                yesterday, total_fit, min_crashes, fitted_at)
            fits_by_id[sid] = fit
            cached[sid] = c
            rows.append(c.to_row(sid))
            fits += 1
        models.upsert(models.Model, rows, ['series_id'])
    for sid in candidates:
        if sid not in cached:
            cached[sid] = Cached.from_row(models_by_id[sid])

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
                        previous.get((sid, today)), False)
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
                            previous.get((total_id, today)), True)
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
