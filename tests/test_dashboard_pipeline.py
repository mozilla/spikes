# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""End-to-end tests of the dashboard pipeline on an in-memory SQLite DB.

The Socorro responses are fixtures or synthetic; nothing touches the
network.
"""

import copy
import datetime
import json
import os
import unittest
from unittest import mock

import numpy as np

from spikes import app, db
from spikes.dashboard import api, collect, config, models, scoring, update
from spikes.dashboard import socorro, versions


FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
TODAY = datetime.date(2026, 9, 2)
NOW = datetime.datetime(2026, 9, 2, 12, 0, 0)
WEEKLY = np.array([1.15, 1.1, 1.1, 1.05, 1.0, 0.8, 0.8])


def load(name):
    with open(os.path.join(FIXTURES, name)) as In:
        return json.load(In)


class FakeFetcher:
    """Serves canned responses per query kind."""

    def __init__(self, responses):
        self.responses = responses
        self.count = 0
        self.failures = 0

    def remaining(self):
        return 1000

    def can_run(self, n=1):
        return True

    @staticmethod
    def kind_of(params):
        hist = params.get('_histogram.date')
        if '_aggs.product' in params:
            return 'day' if hist else 'installs'
        if params.get('_histogram_interval.date') == '1d':
            return 'daily'
        if hist == 'product':
            return 'hourly_total'
        return 'recent'

    def run(self, jobs):
        for params, cb in jobs:
            kind = self.kind_of(params)
            self.count += 1
            if self.responses.get(kind) is None:
                self.failures += 1
                continue
            try:
                cb(self.responses[kind])
            except Exception:  # like Fetcher._run_batch: a bad response
                self.failures += 1
        return len(jobs), len(jobs)


class DBTestCase(unittest.TestCase):
    """Tests that drop and recreate the dashboard tables.

    They refuse to run against a configured database (``DATABASE_URL``)
    unless ``DASHBOARD_TEST_ALLOW_DB=1`` is set, to protect real data.
    """

    def setUp(self):
        if os.environ.get('DATABASE_URL') and \
                not os.environ.get('DASHBOARD_TEST_ALLOW_DB'):
            self.skipTest('DATABASE_URL is set; refusing to drop its tables'
                          ' (set DASHBOARD_TEST_ALLOW_DB=1 to allow)')
        self.ctx = app.app_context()
        self.ctx.push()
        models.drop_all()
        models.create_all()
        versions._cache.clear()  # cycles cached by a previous test's DB
        api._channel_memo.clear()  # payloads of a previous test's DB

    def tearDown(self):
        db.session.rollback()
        models.drop_all()
        self.ctx.pop()


class StaticAssetTest(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_images_and_fonts_are_cached_immutably(self):
        paths = ('/favicon.ico',
                 '/dashboard/static/favicon.png',
                 '/dashboard/static/logo-firefox.svg',
                 '/dashboard/static/ZillaSlabHighlight-Bold.woff2')
        for path in paths:
            with self.subTest(path=path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('Cache-Control'),
                                 'public, max-age=31536000, immutable')

    def test_code_assets_are_not_immutable(self):
        for name in ('dashboard.css', 'dashboard.js'):
            with self.subTest(name=name), self.client.get(
                    f'/dashboard/static/{name}') as response:
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('immutable',
                                 response.headers.get('Cache-Control', ''))


class ConfigTest(unittest.TestCase):

    def test_channels_per_product(self):
        from spikes.dashboard import config
        previous = config.override(
            products=['Firefox', 'Fenix'],
            channels={'Firefox': ['nightly', 'release', 'esr'],
                      'Fenix': ['nightly', 'release']},
            scopes=['all'])
        try:
            self.assertEqual(config.channels('Firefox'),
                             ['nightly', 'release', 'esr'])
            self.assertEqual(config.channels('Fenix'), ['nightly', 'release'])
            self.assertEqual(config.channels(), ['nightly', 'release', 'esr'])
            self.assertEqual(config.pairs(), [
                ('Firefox', 'nightly'), ('Firefox', 'release'),
                ('Firefox', 'esr'), ('Fenix', 'nightly'),
                ('Fenix', 'release')])
            config.override(channels=['nightly', 'beta'])
            self.assertEqual(config.channels('Fenix'), ['nightly', 'beta'])
            self.assertEqual(len(config.pairs()), 4)
            # with the current-version scope every channel is collected
            # twice, the all scope first (see config.channel_key)
            config.override(scopes=['all', 'current'])
            self.assertEqual(len(config.pairs()), 8)
            self.assertEqual(config.pairs()[4], ('Firefox', 'nightly@current'))
        finally:
            config.restore(previous)


class CollectTest(DBTestCase):

    def test_plan_today_incremental(self):
        # today already fetched in full: an incremental window + installs
        as_of = datetime.datetime(2026, 9, 2, 11, 40)
        models.upsert_day('Firefox', 'nightly', TODAY, complete=True,
                          as_of=as_of, installs_as_of=as_of, crashes=10)
        db.session.commit()
        now = datetime.datetime(2026, 9, 2, 11, 45)
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=0,
                             recent_days=0, now=now)
        self.assertEqual([u.kind for u in units], ['recent'])
        self.assertEqual(units[0].start, datetime.datetime(2026, 9, 2, 10))
        self.assertEqual(units[0].end, now)
        self.assertEqual(units[0].day, TODAY)
        p = units[0].params()
        self.assertEqual(p['date'], ['>=2026-09-02T10:00:00',
                                     '<2026-09-02T11:45:00'])
        self.assertNotIn('_aggs.product', p)
        # a full refresh (installs + late-indexed hours) once old enough
        now = datetime.datetime(2026, 9, 2, 12, 15)
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=0,
                             recent_days=0, now=now)
        self.assertEqual([u.kind for u in units], ['day'])
        # the window never starts before the day does
        models.upsert_day('Firefox', 'nightly', TODAY,
                          as_of=datetime.datetime(2026, 9, 2, 0, 20))
        db.session.commit()
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=0,
                             recent_days=0,
                             now=datetime.datetime(2026, 9, 2, 0, 25))
        self.assertEqual(units[0].start, datetime.datetime(2026, 9, 2, 0))

    def test_write_recent_replaces_hours(self):
        # full fetch of the day first
        day = datetime.date(2026, 9, 1)
        unit = collect.Unit('day', 'Firefox', 'nightly', day,
                            day + datetime.timedelta(days=1))
        collect.write_day(unit, socorro.parse_day(load('socorro_day.json')),
                          datetime.datetime(2026, 9, 1, 14, 5), day)
        db.session.commit()
        s = models.get_series('Firefox', 'nightly',
                              'libc.so.6 | cuEGLApiInit')
        total_id = models.total_series('Firefox', 'nightly')
        before = models.load_hourly([s.id, total_id], [day])
        old_total = list(before[total_id][day])
        old_sig = list(before[s.id][day])
        # an incremental window 13:00 -> 14:30 says: hour 13 had 5 crashes
        # of a new signature only, hour 14 had 2 of cuEGLApiInit
        response = {'errors': [], 'total': 7, 'facets': {
            'signature': [{'term': 'brand new', 'count': 5, 'facets': {
                               'cardinality_install_time': {'value': 1}}},
                          {'term': 'libc.so.6 | cuEGLApiInit', 'count': 2,
                           'facets': {'cardinality_install_time':
                                      {'value': 2}}}],
            'histogram_date': [
                {'term': '2026-09-01T13:00:00Z', 'count': 5, 'facets': {
                    'cardinality_install_time': {'value': 1},
                    'signature': [{'term': 'brand new', 'count': 5}]}},
                {'term': '2026-09-01T14:00:00Z', 'count': 2, 'facets': {
                    'cardinality_install_time': {'value': 2},
                    'signature': [{'term': 'libc.so.6 | cuEGLApiInit',
                                   'count': 2}]}}]}}
        unit = collect.Unit('recent', 'Firefox', 'nightly',
                            datetime.datetime(2026, 9, 1, 13),
                            datetime.datetime(2026, 9, 1, 14, 30))
        collect.write_recent(unit, socorro.parse_recent(response),
                             datetime.datetime(2026, 9, 1, 14, 30), day)
        db.session.commit()
        after = models.load_hourly([s.id, total_id], [day])
        sig = after[s.id][day]
        total = after[total_id][day]
        # hours outside the window untouched, inside replaced
        self.assertEqual(sig[:13], old_sig[:13])
        self.assertEqual(total[:13], old_total[:13])
        self.assertEqual(sig[13], 0)
        self.assertEqual(sig[14], 2)
        self.assertEqual(total[13], 5)
        self.assertEqual(total[14], 2)
        self.assertEqual(total[15:], old_total[15:])
        new = models.get_series('Firefox', 'nightly', 'brand new')
        self.assertEqual(after and models.load_hourly([new.id], [day])[
            new.id][day][13], 5)
        # day counts are the sums of the hourly arrays
        daily = models.load_daily([s.id, total_id, new.id], day, day)
        self.assertEqual(daily[s.id][day][0], sum(sig))
        self.assertEqual(daily[total_id][day][0], sum(total))
        self.assertEqual(daily[new.id][day][0], 5)
        # a signature new to the day gets the window's distinct installs;
        # a known one keeps the installs of the last full refresh
        self.assertEqual(daily[new.id][day][1], 1)
        self.assertEqual(daily[s.id][day][1], 9)
        row = models.get_day('Firefox', 'nightly', day)
        self.assertEqual(row.crashes, sum(total))
        self.assertEqual(row.as_of, datetime.datetime(2026, 9, 1, 14, 30))
        # hourly installs of the total replaced in the window
        inst = models.load_hourly([total_id], [day], installs=True)
        self.assertEqual(inst[total_id][day][13:15], [1, 2])
        # installs refresh
        response = {'errors': [], 'total': 3100, 'facets': {
            'product': [{'term': 'Firefox', 'count': 3100, 'facets': {
                'cardinality_install_time': {'value': 1500}}}],
            'signature': [
                {'term': 'libc.so.6 | cuEGLApiInit', 'count': 825,
                 'facets': {'cardinality_install_time': {'value': 11}}}]}}
        unit = collect.Unit('installs', 'Firefox', 'nightly', day,
                            day + datetime.timedelta(days=1))
        collect.write_installs(unit, socorro.parse_installs(response),
                               datetime.datetime(2026, 9, 1, 14, 31), day)
        db.session.commit()
        daily = models.load_daily([s.id, total_id], day, day)
        self.assertEqual(daily[s.id][day][1], 11)
        self.assertEqual(daily[s.id][day][0], sum(sig))  # counts kept
        self.assertEqual(daily[s.id][day][2], 825)  # matched crash count
        self.assertEqual(daily[total_id][day][1], 1500)
        row = models.get_day('Firefox', 'nightly', day)
        self.assertEqual(row.installs_as_of,
                         datetime.datetime(2026, 9, 1, 14, 31))

    def test_plan_fresh_channel(self):
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=30,
                             recent_days=7, chunk_days=14)
        kinds = [u.kind for u in units]
        self.assertEqual(kinds[:8], ['day'] * 8)
        self.assertEqual(units[0].start, TODAY)
        self.assertEqual(units[7].start, TODAY - datetime.timedelta(days=7))
        # 30 - 7 = 23 missing history days -> 2 chunks x 2 queries
        self.assertEqual(kinds[8:], ['daily', 'hourly_total'] * 2)
        self.assertEqual(units[8].start, TODAY - datetime.timedelta(days=30))
        self.assertEqual((units[8].end - units[8].start).days, 14)

    def test_plan_skips_final_days(self):
        old = TODAY - datetime.timedelta(days=3)
        models.upsert_day('Firefox', 'nightly', old, final=True,
                          complete=True, as_of=NOW)
        models.upsert_day('Firefox', 'nightly',
                          TODAY - datetime.timedelta(days=2), final=True,
                          complete=False, as_of=NOW)
        db.session.commit()
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=8,
                             recent_days=7)
        starts = [u.start for u in units if u.kind == 'day']
        self.assertNotIn(old, starts)
        # final but fetched without the hourly split: fetched again
        self.assertIn(TODAY - datetime.timedelta(days=2), starts)
        self.assertIn(TODAY, starts)

    def test_is_final(self):
        day = TODAY - datetime.timedelta(days=1)
        early = datetime.datetime(2026, 9, 2, 3, 0)
        late = datetime.datetime(2026, 9, 2, 7, 0)
        self.assertFalse(collect.is_final(day, early, 10, 10, TODAY, 6, 7))
        self.assertFalse(collect.is_final(day, late, 10, 9, TODAY, 6, 7))
        self.assertFalse(collect.is_final(day, late, 10, None, TODAY, 6, 7))
        self.assertTrue(collect.is_final(day, late, 10, 10, TODAY, 6, 7))
        old = TODAY - datetime.timedelta(days=20)
        self.assertTrue(collect.is_final(old, late, 10, None, TODAY, 6, 7))

    def test_execute_and_write(self):
        responses = {'day': load('socorro_day.json'),
                     'daily': load('socorro_daily.json'),
                     'hourly_total': load('socorro_hourly.json')}
        today = datetime.date(2026, 9, 2)
        units = [collect.Unit('day', 'Firefox', 'nightly',
                              datetime.date(2026, 9, 1),
                              datetime.date(2026, 9, 2)),
                 collect.Unit('daily', 'Firefox', 'nightly',
                              datetime.date(2026, 8, 18),
                              datetime.date(2026, 9, 1)),
                 collect.Unit('hourly_total', 'Firefox', 'nightly',
                              datetime.date(2026, 9, 1),
                              datetime.date(2026, 9, 2))]
        now = datetime.datetime(2026, 9, 2, 12, 0)
        written, failed, skipped = collect.execute(
            units, FakeFetcher(responses), today, now)
        self.assertEqual((written, failed, skipped), (3, 0, 0))
        day = models.get_day('Firefox', 'nightly', datetime.date(2026, 9, 1))
        self.assertTrue(day.complete)
        self.assertEqual(day.crashes, responses['day']['total'])
        self.assertFalse(day.final)  # first fetch: no previous total
        total_id = models.total_series('Firefox', 'nightly')
        daily = models.load_daily([total_id], datetime.date(2026, 8, 18))
        self.assertEqual(len(daily[total_id]), 15)
        s = models.get_series('Firefox', 'nightly',
                              'libc.so.6 | cuEGLApiInit')
        self.assertIsNotNone(s)
        self.assertEqual(s.first_seen, datetime.date(2026, 9, 1))
        rows = models.load_daily([s.id], datetime.date(2026, 9, 1))
        crashes, installs, _ = rows[s.id][datetime.date(2026, 9, 1)]
        self.assertEqual(crashes, 823)
        self.assertEqual(installs, 9)
        hourly = models.load_hourly([s.id, total_id],
                                    [datetime.date(2026, 9, 1)])
        self.assertEqual(sum(hourly[s.id][datetime.date(2026, 9, 1)]), 823)
        self.assertEqual(hourly[total_id][datetime.date(2026, 9, 1)][13],
                         978)
        # a second fetch with the same total after the grace period is final
        collect.execute(units[:1], FakeFetcher(responses), today, now)
        day = models.get_day('Firefox', 'nightly', datetime.date(2026, 9, 1))
        self.assertTrue(day.final)
        # nothing left to fetch for 2026-09-01 (final); the history days
        # written by the daily chunk still lack the total's hourly split
        units = collect.plan('Firefox', 'nightly', today, history_days=15,
                             recent_days=7)
        kinds = [u.kind for u in units]
        self.assertNotIn('daily', kinds)
        self.assertEqual(kinds.count('hourly_total'), 1)
        hourly = [u for u in units if u.kind == 'hourly_total'][0]
        self.assertEqual(hourly.start, datetime.date(2026, 8, 18))
        starts = [u.start for u in units if u.kind == 'day']
        self.assertIn(today, starts)
        self.assertNotIn(datetime.date(2026, 9, 1), starts)

    def test_empty_signature_does_not_clobber_total(self):
        data = load('socorro_daily.json')
        # Socorro sometimes reports crashes with an empty signature
        for bucket in data['facets']['histogram_date']:
            bucket['facets']['signature'].append({'term': '', 'count': 3})
        unit = collect.Unit('daily', 'Firefox', 'nightly',
                            datetime.date(2026, 8, 18),
                            datetime.date(2026, 9, 1))
        collect.write_daily(unit, socorro.parse_daily(data), NOW, TODAY)
        db.session.commit()
        total_id = models.total_series('Firefox', 'nightly')
        day = datetime.date(2026, 8, 18)
        self.assertEqual(models.load_daily([total_id], day, day)[total_id][
            day][0], 21458)
        s = models.get_series('Firefox', 'nightly', socorro.EMPTY_SIGNATURE)
        self.assertEqual(models.load_daily([s.id], day, day)[s.id][day][0],
                         3)

    def test_write_marks_noise(self):
        data = load('socorro_day.json')
        data['facets']['signature'].append(
            {'term': 'IPCError-browser | ShutDownKill', 'count': 50,
             'facets': {'cardinality_install_time': {'value': 40}}})
        unit = collect.Unit('day', 'Firefox', 'nightly',
                            datetime.date(2026, 9, 1),
                            datetime.date(2026, 9, 2))
        collect.write_day(unit, socorro.parse_day(data), NOW, TODAY)
        db.session.commit()
        s = models.get_series('Firefox', 'nightly',
                              'IPCError-browser | ShutDownKill')
        self.assertTrue(s.noise)
        s = models.get_series('Firefox', 'nightly',
                              'libc.so.6 | cuEGLApiInit')
        self.assertFalse(s.noise)


def seed_channel(product, channel, today, now, ndays=60, base=10000.0,
                 seed=0, hour_now=12):
    """Create a synthetic channel: total + a few signatures with stories.

    Returns the signature -> daily mean mapping.
    """
    rng = np.random.default_rng(seed)
    days = [today - datetime.timedelta(days=i) for i in range(ndays, -1, -1)]
    profile = np.ones(24) / 24.0
    total_id = models.total_series(product, channel)
    stories = {
        'stable': dict(mean=100.0, today=1.0),
        'spiking': dict(mean=100.0, today=4.0),
        'brand new': dict(mean=0.0, today=None, new=60),
        # a couple of machines crashing in a loop
        'storm | 0x1': dict(mean=50.0, today=6.0, installs=2),
        # one machine with a thousand crashes of a normally quiet signature
        'one machine': dict(mean=5.0, today=200.0, installs=1),
        # crashes x3 but from the usual number of machines: not a spike
        'loop only': dict(mean=100.0, today=3.0, installs_share=0.3),
        'IPCError-browser | ShutDownKill': dict(mean=200.0, today=5.0),
        'dropping': dict(mean=300.0, today=0.1),
        'tiny': dict(mean=1.0, today=1.0),
    }
    ids = models.series_ids(product, channel, stories.keys(),
                            noise=lambda s: s.startswith('IPCError'))
    daily, hourly = [], []
    for d in days:
        partial = d == today
        w = WEEKLY[d.weekday()]
        total = 0
        total_hours = np.zeros(24)
        for sgn, st in stories.items():
            if partial and st['today'] is None:
                mean = st['new']
            elif partial:
                mean = st['mean'] * st['today']
            else:
                mean = st['mean']
            mu = mean * w
            hours = rng.poisson(mu * profile) if mu > 0 else np.zeros(24)
            hours = hours.astype(float)
            if partial:
                hours[hour_now:] = 0
                hours[hour_now - 1] *= 0.5
            crashes = int(hours.sum())
            installs = st.get('installs', None)
            if installs is None:
                share = 0.9
                if 'installs_share' in st:
                    # today's extra crashes come from the usual installs
                    share = st['installs_share'] if partial else 0.9
                installs = max(1, int(crashes * share)) if crashes else 0
            if crashes > 0 or partial:
                daily.append({'series_id': ids[sgn], 'day': d,
                              'crashes': crashes, 'installs': installs})
                hourly.append({'series_id': ids[sgn], 'day': d,
                               'hourly': [int(x) for x in hours]})
                if crashes > 0:
                    models.update_seen([ids[sgn]], d)
            total += crashes
            total_hours += hours
        # the rest of the channel (hours after now are zeroed below)
        rest = rng.poisson(base * w * profile)
        if partial:
            rest[hour_now:] = 0
        total += int(rest.sum())
        total_hours += rest
        daily.append({'series_id': total_id, 'day': d, 'crashes': total,
                      'installs': int(total * 0.85)})
        hourly.append({'series_id': total_id, 'day': d,
                       'hourly': [int(x) for x in total_hours],
                       'installs': [int(x * 0.85) for x in total_hours]})
        as_of = now if partial else datetime.datetime(
            d.year, d.month, d.day) + datetime.timedelta(hours=30)
        models.upsert_day(product, channel, d, crashes=total, cutoff=None,
                          as_of=as_of, final=not partial, complete=True)
    models.upsert(models.Daily, daily, ['series_id', 'day'])
    models.upsert(models.Hourly, hourly, ['series_id', 'day'])
    db.session.commit()
    return ids


class ScoringTest(DBTestCase):

    def setUp(self):
        super().setUp()
        self.ids = seed_channel('Firefox', 'release', TODAY, NOW)

    def scores(self):
        res = {}
        for score, series in models.load_scores('Firefox', 'release',
                                                [TODAY]):
            res[series.signature] = score
        return res

    def test_score_channel(self):
        summary = scoring.score_channel('Firefox', 'release', TODAY, NOW)
        db.session.commit()
        self.assertEqual(summary['product'], 'Firefox')
        self.assertGreaterEqual(summary['scored'], 5)
        s = self.scores()
        self.assertIn('', s)  # the total
        total = s['']
        self.assertTrue(total.partial)
        self.assertAlmostEqual(total.elapsed, 0.5, delta=0.05)
        self.assertEqual(total.severity, 'ok')
        self.assertEqual(s['stable'].severity, 'ok')
        self.assertLess(abs(s['stable'].z), 3)
        self.assertIn(s['spiking'].severity, ('spike', 'major'))
        self.assertGreater(s['spiking'].ratio, 3)
        self.assertIsNotNone(s['spiking'].first_flagged_at)
        self.assertEqual(s['spiking'].peak_severity,
                         s['spiking'].severity)
        self.assertTrue(s['brand new'].is_new)
        self.assertIn(s['brand new'].severity, ('spike', 'major'))
        # installs are first class: storms and loops are badges, not alerts
        self.assertTrue(s['storm | 0x1'].storm)
        self.assertEqual(s['storm | 0x1'].severity, 'ok')
        self.assertTrue(s['one machine'].storm)
        self.assertEqual(s['one machine'].severity, 'ok')
        self.assertEqual(s['one machine'].installs, 1)
        # crashes alone would have said major
        self.assertGreater(s['one machine'].z, 8)
        self.assertEqual(s['loop only'].severity, 'ok')
        self.assertLess(s['loop only'].z_installs, 3)
        self.assertGreater(s['spiking'].z_installs, 5)
        self.assertIsNotNone(s['spiking'].expected_installs)
        self.assertEqual(s['dropping'].severity, 'drop')
        self.assertIsNotNone(total.expected_installs)
        # the channel's crash excess comes mostly from the crash loops:
        # explained as storm-driven, not reported as a spike
        self.assertTrue(total.details.get('storm_driven'))
        self.assertGreaterEqual(total.details['storm_share'], 0.5)
        self.assertEqual(total.severity, 'ok')
        # noise is scored but counted apart
        self.assertIn('IPCError-browser | ShutDownKill', s)
        self.assertEqual(summary['counts']['noise'], 1)
        self.assertGreaterEqual(summary['counts']['spike'] +
                                summary['counts']['major'], 2)
        self.assertEqual(summary['counts']['storm'], 2)
        # recent window available for the total
        self.assertIsNotNone(total.z_recent)
        self.assertEqual(total.recent_hours, 3)
        # drivers explain the total's deviation
        drivers = total.details['drivers']
        names = [d['signature'] for d in drivers]
        self.assertIn('spiking', names)
        # noise signatures are listed as drivers but flagged as such
        noisy = [d for d in drivers if d['noise']]
        self.assertEqual([d['signature'] for d in noisy],
                         ['IPCError-browser | ShutDownKill'])
        self.assertTrue(all(0 < d['share'] <= 1 for d in drivers))
        # yesterday scored as a complete day
        yesterday = TODAY - datetime.timedelta(days=1)
        ys = {series.signature: score for score, series in
              models.load_scores('Firefox', 'release', [yesterday])}
        self.assertFalse(ys[''].partial)
        self.assertEqual(ys['stable'].severity, 'ok')
        # models cached
        cached = models.load_models([self.ids['stable']])
        self.assertIn(self.ids['stable'], cached)
        self.assertGreater(cached[self.ids['stable']].level, 50)
        # second run reuses the cache and keeps peaks / first_flagged_at
        first = s['spiking'].first_flagged_at
        summary2 = scoring.score_channel('Firefox', 'release', TODAY,
                                         NOW + datetime.timedelta(minutes=10))
        db.session.commit()
        self.assertEqual(summary2['fits'], 0)
        self.assertEqual(self.scores()['spiking'].first_flagged_at, first)

    def test_gates_count_the_last_24_hours(self):
        # 03:00 UTC, an eighth of the day is in.  A quiet signature (5 a
        # day) spiked yesterday (30 crashes) and has 15 crashes from 8
        # installs today: far above expectation, but under the esr floors
        # (20 crashes, 10 installs) if they were checked on today's partial
        # count alone.  Over the last 24 hours it passes both.
        now = datetime.datetime(2026, 9, 2, 3, 0, 0)
        yesterday = TODAY - datetime.timedelta(days=1)
        seed_channel('Firefox', 'esr', TODAY, now, hour_now=3, seed=1)
        sid = models.series_ids('Firefox', 'esr', ['overnight'])['overnight']
        daily, hourly = [], []
        for i in range(60, -1, -1):
            d = TODAY - datetime.timedelta(days=i)
            hours = [0] * 24
            if d == TODAY:
                hours[0:3] = [5, 5, 5]
                installs = 8
            elif d == yesterday:
                hours = [1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 1, 1,
                         1, 1, 2, 2, 1, 1, 1, 1, 2, 2, 1, 1]
                installs = 25
            else:
                for h in (3, 8, 13, 18, 22):
                    hours[h] = 1
                installs = 4
            daily.append({'series_id': sid, 'day': d, 'crashes': sum(hours),
                          'installs': installs})
            hourly.append({'series_id': sid, 'day': d, 'hourly': hours})
            models.update_seen([sid], d)
        models.upsert(models.Daily, daily, ['series_id', 'day'])
        models.upsert(models.Hourly, hourly, ['series_id', 'day'])
        db.session.commit()
        scoring.score_channel('Firefox', 'esr', TODAY, now)
        db.session.commit()
        s = {series.signature: score for score, series in
             models.load_scores('Firefox', 'esr', [TODAY])}
        row = s['overnight']
        self.assertEqual(row.observed, 15)
        self.assertGreater(row.z, 5)
        self.assertIn(row.severity, scoring.UPWARD)
        # 15 today + the 21 hours of yesterday after 03:00 (27 crashes)
        self.assertEqual(row.details['last24']['crashes'], 42)
        self.assertGreaterEqual(row.details['last24']['installs'], 10)
        self.assertEqual(row.last_flagged_at, now)
        self.assertIsNone(s['stable'].last_flagged_at)
        # yesterday is scored as a complete day and was a spike too
        ys = {series.signature: score for score, series in
              models.load_scores('Firefox', 'esr', [yesterday])}
        self.assertIn(ys['overnight'].severity, scoring.UPWARD)
        # a signature with nothing yesterday is still held to the floor:
        # "brand new" (60 a day from today, ~7 crashes by 03:00) is not
        # even a candidate yet
        self.assertNotIn('brand new', s)

    def test_total_fitted_on_long_history(self):
        # Socorro is backfilled 180 days back, but the database keeps the
        # totals: once two years have accumulated the total's yearly
        # component activates, and signatures borrow it (their own fit
        # stays on 180 days)
        seed_channel('Firefox', 'beta', TODAY, NOW, ndays=800, seed=2)
        scoring.score_channel('Firefox', 'beta', TODAY, NOW)
        db.session.commit()
        total_id = models.total_series('Firefox', 'beta')
        total = models.load_models([total_id])[total_id]
        self.assertGreaterEqual(total.history_days, 795)
        comps = total.components['components']
        self.assertTrue(comps['yearly']['active'])
        self.assertGreaterEqual(comps['yearly']['cycles'], 2)
        self.assertIn('yearly', total.factors)
        self.assertEqual(len(total.factors['yearly']), 53)
        sid = models.get_series('Firefox', 'beta', 'stable').id
        stable = models.load_models([sid])[sid]
        self.assertLessEqual(stable.history_days, 181)
        self.assertTrue(stable.components['components']['yearly']['active'])
        self.assertIn('yearly', stable.borrowed)
        # the chart's fit of the total uses the same window
        with app.test_request_context():
            block, fit = api.daily_block('Firefox', 'beta', total_id, TODAY,
                                         30, 'day', None,
                                         history_days=api.config.
                                         fit_history_days())
        self.assertTrue(fit.active['yearly'])
        self.assertEqual(len(block['start']), 30)

    def test_lag_guard(self):
        def summaries(nsuspicious):
            ok = {'ratio': 1.0, 'expected': 100, 'z_recent': 0}
            bad = {'ratio': 0.5, 'expected': 100, 'z_recent': -5}
            return [{'total': bad if i < nsuspicious else ok}
                    for i in range(6)]
        self.assertTrue(update.lag_guard(summaries(6), TODAY))
        self.assertTrue(update.lag_guard(summaries(4), TODAY))
        self.assertFalse(update.lag_guard(summaries(3), TODAY))
        # fewer scored channels than the threshold: all of them must drop
        self.assertTrue(update.lag_guard(summaries(6)[:2], TODAY))
        # the current-version scope restarts from nothing on release day:
        # its channels never count
        current = [dict(s, channel='release@current')
                   for s in summaries(6)]
        self.assertFalse(update.lag_guard(summaries(3) + current, TODAY))

    def test_history_chunk_replanned_when_hourly_fails(self):
        responses = {'day': load('socorro_day.json'),
                     'daily': load('socorro_daily.json'),
                     'hourly_total': None}
        units = [collect.Unit('daily', 'Firefox', 'nightly',
                              datetime.date(2026, 8, 18),
                              datetime.date(2026, 9, 1)),
                 collect.Unit('hourly_total', 'Firefox', 'nightly',
                              datetime.date(2026, 8, 18),
                              datetime.date(2026, 9, 1))]
        written, failed, skipped = collect.execute(
            units, FakeFetcher(responses), TODAY, NOW)
        self.assertEqual((written, failed), (1, 1))
        # the day rows exist but the total has no hourly split: the
        # planner asks for the hourly chunk again (and not for daily)
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=15,
                             recent_days=1)
        kinds = [(u.kind, u.start) for u in units if u.kind != 'day']
        self.assertEqual(kinds, [('hourly_total',
                                  datetime.date(2026, 8, 18))])

    def test_missing_index_days_are_unknown(self):
        # Socorro deleted the oldest week's index (its retention edge):
        # the days of that week are stored as unknown, not as empty, and
        # are not asked for again
        start, end = datetime.date(2026, 8, 18), datetime.date(2026, 9, 1)
        gone = socorro.index_for(start)
        daily = copy.deepcopy(load('socorro_daily.json'))
        daily['errors'] = [{'type': 'missing_index', 'index': gone}]
        daily['facets']['histogram_date'] = [
            b for b in daily['facets']['histogram_date']
            if socorro.index_for(socorro.parse_term(b['term']).date())
            != gone]
        hourly = copy.deepcopy(load('socorro_hourly.json'))
        hourly['errors'] = daily['errors']
        responses = {'daily': daily, 'hourly_total': hourly}
        units = [collect.Unit('daily', 'Firefox', 'nightly', start, end),
                 collect.Unit('hourly_total', 'Firefox', 'nightly', start,
                              end)]
        written, failed, skipped = collect.execute(
            units, FakeFetcher(responses), TODAY, NOW)
        self.assertEqual((written, failed), (2, 0))
        days = [start + datetime.timedelta(days=i)
                for i in range((end - start).days)]
        unknown = [d for d in days if socorro.index_for(d) == gone]
        self.assertEqual(len(unknown), 6)  # Tuesday 18 .. Sunday 23
        rows = {r.day: r
                for r in models.load_days('Firefox', 'nightly', start)}
        total_id = models.total_series('Firefox', 'nightly', create=False)
        counts = models.load_daily([total_id], start)[total_id]
        hours = models.load_hourly([total_id], days).get(total_id, {})
        for d in unknown:
            self.assertIsNone(rows[d].crashes)
            self.assertTrue(rows[d].complete and rows[d].final)
            self.assertNotIn(d, counts)
            self.assertNotIn(d, hours)
        for d in days:
            if d not in unknown:
                self.assertIn(d, counts)
                self.assertIn(d, hours)
        # the fits see them as NaN, not as days without crashes
        dates, y, _ = scoring.build_history({}, rows, start, days[-1])
        self.assertTrue(all(np.isnan(y[dates.index(d)]) for d in unknown))
        self.assertTrue(all(np.isfinite(y[dates.index(d)])
                            for d in days if d not in unknown))
        # and the planner is done with them
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=15,
                             recent_days=1)
        self.assertEqual([u for u in units
                          if u.kind in collect.HISTORY_KINDS], [])
        # today's query whose index is gone stores nothing
        day = load('socorro_day.json')
        day['errors'] = [{'type': 'missing_index',
                          'index': socorro.index_for(TODAY)}]
        fetcher = FakeFetcher({'day': day})
        units = [collect.Unit('day', 'Firefox', 'nightly', TODAY,
                              TODAY + datetime.timedelta(days=1))]
        written, failed, skipped = collect.execute(units, fetcher, TODAY,
                                                   NOW)
        self.assertEqual((written, failed, fetcher.failures), (0, 1, 1))
        self.assertIsNone(models.get_day('Firefox', 'nightly', TODAY))
        # a past day fetched alone (a filter per day) at the retention
        # edge is stored as unknown, like the days of a chunk
        old = TODAY - datetime.timedelta(days=100)
        day['errors'] = [{'type': 'missing_index',
                          'index': socorro.index_for(old)}]
        units = [collect.Unit('day', 'Firefox', 'nightly', old,
                              old + datetime.timedelta(days=1))]
        written, failed, skipped = collect.execute(
            units, FakeFetcher({'day': day}), TODAY, NOW)
        self.assertEqual((written, failed), (1, 0))
        row = models.get_day('Firefox', 'nightly', old)
        self.assertIsNone(row.crashes)
        self.assertTrue(row.complete and row.final)

    def test_stale_non_final_day_is_refetched(self):
        old = TODAY - datetime.timedelta(days=12)
        models.upsert_day('Firefox', 'nightly', old, final=False,
                          complete=True, as_of=NOW, crashes=10)
        db.session.commit()
        units = collect.plan('Firefox', 'nightly', TODAY, history_days=15,
                             recent_days=7)
        self.assertIn(old, [u.start for u in units if u.kind == 'day'])


class HousekeepingTest(DBTestCase):

    def test_prune_drops_series_left_without_data(self):
        old = TODAY - datetime.timedelta(days=200)
        ids = models.series_ids('Firefox', 'release',
                                ['gone', 'kept', 'fresh'])
        total = models.total_series('Firefox', 'release')
        models.upsert(models.Daily, [
            {'series_id': ids['gone'], 'day': old, 'crashes': 1},
            {'series_id': ids['kept'], 'day': old, 'crashes': 50},
            {'series_id': ids['fresh'], 'day': TODAY, 'crashes': 1},
            {'series_id': total, 'day': old, 'crashes': 1}],
            ['series_id', 'day'])
        models.upsert(models.Model, [
            {'series_id': sid, 'fitted_at': NOW, 'last_day': TODAY}
            for sid in (ids['gone'], ids['kept'])], ['series_id'])
        for sgn in ('gone', 'kept'):
            models.replace_bugs(sgn, {1: {'status': 'NEW'}}, NOW)
        models.mark_bugs_checked({'gone': 1, 'kept': 1}, NOW)
        db.session.commit()
        removed = models.prune(TODAY, 120, 3, 365, 10, 60, 30, 30)
        db.session.commit()
        # the low-volume old rows go, and with them the series that has
        # nothing left and its bugs; recent data or volume keep a series
        self.assertEqual(removed, 1)
        self.assertIsNone(models.get_series('Firefox', 'release', 'gone'))
        for sgn in ('kept', 'fresh'):
            self.assertIsNotNone(models.get_series('Firefox', 'release',
                                                   sgn), sgn)
        self.assertEqual(sorted(models.load_bugs(['gone', 'kept'])), ['kept'])
        self.assertEqual(sorted(models.load_bug_checks(['gone', 'kept'])),
                         ['kept'])
        self.assertEqual(models.total_series('Firefox', 'release',
                                             create=False), total)
        self.assertEqual(sorted(models.load_models([ids['gone'],
                                                    ids['kept']])),
                         [ids['kept']])
        # nothing more to do on a second pass
        self.assertEqual(models.prune(TODAY, 120, 3, 365, 10, 60, 30, 30), 0)


class ApiTest(DBTestCase):

    def setUp(self):
        super().setUp()
        seed_channel('Firefox', 'release', TODAY, NOW)
        scoring.score_channel('Firefox', 'release', TODAY, NOW)
        run = models.start_run()
        run.status = 'ok'
        run.finished = NOW
        run.message = json.dumps({'pending_units': 0})
        db.session.commit()
        self.client = app.test_client()
        self.patches = [mock.patch.object(api, 'today_utc',
                                         return_value=TODAY),
                        mock.patch.object(models, 'utcnow',
                                          return_value=NOW),
                        mock.patch.object(api, 'releases',
                                          return_value=[]),
                        mock.patch.object(api, 'next_release',
                                          return_value={
                                              'date': TODAY +
                                              datetime.timedelta(days=12),
                                              'version': '156.0'}),
                        mock.patch.object(api.config, 'pairs',
                                          return_value=[('Firefox',
                                                         'release')])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_summary(self):
        r = self.client.get('/dashboard/api/summary')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['data_health']['status'], 'ok')
        self.assertEqual(d['last_run']['status'], 'ok')
        self.assertEqual(len(d['channels']), 1)
        ch = d['channels'][0]
        self.assertEqual((ch['product'], ch['channel']),
                         ('Firefox', 'release'))
        self.assertEqual(ch['total']['severity'], 'ok')
        self.assertIn('drivers', ch['total'])
        self.assertIn('counts', ch)
        sigs = [a['signature'] for a in d['alerts']]
        self.assertIn('spiking', sigs)
        self.assertIn('brand new', sigs)
        self.assertNotIn('IPCError-browser | ShutDownKill', sigs)
        self.assertIn('thresholds', d)
        self.assertNotIn('one machine', sigs)
        self.assertEqual(ch['counts']['storm'], 2)
        self.assertIn('storm_driven', ch['total'])
        row = d['alerts'][0]
        for key in ('socorro_url', 'spark', 'bugs', 'confidence', 'since',
                    'yesterday', 'excess', 'ratio', 'z_installs',
                    'expected_installs'):
            self.assertIn(key, row)
        self.assertEqual(len(row['spark']['dates']), 28)

    def test_channel(self):
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&days=30')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['daily']['granularity'], 'day')
        # 30 days of history plus the 12-day forecast to the next release
        self.assertEqual(len(d['daily']['start']), 42)
        self.assertEqual(d['daily']['partial'].index(True), 29)
        self.assertEqual(d['daily']['partial'].count(True), 1)
        self.assertEqual(d['daily']['future'][:30], [False] * 30)
        self.assertEqual(d['daily']['future'][30:], [True] * 12)
        self.assertEqual(d['daily']['start'][-1],
                         (TODAY + datetime.timedelta(days=12)).isoformat())
        self.assertIsNone(d['daily']['observed'][-1])
        self.assertIsNotNone(d['daily']['expected'][-1])
        self.assertLess(d['daily']['lo3'][-1], d['daily']['expected'][-1])
        self.assertIsNone(d['daily']['z'][-1])
        self.assertEqual(d['daily']['severity'][-1], 'ok')
        self.assertIsNotNone(d['daily']['projected'][29])
        self.assertEqual(d['next_release'],
                         {'date': (TODAY + datetime.timedelta(days=12))
                          .isoformat(), 'version': '156.0', 'upcoming': True})
        self.assertEqual(d['releases'][-1], d['next_release'])
        for key in ('observed', 'expected', 'lo3', 'hi3', 'lo5', 'hi5', 'z',
                    'severity', 'projected', 'future'):
            self.assertEqual(len(d['daily'][key]), 42)
        self.assertEqual(len(d['hourly']['today']), 24)
        self.assertEqual(d['hourly']['in_progress_hour'], 12)
        self.assertIsNone(d['hourly']['today'][13])
        self.assertEqual(len(d['hourly']['expected_today']), 24)
        self.assertIn('weekly', d['model']['factors'])
        self.assertIn('today_factors', d['model'])
        sigs = {s['signature']: s for s in d['signatures']}
        self.assertIn('spiking', sigs)
        self.assertEqual(d['signatures'][0]['severity'],
                         max((s['severity'] for s in d['signatures']),
                             key=lambda x: scoring.RANK[x]))
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&days=60&granularity=week')
        d = r.get_json()
        self.assertEqual(d['daily']['granularity'], 'week')
        # one week in progress (today's), then the forecast weeks
        self.assertEqual(d['daily']['partial'].count(True), 1)
        cur = d['daily']['partial'].index(True)
        self.assertEqual(d['daily']['future'][cur], False)
        self.assertTrue(all(d['daily']['future'][cur + 1:]))
        self.assertGreaterEqual(len(d['daily']['future']) - cur - 1, 1)
        self.assertIsNone(d['daily']['observed'][-1])
        self.assertIsNotNone(d['daily']['expected'][-1])
        self.assertIsNotNone(d['daily']['projected'][cur])
        self.assertGreater(len(d['daily']['start']), 7)

    def test_signature_and_errors(self):
        r = self.client.get('/dashboard/api/signature?product=Firefox'
                            '&channel=release&signature=spiking&days=30')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['row']['signature'], 'spiking')
        self.assertIn(d['row']['severity'], ('spike', 'major'))
        self.assertEqual(len(d['daily']['start']), 42)  # + 12 forecast days
        self.assertTrue(d['daily']['future'][-1])
        self.assertEqual(d['next_release']['version'], '156.0')
        self.assertIn('borrowed', d['model'])
        self.assertIsNotNone(d['hourly']['today'])
        r = self.client.get('/dashboard/api/signature?product=Firefox'
                            '&channel=release&signature=nope')
        self.assertEqual(r.status_code, 404)
        r = self.client.get('/dashboard/api/channel?product=Nope')
        self.assertEqual(r.status_code, 400)
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=beta')
        self.assertEqual(r.status_code, 404)

    def sign_in(self, email='someone@mozilla.com'):
        from spikes.dashboard import auth
        with self.client.session_transaction() as sess:
            sess[auth.SESSION_KEY] = {'email': email, 'name': 'Someone',
                                      'picture': None}

    def channel_rows(self):
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&days=30')
        self.assertEqual(r.status_code, 200)
        return {s['signature']: s for s in r.get_json()['signatures']}, \
            r.headers['ETag']

    def test_scope(self):
        """``scope=current`` serves the current-version channels (their own
        series, keyed ``channel@current``), with the version current
        today; the all scope does not list them."""
        from spikes.dashboard import versions
        seed_channel('Firefox', 'release@current', TODAY, NOW, seed=1)
        scoring.score_channel('Firefox', 'release@current', TODAY, NOW)
        db.session.commit()
        models.replace_cycles('Firefox', 'release', [
            {'start': TODAY - datetime.timedelta(days=15), 'end': TODAY,
             'label': '154', 'params': {'major_version': 154}},
            {'start': TODAY, 'end': None, 'label': '155',
             'params': {'major_version': 155}}], NOW)
        db.session.commit()
        versions._cache.clear()
        pairs = {None: [('Firefox', 'release'),
                        ('Firefox', 'release@current')],
                 'all': [('Firefox', 'release')],
                 'current': [('Firefox', 'release@current')]}
        with mock.patch.object(api.config, 'pairs',
                               side_effect=lambda scope=None: pairs[scope]):
            r = self.client.get('/dashboard/api/summary')
            d = r.get_json()
            self.assertEqual(d['scope'], 'all')
            self.assertEqual(d['scopes'], config.scopes())
            self.assertEqual([c['channel'] for c in d['channels']],
                             ['release'])
            self.assertEqual(d['channels'][0]['scope'], 'all')
            self.assertIsNone(d['channels'][0]['version'])
            self.assertEqual(d['data_health']['status'], 'ok')
            etag_all = r.headers['ETag']
            r = self.client.get('/dashboard/api/summary?scope=current')
            self.assertEqual(r.status_code, 200)
            self.assertNotEqual(r.headers['ETag'], etag_all)
            d = r.get_json()
            self.assertEqual(d['scope'], 'current')
            c = d['channels'][0]
            self.assertEqual((c['channel'], c['scope'], c['version']),
                             ('release', 'current', '155'))
            self.assertEqual(d['data_health']['status'], 'ok')
            self.assertTrue(all(a['scope'] == 'current' for a in d['alerts']))
            self.assertEqual(self.client.get(
                '/dashboard/api/summary?scope=nope').status_code, 400)
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&scope=current&days=30')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual((d['channel'], d['scope'], d['version']),
                         ('release', 'current', '155'))
        self.assertEqual(d['model']['cycle_from'], 'release')
        self.assertEqual(d['model']['cycle_day'], 1)  # release day today
        row = d['signatures'][0]
        self.assertEqual(row['scope'], 'current')
        self.assertIn('major_version=155', row['socorro_url'])
        # the forecast runs to the next cycle when it is known; here none
        # is planned, so the release schedule (mocked) gives the horizon
        d = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&days=30').get_json()
        self.assertEqual(d['scope'], 'all')
        self.assertEqual(d['model']['cycle_from'], 'calendar')
        self.assertNotIn('major_version', d['signatures'][0]['socorro_url'])

    def test_bugs_on_rows(self):
        """The bugs of a flagged row say whether they were filed after
        its spike started (the flag's first run); an unflagged row shows
        its bugs without that verdict."""
        rows, _ = self.channel_rows()
        self.assertEqual(rows['spiking']['flag']['day'], TODAY.isoformat())
        # the spike's start is the first flagged day's midnight, not the
        # run that flagged it (NOW, noon): a bug from that morning counts
        since = datetime.datetime.combine(TODAY, datetime.time())
        hour = datetime.timedelta(hours=1)
        models.replace_bugs('spiking', {
            1001: {'created_at': since - 30 * hour, 'status': 'RESOLVED',
                   'resolution': 'FIXED', 'summary': 'old one'},
            1002: {'created_at': since + hour, 'status': 'NEW',
                   'summary': 'filed for the spike', 'source': 'bugzilla'},
            1003: {'created_at': None, 'status': None}}, NOW)
        models.replace_bugs('stable', {1004: {'created_at': since - hour,
                                              'status': 'NEW'}}, NOW)
        db.session.commit()
        api.forget_caches()
        rows, etag = self.channel_rows()
        # a bug filed the day before the spike counts for it (the crash
        # was ramping up before the dashboard flagged it)
        models.replace_bugs('spiking', {
            1005: {'created_at': since - 20 * hour, 'status': 'NEW'},
            1001: {'created_at': since - 30 * hour, 'status': 'RESOLVED',
                   'resolution': 'FIXED'}}, NOW)
        db.session.commit()
        api.forget_caches()
        graced = {b['id']: b['after'] for b in
                  self.channel_rows()[0]['spiking']['bugs']}
        self.assertEqual(graced, {1005: True, 1001: False})
        # a signature that appeared a few days before its spike: a bug
        # filed on its first crash is about the spike it grew into
        series = models.get_series('Firefox', 'release', 'spiking')
        series.first_seen = TODAY - datetime.timedelta(days=3)
        models.replace_bugs('spiking', {
            1006: {'created_at': since - 60 * hour, 'status': 'NEW'},
            1007: {'created_at': since - 80 * hour, 'status': 'NEW'}}, NOW)
        db.session.commit()
        api.forget_caches()
        fresh = {b['id']: b['after'] for b in
                 self.channel_rows()[0]['spiking']['bugs']}
        self.assertEqual(fresh, {1006: True, 1007: False})
        series.first_seen = TODAY - datetime.timedelta(days=200)
        models.replace_bugs('spiking', {
            1001: {'created_at': since - 30 * hour, 'status': 'RESOLVED',
                   'resolution': 'FIXED', 'summary': 'old one'},
            1002: {'created_at': since + hour, 'status': 'NEW',
                   'summary': 'filed for the spike', 'source': 'bugzilla'},
            1003: {'created_at': None, 'status': None}}, NOW)
        db.session.commit()
        api.forget_caches()
        rows, etag = self.channel_rows()
        bugs = rows['spiking']['bugs']
        # bug 1003 (nothing from Bugzilla: restricted) is not for everyone
        self.assertEqual([b['id'] for b in bugs], [1002, 1001])
        self.assertEqual([b['after'] for b in bugs], [True, False])
        self.assertEqual(bugs[0]['source'], 'bugzilla')
        self.assertEqual(bugs[1]['resolution'], 'FIXED')
        self.assertEqual(bugs[0]['created'], api.ts(since + hour))
        self.assertFalse(any(b['restricted'] for b in bugs))
        self.assertEqual(rows['stable']['bugs'][0]['after'], None)
        summary = self.client.get('/dashboard/api/summary').get_json()
        alert = [a for a in summary['alerts'] if a['signature'] == 'spiking']
        self.assertEqual(alert[0]['bugs'], bugs)
        self.assertNotIn('done', summary['channels'][0]['counts'])
        # a signed-in user sees the restricted bug too (id only), under
        # a different ETag so the anonymous response is not reused
        self.sign_in()
        rows, etag_user = self.channel_rows()
        self.assertNotEqual(etag, etag_user)
        bugs = rows['spiking']['bugs']
        self.assertEqual([b['id'] for b in bugs], [1002, 1001, 1003])
        self.assertEqual((bugs[2]['restricted'], bugs[2]['after'],
                          bugs[2]['status']), (True, None, None))
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&days=30',
                            headers={'If-None-Match': etag})
        self.assertEqual(r.status_code, 200)
        r = self.client.get('/dashboard/api/channel?product=Firefox'
                            '&channel=release&days=30',
                            headers={'If-None-Match': etag_user})
        self.assertEqual(r.status_code, 304)

    def test_bug_verdict_outlives_the_flag(self):
        """A row no longer flagged keeps judging its bugs against its most
        recent spike (within the score retention); one never flagged has
        no verdict."""
        ids = models.series_ids('Firefox', 'release', ['settled', 'quiet'])
        day = datetime.timedelta(days=1)

        def score(sgn, when, **kw):
            row = {'series_id': ids[sgn], 'day': when, 'as_of': NOW,
                   'partial': False, 'observed': 10, 'expected': 10.0,
                   'z': 0.0, 'severity': 'ok', 'is_new': False,
                   'storm': False}
            row.update(kw)
            models.upsert(models.Score, [row], ['series_id', 'day'])
        for sgn in ('settled', 'quiet'):
            score(sgn, TODAY)
        # a two-day spike that ended twelve days ago
        score('settled', TODAY - 13 * day, severity='spike',
              peak_severity='spike')
        score('settled', TODAY - 12 * day, severity='watch',
              peak_severity='watch')
        spike = datetime.datetime.combine(TODAY - 13 * day, datetime.time())
        hour = datetime.timedelta(hours=1)
        models.replace_bugs('settled', {
            3001: {'created_at': spike + 30 * hour, 'status': 'NEW'},
            3002: {'created_at': spike - 30 * hour, 'status': 'RESOLVED',
                   'resolution': 'FIXED'}}, NOW)
        models.replace_bugs('quiet', {3003: {'created_at': spike + hour,
                                             'status': 'NEW'}}, NOW)
        db.session.commit()
        rows, _ = self.channel_rows()
        self.assertIsNone(rows['settled']['flag'])
        self.assertEqual([(b['id'], b['after'])
                          for b in rows['settled']['bugs']],
                         [(3001, True), (3002, False)])
        self.assertEqual(rows['quiet']['bugs'][0]['after'], None)

    def test_bug_verdict_borrowed_from_the_other_scope(self):
        """A row not flagged in its scope colours its bugs after the same
        signature's spike in the channel's other scope: the current-version
        series is young, the spike shows in the all scope."""
        seed_channel('Firefox', 'release@current', TODAY, NOW, seed=1)
        scoring.score_channel('Firefox', 'release@current', TODAY, NOW)
        # unflag 'spiking' in the current scope only
        sid = models.get_series('Firefox', 'release@current', 'spiking').id
        for day in (TODAY, TODAY - datetime.timedelta(days=1)):
            models.upsert(models.Score, [{
                'series_id': sid, 'day': day, 'as_of': NOW, 'partial': False,
                'observed': 100, 'expected': 100.0, 'z': 0.0,
                'severity': 'ok', 'peak_severity': None, 'is_new': False,
                'storm': False, 'first_flagged_at': None,
                'last_flagged_at': None}], ['series_id', 'day'])
        start = datetime.datetime.combine(TODAY, datetime.time())
        hour = datetime.timedelta(hours=1)
        models.replace_bugs('spiking', {
            2001: {'created_at': start + hour, 'status': 'NEW'},
            2002: {'created_at': start - 30 * hour, 'status': 'NEW'}}, NOW)
        models.replace_bugs('stable', {2003: {'created_at': start + hour,
                                              'status': 'NEW'}}, NOW)
        db.session.commit()
        pairs = {None: [('Firefox', 'release'),
                        ('Firefox', 'release@current')],
                 'all': [('Firefox', 'release')],
                 'current': [('Firefox', 'release@current')]}
        with mock.patch.object(api.config, 'pairs',
                               side_effect=lambda scope=None: pairs[scope]):
            d = self.client.get('/dashboard/api/channel?product=Firefox'
                                '&channel=release&scope=current&days=30'
                                ).get_json()
        rows = {s['signature']: s for s in d['signatures']}
        self.assertIsNone(rows['spiking']['flag'])
        self.assertEqual([(b['id'], b['after'])
                          for b in rows['spiking']['bugs']],
                         [(2001, True), (2002, False)])
        # 'stable' spikes in neither scope: no verdict
        self.assertEqual(rows['stable']['bugs'][0]['after'], None)
        # flagged in both scopes, the current one since today only and the
        # all one since yesterday: the spike started yesterday
        yesterday = TODAY - datetime.timedelta(days=1)
        models.upsert(models.Score, [{
            'series_id': sid, 'day': TODAY, 'as_of': NOW, 'partial': True,
            'observed': 400, 'expected': 100.0, 'z': 9.0,
            'severity': 'spike', 'peak_severity': 'spike', 'is_new': False,
            'storm': False, 'first_flagged_at': NOW}], ['series_id', 'day'])
        all_sid = models.get_series('Firefox', 'release', 'spiking').id
        models.upsert(models.Score, [{
            'series_id': all_sid, 'day': yesterday, 'as_of': NOW,
            'partial': False, 'observed': 300, 'expected': 100.0, 'z': 7.0,
            'severity': 'spike', 'peak_severity': 'spike', 'is_new': False,
            'storm': False}], ['series_id', 'day'])
        models.replace_bugs('spiking', {2004: {
            'created_at': start - 12 * hour, 'status': 'NEW'}}, NOW)
        db.session.commit()
        api.forget_caches()
        with mock.patch.object(api.config, 'pairs',
                               side_effect=lambda scope=None: pairs[scope]):
            d = self.client.get('/dashboard/api/channel?product=Firefox'
                                '&channel=release&scope=current&days=30'
                                ).get_json()
        row = {s['signature']: s for s in d['signatures']}['spiking']
        self.assertEqual(row['flag']['day'], TODAY.isoformat())
        self.assertEqual([(b['id'], b['after']) for b in row['bugs']],
                         [(2004, True)])

    def test_flag_window(self):
        """Yesterday's flags stay listed for 48 h (scores are per UTC day:
        without this the page is empty in the European morning)."""
        yesterday = TODAY - datetime.timedelta(days=1)
        sid = models.get_series('Firefox', 'release', 'stable').id

        def put(**kw):
            row = {'series_id': sid, 'day': yesterday, 'as_of': NOW,
                   'partial': False, 'elapsed': 1.0, 'observed': 430,
                   'expected': 98.0, 'expected_day': 98.0, 'z': 12.0,
                   'ratio': 4.39, 'excess': 332.0, 'severity': 'ok',
                   'is_new': False, 'storm': False, 'first_flagged_at': None,
                   'last_flagged_at': None, 'peak_severity': None,
                   'peak_z': None, 'peak_excess': None, 'peak_at': None}
            row.update(kw)
            models.upsert(models.Score, [row], ['series_id', 'day'])
            db.session.commit()
            api.forget_caches()

        def view():
            summary = self.client.get('/dashboard/api/summary').get_json()
            alert = [a for a in summary['alerts']
                     if a['signature'] == 'stable']
            ch = self.client.get('/dashboard/api/channel?product=Firefox'
                                 '&channel=release&days=30').get_json()
            row = {s['signature']: s for s in ch['signatures']}['stable']
            return summary, alert[0] if alert else None, row, ch['counts']

        summary, alert, row, counts = view()
        self.assertEqual(summary['flag_window_hours'], 48)
        self.assertIsNone(row['flag'])
        self.assertIsNone(alert)
        base_major = counts['major']
        # flagged until the end of yesterday: shown as yesterday's major
        put(severity='major', peak_severity='major', peak_z=12.0,
            peak_at=datetime.datetime(2026, 9, 1, 9, 30),
            first_flagged_at=datetime.datetime(2026, 9, 1, 9, 12),
            last_flagged_at=datetime.datetime(2026, 9, 1, 23, 57))
        summary, alert, row, counts = view()
        self.assertEqual(row['severity'], 'ok')  # today's own state
        self.assertEqual(row['flag']['severity'], 'major')
        self.assertEqual(row['flag']['day'], '2026-09-01')
        self.assertEqual(row['flag']['observed'], 430)
        self.assertEqual(row['flag']['since'], '2026-09-01T09:12:00Z')
        self.assertIsNone(row['flag']['peak'])
        self.assertEqual(row['flagged_days'], 1)
        self.assertIsNotNone(alert)
        self.assertEqual(alert['flag']['severity'], 'major')
        self.assertEqual(counts['major'], base_major + 1)
        # sorted with the flag: a carried-over major ranks with the majors
        ch = self.client.get('/dashboard/api/channel?product=Firefox'
                             '&channel=release&days=30').get_json()
        names = [s['signature'] for s in ch['signatures']]
        ranks = [scoring.RANK[api.flag_severity(s)] for s in ch['signatures']]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(ranks[names.index('stable')], scoring.RANK['major'])
        # 49 hours after the last flagged run: gone
        put(severity='major', peak_severity='major',
            last_flagged_at=NOW - datetime.timedelta(hours=49))
        summary, alert, row, counts = view()
        self.assertIsNone(row['flag'])
        self.assertIsNone(alert)
        # a transient spike (stepped down to ok during the day) counts
        # through its peak; its time falls back to the peak's
        put(severity='ok', peak_severity='spike', peak_z=6.0,
            peak_excess=200.0, peak_at=datetime.datetime(2026, 9, 1, 14, 0))
        summary, alert, row, counts = view()
        self.assertEqual(row['flag']['severity'], 'spike')
        self.assertEqual(row['flag']['peak']['z'], 6.0)
        self.assertEqual(row['flag']['at'], '2026-09-01T14:00:00Z')
        # a drop has no peak: the end of its day is the reference time
        put(severity='drop', observed=10, z=-6.0, excess=-88.0)
        summary, alert, row, counts = view()
        self.assertEqual(row['flag']['severity'], 'drop')
        self.assertEqual(row['flag']['at'], '2026-09-02T00:00:00Z')
        # "new" yesterday is kept as well
        put(is_new=True)
        summary, alert, row, counts = view()
        self.assertEqual(row['flag']['severity'], 'ok')
        self.assertTrue(row['flag']['is_new'])
        self.assertIsNotNone(alert)
        self.assertEqual(counts['new'], 2)  # + the seeded "brand new"
        # today's own flag wins over a milder carried-over one
        spiking = models.get_series('Firefox', 'release', 'spiking').id
        models.upsert(models.Score, [{
            'series_id': spiking, 'day': yesterday, 'as_of': NOW,
            'partial': False, 'observed': 130, 'expected': 98.0, 'z': 3.2,
            'excess': 32.0, 'severity': 'watch', 'peak_severity': 'watch',
            'last_flagged_at': datetime.datetime(2026, 9, 1, 23, 57),
            'is_new': False, 'storm': False}], ['series_id', 'day'])
        db.session.commit()
        e = api.channel_scores('Firefox', 'release', TODAY)[spiking]
        flag = api.flag_of(e, NOW)
        self.assertEqual(flag['day'], '2026-09-02')
        self.assertEqual(flag['severity'], e['today'].severity)
        self.assertIn(flag['severity'], ('spike', 'major'))

    def test_html(self):
        r = self.client.get('/dashboard.html')
        self.assertEqual(r.status_code, 200)

    def test_robots(self):
        with self.client.get('/robots.txt') as r:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.mimetype, 'text/plain')
            self.assertEqual(r.get_data(as_text=True),
                             'User-agent: *\nDisallow: /\n')

    def test_summary_cached_per_run(self):
        """The summary is computed once per run: the first request (or
        the scheduler) stores it, the next ones read it; a new run is a
        miss.  Channel payloads are memoized in the process the same way."""
        run = api.last_run()
        d = self.client.get('/dashboard/api/summary').get_json()
        key, version = 'summary:all:anon', api.summary_version(run, TODAY)
        self.assertIsNotNone(models.get_cache(key, version))
        self.assertIsNone(models.get_cache(key, 'other'))
        with mock.patch.object(api, 'summary_payload',
                               side_effect=AssertionError('recomputed')):
            d2 = self.client.get('/dashboard/api/summary').get_json()
        self.assertEqual(d2['alerts'], d['alerts'])
        self.assertEqual(d2['channels'], d['channels'])
        # the scheduler warms every scope for both kinds of reader
        api.warm_summaries(run, TODAY, NOW)
        for scope in config.scopes():
            for who in ('anon', 'user'):
                self.assertIsNotNone(models.get_cache(
                    'summary:{}:{}'.format(scope, who), version), (scope, who))
        # another run: recomputed
        run2 = models.start_run()
        run2.started = run.started + datetime.timedelta(minutes=4)
        run2.status, run2.finished = 'ok', NOW + datetime.timedelta(minutes=5)
        run2.message = json.dumps({'pending_units': 0})
        db.session.commit()
        with mock.patch.object(api, 'summary_payload',
                               wraps=api.summary_payload) as sp:
            self.client.get('/dashboard/api/summary')
        self.assertEqual(sp.call_count, 1)
        # the channel view is memoized per run too
        url = '/dashboard/api/channel?product=Firefox&channel=release&days=60'
        d = self.client.get(url).get_json()
        with mock.patch.object(api, 'channel_payload',
                               side_effect=AssertionError('recomputed')):
            d2 = self.client.get(url).get_json()
        self.assertEqual(d2['signatures'], d['signatures'])

    def test_conditional_and_gzip(self):
        r = self.client.get('/dashboard/api/summary')
        etag = r.headers.get('ETag')
        self.assertTrue(etag)
        self.assertTrue(r.get_json()['data_version'])
        r2 = self.client.get('/dashboard/api/summary',
                             headers={'If-None-Match': etag})
        self.assertEqual(r2.status_code, 304)
        r3 = self.client.get('/dashboard/api/channel?product=Firefox'
                             '&channel=release&days=30',
                             headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r3.headers.get('Content-Encoding'), 'gzip')
        import gzip
        d = json.loads(gzip.decompress(r3.data))
        self.assertEqual(d['product'], 'Firefox')
        self.assertLess(len(r3.data), len(json.dumps(d)) / 3)
        # a different view has a different tag
        r4 = self.client.get('/dashboard/api/channel?product=Firefox'
                             '&channel=release&days=90',
                             headers={'If-None-Match': r3.headers['ETag']})
        self.assertEqual(r4.status_code, 200)
        # request strings never reach the header: a quoted signature works
        r5 = self.client.get('/dashboard/api/signature', query_string={
            'product': 'Firefox', 'channel': 'release',
            'signature': 'storm | 0x1'})
        self.assertEqual(r5.status_code, 200)
        self.assertNotIn('"0x', r5.headers['ETag'][1:-1])
        # the page's script and style are compressed too
        r6 = self.client.get('/dashboard/static/dashboard.css',
                             headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r6.headers.get('Content-Encoding'), 'gzip')
        self.assertIn(b'.card', gzip.decompress(r6.data))
        # the version flips when the data goes stale, so the banner shows
        with mock.patch.object(models, 'utcnow',
                               return_value=NOW + datetime.timedelta(
                                   hours=2)):
            r6 = self.client.get('/dashboard/api/summary',
                                 headers={'If-None-Match': etag})
            self.assertEqual(r6.status_code, 200)
            self.assertEqual(r6.get_json()['data_health']['status'],
                             'stale_local')


if __name__ == '__main__':
    unittest.main()
