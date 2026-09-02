# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Flask blueprint: ``dashboard.html`` and the JSON API (see API.md)."""

import datetime
import gzip
import hashlib
import math
import time

import numpy as np
import requests
from flask import Blueprint, Response, jsonify, render_template, request

from spikes.logger import logger
from . import config, intraday, models, scoring, seasonal, socorro


blueprint = Blueprint('dashboard', __name__, template_folder='templates',
                      static_folder='static',
                      static_url_path='/dashboard/static')

ALERT_SEVERITIES = ('major', 'spike', 'watch', 'drop')
PRODUCT_DETAILS = 'https://product-details.mozilla.org/1.0/'
RELEASES_URL = PRODUCT_DETAILS + 'firefox_history_major_releases.json'
STABILITY_URL = PRODUCT_DETAILS + 'firefox_history_stability_releases.json'
VERSIONS_URL = PRODUCT_DETAILS + 'firefox_versions.json'
_releases_cache = {'at': 0.0, 'data': [], 'esr': []}


class BadRequest(Exception):
    pass


_tables_ready = False


@blueprint.before_request
def ensure_tables():
    """Create the dashboard tables once per web process, so the API works
    before the scheduler's first run (a fresh database)."""
    global _tables_ready
    if _tables_ready:
        return
    try:
        models.create_all()
        _tables_ready = True
    except Exception as ex:  # another process may be creating them
        from spikes import db
        db.session.rollback()
        logger.warning('Dashboard: could not create tables: %s', ex)


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------

def num(x, digits=None):
    """JSON-safe number (None for NaN/inf)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    if digits is not None:
        x = round(x, digits)
    return x


def ts(dt):
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + 'Z'


def day_str(d):
    return d.isoformat() if d is not None else None


def score_json(score, series, final=None):
    """Serialize a :class:`models.Score` row (see API.md ``Score``)."""
    details = score.details or {}
    recent = None
    if score.expected_recent is not None:
        # z is null when the window has too few expected crashes to score
        e = score.expected_recent
        o = score.observed_recent or 0
        recent = {'hours': score.recent_hours, 'observed': o,
                  'expected': num(e, 2), 'z': num(score.z_recent, 2),
                  'excess': num(o - e, 1),
                  'ratio': num(o / e, 3) if e else None}
    peak = None
    if score.peak_severity:
        peak = {'severity': score.peak_severity, 'z': num(score.peak_z, 2),
                'excess': num(score.peak_excess, 1),
                'at': ts(score.peak_at)}
    res = {
        'day': day_str(score.day), 'as_of': ts(score.as_of),
        'partial': bool(score.partial),
        'elapsed_fraction': num(score.elapsed, 4),
        'observed': int(score.observed or 0),
        'expected': num(score.expected, 2),
        'expected_day': num(score.expected_day, 2),
        'excess': num(score.excess, 1), 'ratio': num(score.ratio, 3),
        'z': num(score.z, 2), 'confidence': scoring.confidence(score.z),
        'projected': num(score.projected, 1),
        'projected_lo': num(score.projected_lo, 1),
        'projected_hi': num(score.projected_hi, 1),
        'recent': recent, 'recent_reason': score.recent_reason,
        'installs': score.installs,
        'expected_installs': num(score.expected_installs, 2),
        'z_installs': num(score.z_installs, 2),
        'installs_ratio': num(details.get('installs_ratio')
                              if details.get('installs_ratio') is not None
                              else (score.observed / score.installs
                                    if score.installs else None), 2),
        'installs_as_of': details.get('installs_as_of'),
        'storm': bool(score.storm), 'severity': score.severity,
        'is_new': bool(score.is_new), 'noise': bool(series.noise),
        'since': ts(score.first_flagged_at), 'peak': peak,
        'level': num(details.get('level'), 2),
        'dispersion': num(details.get('dispersion'), 3),
        'level_change_28': num(details.get('level_change_28'), 3),
    }
    if final is not None:
        res['final'] = bool(final)
    if series.is_total:
        res['drivers'] = details.get('drivers', [])
        res['storm_share'] = details.get('storm_share', 0.0)
        res['storm_driven'] = bool(details.get('storm_driven'))
    return res


def row_json(score, series, product, channel, spark=None, yesterday=None,
             flagged_days=0):
    res = score_json(score, series)
    res.update({
        'signature': series.signature, 'product': product,
        'channel': channel, 'series_id': series.id,
        'socorro_url': socorro.link(product, channel, score.day,
                                    series.signature),
        'bugs': {'open': series.bug_open, 'closed': series.bug_closed},
        'first_seen': day_str(series.first_seen),
        'flagged_days': flagged_days,
        'yesterday': yesterday, 'spark': spark,
    })
    return res


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------

def today_utc():
    """The day the scores describe: the latest scored day.

    Between 00:00 UTC and the first run of a new day the latest scores are
    yesterday's, so the page keeps showing them (flagged ``partial``)
    instead of going blank.
    """
    import sqlalchemy as sa
    from spikes import db
    try:
        latest = db.session.execute(
            sa.select(sa.func.max(models.Score.day))).scalar_one()
    except Exception:  # tables not created yet
        db.session.rollback()
        latest = None
    return latest or models.utctoday()


def parse_args(args):
    product = args.get('product', 'Firefox')
    channel = args.get('channel', 'release')
    if product not in config.products():
        raise BadRequest('unknown product')
    if channel not in config.channels(product):
        raise BadRequest('unknown channel for this product')
    try:
        days = int(args.get('days', 90))
    except ValueError:
        raise BadRequest('days must be an integer')
    days = min(max(days, 7), 730)
    granularity = args.get('granularity', 'day')
    if granularity not in ('day', 'week'):
        raise BadRequest('granularity must be day or week')
    return product, channel, days, granularity


def _fetch_json(url):
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def _load_releases():
    """Firefox major releases and ESR point releases (product-details)."""
    major = _fetch_json(RELEASES_URL)
    data = [{'version': v, 'date': d}
            for v, d in sorted(major.items(), key=lambda p: p[1])]
    # the ESR trains are the majors of FIREFOX_ESR* in firefox_versions;
    # their point releases are in the stability list without a suffix
    esr = []
    try:
        versions = _fetch_json(VERSIONS_URL)
        # the current ESR and the next one (not the legacy ESR115 train)
        majors = {versions[k].split('.')[0]
                  for k in ('FIREFOX_ESR', 'FIREFOX_ESR_NEXT')
                  if versions.get(k)}
        stability = _fetch_json(STABILITY_URL)
        # ESR point releases are X.N.0 (N >= 1); X.0.y are the release
        # channel's own dot releases of that major
        points = {}
        for k, d in stability.items():
            parts = k.split('.')
            if parts[0] in majors and len(parts) > 1 and \
                    parts[1].isdigit() and int(parts[1]) >= 1:
                points[k] = d
        for k, d in major.items():
            if k.split('.')[0] in majors:
                points.setdefault(k, d)
        # both trains ship the same day: one marker, e.g. "140.14 / 153.1 esr"
        by_date = {}
        for v, d in points.items():
            short = v[:-2] if v.endswith('.0') and v.count('.') == 2 else v
            by_date.setdefault(d, []).append(short)
        esr = [{'version': ' / '.join(sorted(vs, key=lambda x: [
            int(p) for p in x.split('.')])) + ' esr', 'date': d}
            for d, vs in sorted(by_date.items())]
    except Exception as ex:  # majors are better than nothing
        logger.warning('Dashboard: ESR releases unavailable: %s', ex)
    return data, esr


def releases(since=None, channel=None):
    """Release dates to mark on the charts (cached for a day).

    Firefox major releases (merge/ship days, meaningful for nightly, beta,
    release and Fenix); the ESR channel gets the ESR point releases.
    """
    now = time.time()
    if now - _releases_cache['at'] > 86400:
        try:
            data, esr = _load_releases()
            _releases_cache.update(at=now, data=data, esr=esr)
        except Exception as ex:  # the dashboard works without it
            logger.warning('Dashboard: releases unavailable: %s', ex)
            _releases_cache['at'] = now - 86400 + 600
    data = _releases_cache['esr' if channel == 'esr' else 'data']
    if since is not None:
        data = [x for x in data if x['date'] >= since.isoformat()]
    return data


def last_run():
    """The latest completed run (a running one has no results yet)."""
    try:
        runs = models.last_runs(1, finished_only=True)
    except Exception:  # tables not created yet
        from spikes import db
        db.session.rollback()
        return None
    return runs[0] if runs else None


def data_health(now, run, channels, check_count=True):
    if run is None:
        return {'status': 'backfilling', 'since': None,
                'detail': 'No run yet'}
    import json
    info = json.loads(run.message or '{}') if run.message else {}
    finished = run.finished or run.started
    age = (now - finished).total_seconds() / 60.0
    if run_is_stale(now, run):
        return {'status': 'stale_local', 'since': ts(finished),
                'detail': 'Last run {} {:.0f} min ago'.format(run.status,
                                                                age)}
    expected_channels = len(config.pairs())
    missing = expected_channels - len(channels) if check_count else 0
    if info.get('errors') or missing > 0:
        detail = '; '.join(info.get('errors', []))
        if missing:
            detail = ('{} channel(s) without scores. '.format(missing) +
                      detail).strip()
        return {'status': 'stale_local', 'since': ts(finished),
                'detail': detail}
    if run.lag_suspected:
        return {'status': 'stale_upstream', 'since': ts(run.started),
                'detail': 'Most channels are below expectation at once: '
                          'Socorro processing is probably late; drops are '
                          'hidden'}
    if info.get('pending_units'):
        return {'status': 'backfilling', 'since': ts(run.started),
                'detail': 'Backfilling history: {} fetches pending'.format(
                    info['pending_units'])}
    return {'status': 'ok', 'since': None, 'detail': None}


def thresholds():
    return config.severity_rules()


def channel_day(product, channel, today):
    """The day the channel's latest scores describe (normally *today*)."""
    import sqlalchemy as sa
    from spikes import db
    latest = db.session.execute(sa.select(sa.func.max(models.Score.day)).join(
        models.Series, models.Series.id == models.Score.series_id).where(
        models.Series.product == product,
        models.Series.channel == channel)).scalar_one()
    return latest or today


def channel_scores(product, channel, today):
    """Today's and yesterday's scores of a channel keyed by series id."""
    yesterday = today - datetime.timedelta(days=1)
    scores = models.load_scores(product, channel, [today, yesterday])
    by_series = {}
    for score, series in scores:
        entry = by_series.setdefault(series.id, {'series': series})
        entry['today' if score.day == today else 'yesterday'] = score
    return by_series


def sparks(product, channel, series_ids, today, cached_models, ndays=28):
    """28-day sparklines.  A day without a stored row is a day under the
    channel's top-N cut: shown as the censored value the model uses (0, or
    half the cutoff), not as a hole; only days the channel has no data for
    are null."""
    start = today - datetime.timedelta(days=ndays - 1)
    dates = [start + datetime.timedelta(days=i) for i in range(ndays)]
    daily = models.load_daily(series_ids, start, today)
    day_rows = {r.day: r for r in models.load_days(product, channel, start,
                                                   today)}
    res = {}
    for sid in series_ids:
        rows = daily.get(sid, {})
        model = cached_models.get(sid)
        observed, expected = [], []
        for d in dates:
            if d in rows:
                observed.append(rows[d][0])
            else:
                observed.append(num(scoring.censored_value(day_rows.get(d)),
                                    1))
            expected.append(num(model.expected(d), 1) if model else None)
        res[sid] = {'dates': [day_str(d) for d in dates],
                    'observed': observed, 'expected': expected}
    return res


def flagged_days_map(series_ids, today, ndays=7):
    """Consecutive previous days (back from yesterday) flagged >= watch."""
    since = today - datetime.timedelta(days=ndays)
    import sqlalchemy as sa
    from spikes import db
    q = sa.select(models.Score.series_id, models.Score.day).where(
        models.Score.series_id.in_(list(series_ids)),
        models.Score.day >= since, models.Score.day < today,
        models.Score.peak_severity.in_(list(scoring.UPWARD)))
    flagged = {}
    for sid, day in db.session.execute(q):
        flagged.setdefault(sid, set()).add(day)
    res = {}
    for sid, days in flagged.items():
        n = 0
        d = today - datetime.timedelta(days=1)
        while d in days:
            n += 1
            d -= datetime.timedelta(days=1)
        res[sid] = n
    return res


def yesterday_json(score, final):
    if score is None:
        return None
    return {'observed': int(score.observed or 0),
            'expected': num(score.expected, 2), 'z': num(score.z, 2),
            'severity': score.severity, 'final': bool(final)}


def rows_json(product, channel, by_series, today, with_yesterday_final,
              only_ids=None):
    ids = [sid for sid, e in by_series.items()
           if 'today' in e and not e['series'].is_total and
           (only_ids is None or sid in only_ids)]
    cached = {sid: scoring.Cached.from_row(m)
              for sid, m in models.load_models(ids).items()}
    spark = sparks(product, channel, ids, today, cached)
    flagged = flagged_days_map(ids, today)
    rows = []
    for sid in ids:
        e = by_series[sid]
        rows.append(row_json(e['today'], e['series'], product, channel,
                             spark=spark.get(sid),
                             yesterday=yesterday_json(e.get('yesterday'),
                                                      with_yesterday_final),
                             flagged_days=flagged.get(sid, 0)))
    return rows


def sort_key(row):
    return (-scoring.RANK.get(row['severity'], 0), -(row['excess'] or 0))


def counts_of(rows):
    counts = {k: 0 for k in ('major', 'spike', 'watch', 'drop', 'new',
                             'storm', 'noise')}
    for r in rows:
        if r['noise']:
            counts['noise'] += 1
            continue
        if r['severity'] in counts:
            counts[r['severity']] += 1
        if r['is_new']:
            counts['new'] += 1
        if r['storm']:
            counts['storm'] += 1
    counts['scored'] = len(rows)
    return counts


def day_final(product, channel, day):
    row = models.get_day(product, channel, day)
    return bool(row and row.final)


def channel_summary(product, channel, today, now):
    total_id = models.total_series(product, channel, create=False)
    if total_id is None:
        return None
    today = channel_day(product, channel, today)
    by_series = channel_scores(product, channel, today)
    entry = by_series.get(total_id)
    if entry is None or 'today' not in entry:
        return None
    yesterday = today - datetime.timedelta(days=1)
    rows = rows_json(product, channel, by_series, today,
                     day_final(product, channel, yesterday))
    model = models.load_models([total_id]).get(total_id)
    return {
        'product': product, 'channel': channel, 'day': day_str(today),
        'as_of': ts(entry['today'].as_of),
        'history_days': model.history_days if model else 0,
        'total': score_json(entry['today'], entry['series']),
        'yesterday': score_json(entry['yesterday'], entry['series'],
                                day_final(product, channel, yesterday))
        if 'yesterday' in entry else None,
        'counts': counts_of(rows),
        '_rows': rows,
    }


# --------------------------------------------------------------------------
# Series blocks (charts)
# --------------------------------------------------------------------------

def daily_block(product, channel, series_id, today, days, granularity,
                score_today, prior_fit=None):
    """Recompute the fit of a series and return the ``daily`` block and
    the fitted model (see API.md)."""
    history_days = max(days, config.get('history_days', 180))
    start = today - datetime.timedelta(days=history_days)
    yesterday = today - datetime.timedelta(days=1)
    day_rows = {r.day: r for r in models.load_days(product, channel, start)}
    daily = models.load_daily([series_id], start, today).get(series_id, {})
    dates, y, _ = scoring.build_history(daily, day_rows, start, yesterday)
    fit = seasonal.fit(dates, y, level_window=config.get('level_window', 14),
                       prior=prior_fit,
                       own_min=config.get('own_factors_min_crashes', 10),
                       trend_min_level=config.get('trend_min_level', 50))
    c2 = fit.c2
    # append today (partial)
    exp_today = fit.forecast(today)
    all_dates = dates + [today]
    observed = list(y) + [daily.get(today, (np.nan, None))[0]]
    expected = list(fit.expected) + [exp_today]
    zs = list(fit.z) + [score_today.z if score_today else None]
    cut = max(0, len(all_dates) - days)
    all_dates, observed, expected, zs = (all_dates[cut:], observed[cut:],
                                         expected[cut:], zs[cut:])
    rules = thresholds()
    if granularity == 'week':
        agg = seasonal.aggregate_weekly(all_dates, observed, expected, c2)
        # the in-progress week: its expectation and band stay full-day
        # (consistent with `projected`), but its score compares today's
        # count so far with today's expectation so far
        if score_today is not None and score_today.expected is not None \
                and agg and all_dates[-1] >= today:
            sofar = list(expected)
            sofar[-1] = score_today.expected
            agg[-1]['z'] = seasonal.aggregate_weekly(
                all_dates, observed, sofar, c2)[-1]['z']
        # a leading week cut by the requested range is not a full week
        while len(agg) > 1 and agg[0]['ndays'] < 7:
            agg.pop(0)
        block = {'granularity': 'week',
                 'start': [day_str(a['start']) for a in agg],
                 'observed': [num(a['observed']) for a in agg],
                 'expected': [num(a['expected'], 2) for a in agg],
                 'lo3': [num(a['lo3'], 1) for a in agg],
                 'hi3': [num(a['hi3'], 1) for a in agg],
                 'lo5': [num(a['lo5'], 1) for a in agg],
                 'hi5': [num(a['hi5'], 1) for a in agg],
                 'z': [num(a['z'], 2) for a in agg],
                 'partial': [a['start'] + datetime.timedelta(days=6) >= today
                             for a in agg],
                 'projected': [None] * len(agg)}
        block['severity'] = [scoring.severity_of(a['z'], None, rules)
                             if a['z'] is not None else 'ok' for a in agg]
        if block['partial'] and block['partial'][-1] and score_today and agg:
            block['severity'][-1] = score_today.severity
            if score_today.projected is not None:
                last = agg[-1]
                done = (last['observed'] or 0) - \
                    (daily.get(today, (0,))[0] or 0)
                block['projected'][-1] = num(done + score_today.projected, 1)
    else:
        e = np.array([np.nan if v is None else v for v in expected],
                     dtype=np.float64)
        lo3, hi3 = seasonal.band(e, 3, c2)
        lo5, hi5 = seasonal.band(e, 5, c2)
        block = {'granularity': 'day',
                 'start': [day_str(d) for d in all_dates],
                 'observed': [num(v) for v in observed],
                 'expected': [num(v, 2) for v in expected],
                 'lo3': [num(v, 1) for v in lo3],
                 'hi3': [num(v, 1) for v in hi3],
                 'lo5': [num(v, 1) for v in lo5],
                 'hi5': [num(v, 1) for v in hi5],
                 'z': [num(v, 2) for v in zs],
                 'partial': [d >= today for d in all_dates],
                 'projected': [None] * len(all_dates)}
        block['severity'] = [scoring.severity_of(z, None, rules)
                             if z is not None else 'ok' for z in block['z']]
        if score_today is not None:
            block['projected'][-1] = num(score_today.projected, 1)
            block['severity'][-1] = score_today.severity
    return block, fit


def model_block(fit, today, cached=None, borrowed=None):
    s = fit.summary()
    return {'level': num(fit.next_level, 2),
            'dispersion': num(fit.dispersion, 3), 'c2': num(fit.c2, 6),
            'history_days': s['history_days'],
            'components': s['components'], 'factors': s['factors'],
            'today_factors': fit.factors_at(today),
            'cycle_day': int(seasonal.cycle_phase([today])[0]) + 1,
            'borrowed': s['borrowed']}


def hourly_block(product, channel, series_id, today, expected_today,
                 expected_yesterday, as_of):
    yesterday = today - datetime.timedelta(days=1)
    total_id = models.total_series(product, channel, create=False)
    hist_days = [today - datetime.timedelta(days=i)
                 for i in range(scoring.PROFILE_HISTORY_DAYS + 1)]
    total_hourly = models.load_hourly([total_id], hist_days).get(
        total_id, {}) if total_id is not None else {}
    profile = intraday.build_profile(
        total_hourly, today, profile_days=config.get('profile_days', 28),
        weekday_days=config.get('profile_weekday_days', 8))
    own = models.load_hourly([series_id], [today, yesterday]).get(series_id,
                                                                  {})
    hours = list(range(24))
    in_progress = None
    if as_of is not None and as_of.date() == today:
        in_progress = as_of.hour
    t = own.get(today)
    if t is not None and in_progress is not None:
        t = [v if h <= in_progress else None for h, v in enumerate(t)]
    res = {'hours': hours, 'today': t, 'yesterday': own.get(yesterday),
           'in_progress_hour': in_progress,
           'profile_source': profile.source if profile else None}
    if profile is not None:
        res['expected_today'] = [
            num(v, 2) for v in profile.hourly_expected(today.weekday(),
                                                       expected_today)]
        res['expected_yesterday'] = [
            num(v, 2) for v in profile.hourly_expected(yesterday.weekday(),
                                                       expected_yesterday)]
    else:
        res['expected_today'] = res['expected_yesterday'] = None
    return res


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@blueprint.errorhandler(BadRequest)
def bad_request(ex):
    return jsonify({'error': str(ex)}), 400


def run_is_stale(now, run):
    """The clock alone can turn the data stale (no new run)."""
    if run is None:
        return False
    finished = run.finished or run.started
    return run.status == 'failed' or \
        (now - finished).total_seconds() > 30 * 60


def data_version(run, now=None):
    """Token that changes whenever a run has produced new scores, or the
    data went stale (so the client re-renders its health banner)."""
    if run is None:
        return None
    now = now or models.utcnow()
    return '{}-{}-{}'.format(run.id, ts(run.finished or run.started),
                             'stale' if run_is_stale(now, run) else 'ok')


def etag_for(run, parts, now=None):
    """ETag from the data version and a digest of the request parts (no
    raw request string, which may contain quotes, ends up in the header)."""
    version = data_version(run, now)
    if version is None:
        return None
    key = '\x00'.join(str(p) for p in parts).encode('utf-8')
    return '{}-{}'.format(version, hashlib.sha1(key).hexdigest()[:16])


def conditional(run, *parts, now=None):
    """304 response when the client already has this version, else None.

    The page polls every few minutes; between two runs nothing changes,
    so an unchanged version costs a 304 instead of a few hundred KB.
    """
    etag = etag_for(run, parts, now)
    if etag is not None and request.if_none_match.contains(etag):
        response = Response(status=304)
        response.set_etag(etag)
        return response
    return None


def versioned(payload, run, *parts, now=None):
    """JSON response carrying the data version as ETag and in the body."""
    payload['data_version'] = data_version(run, now)
    response = jsonify(payload)
    etag = etag_for(run, parts, now)
    if etag is not None:
        response.set_etag(etag)
    response.headers['Cache-Control'] = 'no-cache, private'
    return response


@blueprint.after_request
def compress(response):
    """gzip JSON responses (Heroku does not compress; a channel payload
    shrinks from ~350 KB to ~60 KB)."""
    if response.status_code != 200 or response.direct_passthrough or \
            not response.mimetype == 'application/json' or \
            'gzip' not in request.headers.get('Accept-Encoding', '') or \
            response.content_length is not None and \
            response.content_length < 1024:
        return response
    data = gzip.compress(response.get_data(), compresslevel=6)
    response.set_data(data)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = str(len(data))
    response.headers.add('Vary', 'Accept-Encoding')
    return response


@blueprint.route('/dashboard.html')
def html():
    return render_template('dashboard.html')


@blueprint.route('/dashboard/api/summary')
def summary():
    now = models.utcnow()
    run = last_run()
    not_modified = conditional(run, now=now)
    if not_modified is not None:
        return not_modified
    today = today_utc()
    channels = []
    alerts = []
    as_of = None
    for product, channel in config.pairs():
        s = channel_summary(product, channel, today, now)
        if s is None:
            continue
        rows = s.pop('_rows')
        alerts.extend(r for r in rows if not r['noise'] and
                      (r['severity'] in ALERT_SEVERITIES or r['is_new']))
        channels.append(s)
        if s['as_of'] and (as_of is None or s['as_of'] > as_of):
            as_of = s['as_of']
    health = data_health(now, run, channels)
    if health['status'] == 'stale_upstream':
        alerts = [a for a in alerts if a['severity'] != 'drop']
    alerts.sort(key=sort_key)
    import json
    info = json.loads(run.message or '{}') if run and run.message else {}
    return versioned({
        'now': ts(now), 'as_of': as_of,
        'last_run': {'started': ts(run.started), 'finished': ts(run.finished),
                     'status': run.status, 'queries': run.queries,
                     'failures': run.failures,
                     'message': info.get('error') or (
                         '; '.join(info['errors']) if info.get('errors')
                         else None),
                     'lag_suspected': bool(run.lag_suspected)}
        if run else None,
        'data_health': health, 'thresholds': thresholds(),
        'channels': channels, 'alerts': alerts[:50],
        'releases': releases(today - datetime.timedelta(days=730)),
    }, run, now=now)


@blueprint.route('/dashboard/api/channel')
def channel_view():
    product, channel, days, granularity = parse_args(request.args)
    now = models.utcnow()
    run = last_run()
    not_modified = conditional(run, product, channel, days, granularity,
                               now=now)
    if not_modified is not None:
        return not_modified
    today = today_utc()
    s = channel_summary(product, channel, today, now)
    if s is None:
        return jsonify({'error': 'no data for this channel yet'}), 404
    rows = s.pop('_rows')
    rows.sort(key=sort_key)
    today = datetime.date.fromisoformat(s['day'])
    total_id = models.total_series(product, channel, create=False)
    by_series = channel_scores(product, channel, today)
    total_score = by_series[total_id]['today']
    daily, fit = daily_block(product, channel, total_id, today, days,
                             granularity, total_score)
    s.update({
        'daily': daily,
        'model': model_block(fit, today),
        'hourly': hourly_block(product, channel, total_id, today,
                               fit.forecast(today),
                               float(fit.expected[-1]) if fit.ndays and
                               np.isfinite(fit.expected[-1]) else
                               fit.forecast(today), total_score.as_of),
        'signatures': rows, 'releases': releases(
            today - datetime.timedelta(days=days), channel),
        'thresholds': thresholds(),
        'data_health': data_health(now, run, [s], check_count=False),
    })
    return versioned(s, run, product, channel, days, granularity, now=now)


@blueprint.route('/dashboard/api/signature')
def signature_view():
    product, channel, days, granularity = parse_args(request.args)
    signature = request.args.get('signature')
    if not signature:
        raise BadRequest('signature is required')
    now = models.utcnow()
    run = last_run()
    not_modified = conditional(run, product, channel, days, granularity,
                               signature, now=now)
    if not_modified is not None:
        return not_modified
    today = channel_day(product, channel, today_utc())
    series = models.get_series(product, channel, signature)
    if series is None:
        return jsonify({'error': 'unknown signature'}), 404
    by_series = channel_scores(product, channel, today)
    entry = by_series.get(series.id)
    if entry is None or 'today' not in entry:
        return jsonify({'error': 'signature not scored today'}), 404
    total_id = models.total_series(product, channel, create=False)
    _, prior = daily_block(product, channel, total_id, today, days, 'day',
                           by_series.get(total_id, {}).get('today'))
    daily, fit = daily_block(product, channel, series.id, today, days,
                             granularity, entry['today'], prior_fit=prior)
    yesterday = today - datetime.timedelta(days=1)
    rows = rows_json(product, channel, by_series, today,
                     day_final(product, channel, yesterday),
                     only_ids={series.id})
    e_yday = float(fit.expected[-1]) if fit.ndays and \
        np.isfinite(fit.expected[-1]) else fit.forecast(today)
    return versioned({
        'row': rows[0] if rows else None,
        'daily': daily, 'model': model_block(fit, today),
        'hourly': hourly_block(product, channel, series.id, today,
                               fit.forecast(today), e_yday,
                               entry['today'].as_of),
        'releases': releases(today - datetime.timedelta(days=days), channel),
    }, run, product, channel, days, granularity, signature, now=now)
