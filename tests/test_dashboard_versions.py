# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Version cycles of the ``current`` scope (spikes/dashboard/versions.py):
calendars from fixture feeds, cycles per channel, Socorro filters, the
planner's splitting and the release-phase cycle.  No network."""

import datetime
import unittest
from unittest import mock

import numpy as np

from spikes.dashboard import collect, config, socorro, versions

from tests.test_dashboard_pipeline import DBTestCase


D = datetime.date
TODAY = D(2026, 9, 3)

# a slice of product-details (real dates of summer 2026)
MAJOR = {'152.0': '2026-06-16', '153.0': '2026-07-21', '154.0': '2026-08-18',
         '155.0': '2026-09-01', '140.0': '2025-06-24', '153.0.1': 'x'}
DEV = {'153.0b1': '2026-06-17', '154.0b1': '2026-07-22',
       '155.0b1': '2026-08-17', '156.0b1': '2026-08-31',
       '156.0b2': '2026-09-02'}
STAB = {'140.14.0': '2026-08-18', '140.15.0': '2026-09-01',
        '140.13.0': '2026-07-21', '153.1.0': '2026-08-18',
        '153.2.0': '2026-09-01', '154.0.1': '2026-08-25',
        '152.0.6': '2026-07-14'}
SCHEDULES = {
    155: {'nightly_start': '2026-07-21 00:00:00+00:00',
          'beta_1': '2026-08-17 00:00:00+00:00',
          'release': '2026-09-01 14:00:00+00:00'},
    156: {'nightly_start': '2026-08-16 00:00:00+00:00',
          'beta_1': '2026-08-31 13:00:00+00:00',
          'release': '2026-09-15 14:00:00+00:00'},
    157: {'nightly_start': '2026-08-27 16:00:00+00:00',
          'beta_1': '2026-09-14 00:00:00+00:00',
          'release': '2026-10-13 14:00:00+00:00'},
}


def calendar():
    cal = versions.calendar_from_feeds(MAJOR, DEV, STAB)
    return versions.apply_schedules(cal, SCHEDULES, TODAY)


class CalendarTest(unittest.TestCase):

    def test_feeds(self):
        cal = calendar()
        self.assertEqual(cal.majors[155], D(2026, 9, 1))
        self.assertNotIn('153.0.1', cal.majors)
        self.assertEqual(cal.betas[156], D(2026, 8, 31))
        self.assertEqual(cal.nightly_starts[157], D(2026, 8, 27))
        # no schedule for 154: the day before 153.0b1
        self.assertEqual(cal.nightly_starts[154], D(2026, 6, 16))
        self.assertEqual(cal.esr_points[140][15], D(2026, 9, 1))
        self.assertEqual(cal.esr_points[140][0], D(2025, 6, 24))
        self.assertEqual(cal.esr_points[153][0], D(2026, 7, 21))
        self.assertEqual(cal.future, {
            'release': {156: D(2026, 9, 15), 157: D(2026, 10, 13)},
            'beta': {157: D(2026, 9, 14)}})

    def test_train_cycles(self):
        cal = calendar()
        since = D(2026, 7, 1)
        rel = versions.compute_cycles(cal, 'release', since)
        self.assertEqual([(c['start'], c['end'], c['label']) for c in rel], [
            (D(2026, 6, 16), D(2026, 7, 21), '152'),
            (D(2026, 7, 21), D(2026, 8, 18), '153'),
            (D(2026, 8, 18), D(2026, 9, 1), '154'),
            (D(2026, 9, 1), D(2026, 9, 15), '155'),
            (D(2026, 9, 15), D(2026, 10, 13), '156'),   # planned
            (D(2026, 10, 13), None, '157')])
        self.assertEqual(rel[3]['params'], {'major_version': 155})
        beta = versions.compute_cycles(cal, 'beta', since)
        self.assertEqual([(c['start'], c['label']) for c in beta], [
            (D(2026, 6, 17), '153'), (D(2026, 7, 22), '154'),
            (D(2026, 8, 17), '155'), (D(2026, 8, 31), '156'),
            (D(2026, 9, 14), '157')])
        nightly = versions.compute_cycles(cal, 'nightly', since)
        self.assertEqual([(c['start'], c['label']) for c in nightly], [
            (D(2026, 6, 16), '154'), (D(2026, 7, 21), '155'),
            (D(2026, 8, 16), '156'), (D(2026, 8, 27), '157')])
        self.assertIsNone(nightly[-1]['end'])

    def test_esr_cycles(self):
        cal = calendar()
        esr = versions.compute_cycles(cal, 'esr', D(2026, 8, 1),
                                      overlap_weeks=12)
        # 153 ESR ships 2026-07-21 but becomes the current train 12 weeks
        # later (2026-10-13); until then 140's point releases are current
        # the point releases still to come are planned on the release
        # days of the schedules (156: 09-15, 157: 10-13)
        self.assertEqual([(c['start'], c['end'], c['label']) for c in esr], [
            (D(2026, 7, 21), D(2026, 8, 18), '140.13'),
            (D(2026, 8, 18), D(2026, 9, 1), '140.14'),
            (D(2026, 9, 1), D(2026, 9, 15), '140.15'),
            (D(2026, 9, 15), D(2026, 10, 13), '140.16'),
            (D(2026, 10, 13), None, '153.4')])
        self.assertEqual(esr[2]['params']['version'][:3],
                         ['140.15esr', '140.15.0esr', '140.15.1esr'])
        self.assertEqual(len(esr[2]['params']['version']), 11)

    def test_planned_esr_points(self):
        cal = calendar()
        self.assertEqual(cal.esr_points[140][16], D(2026, 9, 15))
        self.assertEqual(cal.esr_points[140][17], D(2026, 10, 13))
        self.assertEqual(cal.esr_points[153][3], D(2026, 9, 15))
        self.assertEqual(cal.esr_points[153][4], D(2026, 10, 13))
        # idempotent, and a train that stopped shipping is left alone
        old = {0: D(2024, 7, 9), 13: D(2025, 6, 24)}
        cal.esr_points[128] = dict(old)
        cal.plan_esr_points(TODAY)
        self.assertEqual(max(cal.esr_points[140]), 17)
        self.assertEqual(cal.esr_points[128], old)

    def test_schedules_wanted(self):
        cal = versions.calendar_from_feeds(MAJOR, {}, {})
        previous = config.override(history_days=10)
        try:
            # 10 + 60 days back from 2026-09-03 is 2026-06-25: 152 is the
            # major current then; up to the nightly of today plus one
            self.assertEqual(versions.schedules_wanted(cal, TODAY),
                             [152, 153, 154, 155, 156, 157, 158])
        finally:
            config.restore(previous)


class Row:
    def __init__(self, start, end, label, params):
        self.start, self.end, self.label, self.params = (start, end, label,
                                                         params)


def release_cycles():
    cal = calendar()
    return versions.Cycles([Row(c['start'], c['end'], c['label'], c['params'])
                            for c in versions.compute_cycles(
                                cal, 'release', D(2026, 6, 1))])


class CyclesTest(unittest.TestCase):

    def test_at_split_phase(self):
        cyc = release_cycles()
        self.assertEqual(cyc.at(D(2026, 8, 31)).label, '154')
        self.assertEqual(cyc.at(D(2026, 9, 1)).label, '155')
        self.assertEqual(cyc.at(D(2026, 1, 1)).label, '140')  # sparse fixture
        self.assertIsNone(cyc.at(D(2025, 1, 1)))
        self.assertEqual(cyc.next_start(TODAY), D(2026, 9, 15))
        split = cyc.split(D(2026, 8, 25), D(2026, 9, 5))
        self.assertEqual([(a, b, c.label) for a, b, c in split], [
            (D(2026, 8, 25), D(2026, 9, 1), '154'),
            (D(2026, 9, 1), D(2026, 9, 5), '155')])
        # a range across a boundary; days before every cycle have none
        split = cyc.split(D(2026, 6, 14), D(2026, 6, 17))
        self.assertEqual([c.label for _, _, c in split], ['140', '152'])
        split = cyc.split(D(2025, 6, 23), D(2025, 6, 25))
        self.assertIsNone(split[0][2])
        self.assertEqual(split[1][2].label, '140')
        phase = cyc.phase([D(2026, 9, 1), D(2026, 9, 3), D(2026, 8, 31),
                           D(2026, 8, 17)])
        # 0 on the release day, 2 two days later; 154 lasted 14 days; the
        # 27th day of 153; a longer cycle is clipped to 27
        np.testing.assert_array_equal(phase, [0, 2, 13, 27])
        self.assertEqual(cyc.phase([D(2026, 9, 30)])[0], 15)
        long = versions.Cycles([Row(D(2026, 1, 1), None, '1', {})])
        self.assertEqual(long.phase([D(2026, 3, 1)])[0], 27)
        # outside any cycle: the calendar phase (never an error)
        self.assertEqual(int(cyc.phase([D(2025, 1, 1)])[0]),
                         int(versions.seasonal.cycle_phase([D(2025, 1, 1)])
                             [0]))

    def test_socorro_params(self):
        cyc = release_cycles()
        p = socorro.query_params('day', 'Firefox', 'release@current',
                                 D(2026, 9, 2), D(2026, 9, 3),
                                 cyc.at(D(2026, 9, 2)).params)
        self.assertEqual(p['release_channel'], 'release')
        self.assertEqual(p['major_version'], 155)
        p = socorro.query_params('daily', 'Firefox', 'esr@current',
                                 D(2026, 9, 2), D(2026, 9, 3),
                                 {'version': ['140.15.0esr']})
        self.assertEqual(p['release_channel'], 'esr')
        self.assertEqual(p['version'], ['140.15.0esr'])
        # the all scope is untouched
        p = socorro.query_params('day', 'Firefox', 'release', D(2026, 9, 2),
                                 D(2026, 9, 3))
        self.assertNotIn('major_version', p)
        self.assertEqual(socorro.noise_patterns('release@current'),
                         socorro.noise_patterns('release'))

    def test_config_keys(self):
        self.assertEqual(config.channel_key('release'), 'release')
        self.assertEqual(config.channel_key('release', 'current'),
                         'release@current')
        self.assertEqual(config.split_channel('release@current'),
                         ('release', 'current'))
        self.assertEqual(config.split_channel('esr'), ('esr', 'all'))
        previous = config.override(products=['Firefox'],
                                   channels=['nightly', 'release'])
        try:
            self.assertEqual(config.pairs(), [
                ('Firefox', 'nightly'), ('Firefox', 'release'),
                ('Firefox', 'nightly@current'),
                ('Firefox', 'release@current')])
            self.assertEqual(config.pairs('current'),
                             [('Firefox', 'nightly@current'),
                              ('Firefox', 'release@current')])
            config.override(scopes=['all'])
            self.assertEqual(len(config.pairs()), 2)
        finally:
            config.restore(previous)


class StoredCyclesTest(DBTestCase):
    """The stored cycles drive the planner and the crash-stats links."""

    def setUp(self):
        super().setUp()
        from spikes.dashboard import models
        self.models = models
        cal = calendar()
        now = datetime.datetime(2026, 9, 3, 12)
        for channel in ('release', 'nightly'):
            models.replace_cycles('Firefox', channel, versions.compute_cycles(
                cal, channel, D(2026, 6, 1)), now)
        versions._cache.clear()

    def tearDown(self):
        versions._cache.clear()
        super().tearDown()

    def test_chart_markers(self):
        """Every channel's chart marks its own version boundaries: the
        merge days on nightly, the first betas on beta (from the stored
        cycles), up to today; the upcoming one comes from the schedule
        or the next stored cycle, in the same style."""
        from spikes.dashboard import api
        self.models.replace_cycles('Firefox', 'beta', versions.compute_cycles(
            calendar(), 'beta', D(2026, 6, 1)), datetime.datetime(2026, 9, 3))
        versions._cache.clear()
        with mock.patch.object(self.models, 'utcnow',
                               return_value=datetime.datetime(2026, 9, 3, 12)):
            self.assertEqual(api.releases(D(2026, 8, 1), 'nightly'), [
                {'date': '2026-08-16', 'version': '156.0a1'},
                {'date': '2026-08-27', 'version': '157.0a1'}])
            self.assertEqual(api.releases(D(2026, 8, 1), 'beta@current'), [
                {'date': '2026-08-17', 'version': '155.0b1'},
                {'date': '2026-08-31', 'version': '156.0b1'}])
        schedules = {'nightly': {'version': '157.0',
                                 'merge_day': '2026-09-10 16:00:00+00:00',
                                 'beta_1': '2026-09-14 13:00:00+00:00'},
                     'beta': {'version': '156.0',
                              'release': '2026-09-15 14:00:00+00:00'}}
        with mock.patch.object(api, '_schedule', schedules.get):
            nxt = {ch: api.next_release('Firefox', ch, TODAY)
                   for ch in ('nightly', 'beta', 'release', 'esr')}
        self.assertEqual(nxt, {
            'nightly': {'date': D(2026, 9, 10), 'version': '158.0a1'},
            'beta': {'date': D(2026, 9, 14), 'version': '157.0b1'},
            'release': {'date': D(2026, 9, 15), 'version': '156.0'},
            'esr': {'date': D(2026, 9, 15), 'version': 'ESR point release'}})
        # the next stored cycle is the boundary in both scopes
        self.models.replace_cycles('Firefox', 'esr', versions.compute_cycles(
            calendar(), 'esr', D(2026, 6, 1)), datetime.datetime(2026, 9, 3))
        versions._cache.clear()
        with mock.patch.object(api, 'next_release', return_value=None):
            day, marker = api.horizon_for('Firefox', 'release@current', TODAY)
            self.assertEqual((day, marker['version']),
                             (D(2026, 9, 15), '156.0'))
            day, marker = api.horizon_for('Firefox', 'esr', TODAY)
            self.assertEqual((day, marker['version']),
                             (D(2026, 9, 15), '140.16 esr'))

    def test_plan_splits_history_at_releases(self):
        units = collect.plan('Firefox', 'release@current', TODAY,
                             history_days=30, recent_days=7, chunk_days=14)
        # today and the 7 recent days carry their cycle
        for u in units[:8]:
            self.assertEqual(u.kind, 'day')
            self.assertEqual(u.label, '155' if u.day >= D(2026, 9, 1)
                             else '154')
            self.assertEqual(u.params()['major_version'], int(u.label))
        # 23 history days from 2026-08-04: 14-day chunks, split on 08-18
        # and 09-01 (the 154 and 155 releases)
        history = [(u.kind, u.start, u.end, u.label) for u in units[8:]]
        self.assertEqual(history, [
            ('daily', D(2026, 8, 4), D(2026, 8, 18), '153'),
            ('hourly_total', D(2026, 8, 4), D(2026, 8, 18), '153'),
            ('daily', D(2026, 8, 18), D(2026, 8, 27), '154'),
            ('hourly_total', D(2026, 8, 18), D(2026, 8, 27), '154')])
        # a day stored under another cycle label is fetched again
        self.models.upsert_day('Firefox', 'release@current', D(2026, 8, 20),
                               crashes=1, as_of=datetime.datetime(2026, 9, 1),
                               final=True, complete=True, version='153')
        self.models.upsert_day('Firefox', 'release@current', D(2026, 8, 21),
                               crashes=1, as_of=datetime.datetime(2026, 9, 1),
                               final=True, complete=True, version='154')
        units = collect.plan('Firefox', 'release@current', TODAY,
                             history_days=30, recent_days=7, chunk_days=14)
        starts = {(u.kind, u.start) for u in units}
        self.assertIn(('daily', D(2026, 8, 18)), starts)      # 18..21
        self.assertIn(('daily', D(2026, 8, 22)), starts)      # 22..27
        self.assertNotIn(('daily', D(2026, 8, 21)), starts)
        # the all scope plans as before, without filters
        units = collect.plan('Firefox', 'release', TODAY, history_days=30,
                             recent_days=7, chunk_days=14)
        self.assertTrue(all(u.cycle is None for u in units))
        self.assertNotIn('major_version', units[0].params())
        # a channel whose cycles are unknown plans nothing
        self.assertEqual(collect.plan('Firefox', 'beta@current', TODAY,
                                      history_days=30), [])

    def test_helpers_and_link(self):
        self.assertEqual(versions.label_for('Firefox', 'release@current',
                                            TODAY), '155')
        self.assertIsNone(versions.label_for('Firefox', 'release', TODAY))
        self.assertEqual(versions.params_for('Firefox', 'nightly@current',
                                             TODAY), {'major_version': 157})
        comps = versions.components_for('Firefox', 'release@current')
        cycle = versions.seasonal.component(comps, 'cycle')
        self.assertEqual(int(cycle.phase([D(2026, 9, 3)])[0]), 2)
        self.assertIs(versions.components_for('Firefox', 'release'),
                      versions.seasonal.COMPONENTS)
        url = socorro.link('Firefox', 'release@current', D(2026, 9, 2), 'sig')
        self.assertIn('major_version=155', url)
        self.assertIn('release_channel=release', url)
        self.assertNotIn('major_version',
                         socorro.link('Firefox', 'release', D(2026, 9, 2)))
