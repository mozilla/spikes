# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import json
import os
import unittest

from spikes.dashboard import socorro


FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def load(name):
    with open(os.path.join(FIXTURES, name)) as In:
        return json.load(In)


class SocorroParsingTest(unittest.TestCase):

    def test_query_params(self):
        start = datetime.date(2026, 9, 1)
        end = datetime.date(2026, 9, 2)
        p = socorro.query_params('day', 'Firefox', 'nightly', start, end)
        self.assertEqual(p['product'], 'Firefox')
        self.assertEqual(p['release_channel'], 'nightly')
        self.assertEqual(p['date'], ['>=2026-09-01', '<2026-09-02'])
        self.assertEqual(p['_histogram.date'],
                         ['signature', '_cardinality.install_time'])
        self.assertEqual(p['_histogram_interval.date'], '1h')
        self.assertEqual(p['_aggs.signature'], '_cardinality.install_time')
        self.assertEqual(p['_aggs.product'], '_cardinality.install_time')
        self.assertEqual(p['_results_number'], 0)
        self.assertIn('!Infobar', p['submitted_from'])
        p = socorro.query_params('daily', 'Fenix', 'beta', start, end)
        self.assertEqual(p['_histogram_interval.date'], '1d')
        self.assertNotIn('_aggs.signature', p)
        p = socorro.query_params('hourly_total', 'Fenix', 'beta', start, end)
        self.assertEqual(p['_histogram.date'], 'product')
        with self.assertRaises(ValueError):
            socorro.query_params('nope', 'Firefox', 'nightly', start, end)

    def test_recent_and_installs_params(self):
        start = datetime.datetime(2026, 9, 2, 10)
        end = datetime.datetime(2026, 9, 2, 11, 45, 30)
        p = socorro.query_params('recent', 'Firefox', 'esr', start, end)
        self.assertEqual(p['date'], ['>=2026-09-02T10:00:00',
                                     '<2026-09-02T11:45:30'])
        self.assertEqual(p['_histogram_interval.date'], '1h')
        # window installs for signatures new to the day, no day total
        self.assertEqual(p['_aggs.signature'], '_cardinality.install_time')
        self.assertNotIn('_aggs.product', p)
        p = socorro.query_params('installs', 'Firefox', 'esr',
                                 datetime.date(2026, 9, 2),
                                 datetime.date(2026, 9, 3))
        self.assertEqual(p['_aggs.product'], '_cardinality.install_time')
        self.assertNotIn('_histogram.date', p)

    def test_parse_recent(self):
        data = load('socorro_day.json')
        res = socorro.parse_recent(data)
        day = res[datetime.date(2026, 9, 1)]
        bucket13 = [b for b in data['facets']['histogram_date']
                    if b['term'].startswith('2026-09-01T13')][0]
        self.assertEqual(day['hourly_total'][13], bucket13['count'])
        self.assertEqual(sum(day['hourly_total'].values()), data['total'])
        self.assertGreater(day['hourly_installs'][13], 0)
        self.assertGreater(
            day['signatures']['libc.so.6 | cuEGLApiInit'][13], 500)

    def test_parse_installs(self):
        data = load('socorro_day.json')
        res = socorro.parse_installs(data, size=1000)
        self.assertEqual(res['installs'], 1420)
        self.assertEqual(res['signatures']['libc.so.6 | cuEGLApiInit'],
                         (823, 9))
        self.assertIsNone(res['cutoff'])

    def test_link(self):
        url = socorro.link('Firefox', 'release', datetime.date(2026, 9, 1),
                           'foo | bar')
        self.assertIn('crash-stats.mozilla.org/search/', url)
        self.assertIn('signature=%3Dfoo', url)
        self.assertIn('submitted_from=%21Infobar', url)
        self.assertIn('date=%3E%3D2026-09-01', url)
        self.assertTrue(url.endswith('#crash-reports'))

    def test_normalize_signature(self):
        self.assertEqual(socorro.normalize_signature('foo | bar'),
                         'foo | bar')
        # an empty signature must never collide with the total's key ('')
        self.assertEqual(socorro.normalize_signature(''),
                         socorro.EMPTY_SIGNATURE)
        self.assertEqual(socorro.normalize_signature('  '),
                         socorro.EMPTY_SIGNATURE)
        norm = socorro.normalize_signature('foo | 0x1a2B | bar')
        self.assertEqual(norm, '"foo | "0x[0-9a-fA-F]+" | bar"')
        self.assertEqual(socorro.normalize_signature('foo | 0xdead'),
                         socorro.normalize_signature('foo | 0xbeef'))
        self.assertEqual(socorro.search_term('foo | bar'), '=foo | bar')
        self.assertEqual(socorro.search_term(norm), '@' + norm)

    def test_noise(self):
        pats = socorro.noise_patterns('release')
        self.assertTrue(socorro.is_noise('IPCError-browser | ShutDownKill',
                                         pats))
        self.assertFalse(socorro.is_noise('OOM | small', pats))

    def test_parse_day(self):
        data = load('socorro_day.json')
        res = socorro.parse_day(data, size=1000)
        self.assertEqual(res['day'], datetime.date(2026, 9, 1))
        self.assertEqual(res['total'], data['total'])
        self.assertEqual(len(res['hourly_total']), 24)
        self.assertEqual(sum(res['hourly_total']), data['total'])
        self.assertEqual(res['hours_capped'], 0)
        # fewer than 1000 signatures returned: nothing censored
        self.assertIsNone(res['cutoff'])
        top = 'libc.so.6 | cuEGLApiInit'
        info = res['signatures'][top]
        self.assertEqual(info['crashes'], sum(info['hourly']))
        self.assertGreater(info['hourly'][13], 500)
        self.assertGreater(info['installs'], 0)
        self.assertLessEqual(info['installs'], info['crashes'])
        # channel-level distinct installs, per day and per hour
        self.assertEqual(res['installs'], 1420)
        self.assertEqual(len(res['hourly_installs']), 24)
        self.assertGreater(res['hourly_installs'][13], 0)
        self.assertLessEqual(res['hourly_installs'][13],
                             res['hourly_total'][13])
        # censoring detected when the facet list is full
        res = socorro.parse_day(data, size=6)
        self.assertEqual(res['cutoff'],
                         data['facets']['signature'][-1]['count'])

    def test_parse_hourly_total(self):
        data = load('socorro_hourly.json')
        res = socorro.parse_hourly_total(data)
        hours = res[datetime.date(2026, 9, 1)]
        self.assertEqual(sum(hours), data['total'])
        self.assertEqual(hours[13], 978)

    def test_parse_daily(self):
        data = load('socorro_daily.json')
        res = socorro.parse_daily(data, size=6)
        self.assertEqual(len(res), 14)
        first = res[datetime.date(2026, 8, 18)]
        self.assertEqual(first['total'], 21458)
        self.assertEqual(first['signatures']['OOM | small'], 2646)
        self.assertIsNotNone(first['cutoff'])
        res = socorro.parse_daily(data, size=1000)
        self.assertIsNone(res[datetime.date(2026, 8, 18)]['cutoff'])

    def test_addresses_merged(self):
        data = {'total': 3, 'errors': [], 'facets': {
            'signature': [
                {'term': 'foo | 0x1', 'count': 2,
                 'facets': {'cardinality_install_time': {'value': 2}}},
                {'term': 'foo | 0x2', 'count': 1,
                 'facets': {'cardinality_install_time': {'value': 1}}}],
            'histogram_date': [
                {'term': '2026-09-01T05:00:00Z', 'count': 3,
                 'facets': {'signature': [{'term': 'foo | 0x1', 'count': 2},
                                          {'term': 'foo | 0x2',
                                           'count': 1}]}}]}}
        res = socorro.parse_day(data, size=1000)
        self.assertEqual(list(res['signatures']),
                         ['"foo | "0x[0-9a-fA-F]+'])
        info = res['signatures']['"foo | "0x[0-9a-fA-F]+']
        # installs are not additive across variants: lower bound (max)
        self.assertEqual((info['crashes'], info['installs']), (3, 2))
        self.assertEqual(info['hourly'][5], 3)
        # no channel-level cardinality in this response
        self.assertIsNone(res['installs'])
        self.assertIsNone(res['hourly_installs'])

    def test_errors_raise(self):
        with self.assertRaises(ValueError):
            socorro.parse_day({'errors': ['bad'], 'facets': {}})
        with self.assertRaises(ValueError):
            socorro.parse_daily('not json')


if __name__ == '__main__':
    unittest.main()
