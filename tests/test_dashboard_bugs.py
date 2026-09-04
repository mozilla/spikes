# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The bug look-up of the flagged signatures (spikes/dashboard/bugs.py):
the helpers, and the policy against an in-memory database with the
network replaced by fakes."""

import datetime
import unittest
from unittest import mock
from urllib.parse import parse_qsl

from spikes import db
from spikes.dashboard import bugs, config, models

from tests.test_dashboard_pipeline import DBTestCase


TODAY = datetime.date(2026, 9, 2)
NOW = datetime.datetime(2026, 9, 2, 12, 0, 0)
HOUR = datetime.timedelta(hours=1)


class HelpersTest(unittest.TestCase):

    def test_signatures_of(self):
        field = ('[@ shutdownhang | mozilla::SpinEventLoopUntil |\n'
                 '   nsThreadManager::Shutdown ]\n[@OOM | small]')
        self.assertEqual(bugs.signatures_of(field), [
            'shutdownhang | mozilla::SpinEventLoopUntil | '
            'nsThreadManager::Shutdown', 'OOM | small'])
        self.assertEqual(bugs.signatures_of(None), [])

    def test_attach_needs_an_exact_listing(self):
        wanted = {bugs.normalize(s): s for s in ('OOM | small', 'a | b')}
        found = {}
        bugs.attach({'id': 7, 'cf_crash_signature': '[@ OOM | small ]',
                     'creation_time': '2026-09-01T10:00:00Z',
                     'status': 'NEW', 'resolution': '', 'summary': 'x'},
                    wanted, found)
        # a substring match ("OOM | small | more") does not count
        bugs.attach({'id': 8, 'cf_crash_signature': '[@ OOM | small | more ]',
                     'creation_time': '2026-09-01T10:00:00Z',
                     'status': 'NEW', 'resolution': ''}, wanted, found)
        self.assertEqual(list(found), ['OOM | small'])
        self.assertEqual(found['OOM | small'][7], {
            'created_at': datetime.datetime(2026, 9, 1, 10, 0, 0),
            'status': 'NEW', 'resolution': None, 'summary': 'x'})

    def test_chunks_by_chars(self):
        sigs = ['a' * 900, 'b' * 900, 'c' * 900, 'd' * 3000]
        chunks = list(bugs.chunks_by_chars(sigs, chars=2000))
        self.assertEqual([len(c) for c in chunks], [2, 1, 1])
        self.assertEqual(chunks[-1], ['d' * 3000])  # never dropped

    def test_search_query(self):
        q = dict(parse_qsl(bugs.search_query(['a | b', 'c'])))
        self.assertEqual(q['j_top'], 'OR')
        self.assertEqual((q['f1'], q['o1'], q['v1']),
                         ('cf_crash_signature', 'substring', 'a | b'))
        self.assertEqual(q['v2'], 'c')
        self.assertIn('cf_crash_signature', q['include_fields'])
        self.assertEqual(q['limit'], str(bugs.SEARCH_LIMIT))


class PolicyTest(DBTestCase):

    def flag(self, channel, signatures):
        ids = models.series_ids('Firefox', channel, signatures)
        for sgn in signatures:
            models.upsert(models.Score, [{
                'series_id': ids[sgn], 'day': TODAY, 'as_of': NOW,
                'partial': True, 'observed': 40, 'expected': 5.0, 'z': 8.0,
                'severity': 'spike', 'is_new': False, 'storm': False,
                'first_flagged_at': NOW - 3 * HOUR}], ['series_id', 'day'])
        db.session.commit()

    def test_lookup(self):
        shared, lonely = 'shared | signature', 'lonely | signature'
        # the same signature flagged in two channel keys: one look-up
        self.flag('release', [shared, lonely])
        self.flag('release@current', [shared])
        calls = {'socorro': [], 'search': [], 'details': []}

        def socorro(signatures, fetcher):
            calls['socorro'].append(list(signatures))
            fetcher.count += 1
            return {s: ({123, 456} if s == shared else set())
                    for s in signatures}

        def search(signatures, fetcher):
            calls['search'].append(list(signatures))
            return {s: {789: {'created_at': NOW + HOUR, 'status': 'NEW',
                              'resolution': None, 'summary': 'fresh'}}
                    for s in signatures}

        def details(ids, fetcher):
            calls['details'].append(sorted(ids))
            return {123: {'created_at': datetime.datetime(2026, 8, 1),
                          'status': 'RESOLVED', 'resolution': 'FIXED',
                          'summary': 'old'},
                    456: {'created_at': NOW - HOUR, 'status': 'NEW',
                          'resolution': None, 'summary': 'new'}}

        patches = [mock.patch.object(bugs, 'fetch_socorro', socorro),
                   mock.patch.object(bugs, 'search_bugzilla', search),
                   mock.patch.object(bugs, 'fetch_details', details)]
        for p in patches:
            p.start()
        try:
            counter = bugs.Counter()
            self.assertEqual(bugs.refresh(TODAY, NOW, counter), 2)
            self.assertEqual(counter.count, 1)
            # both signatures in one Socorro pass, the search only for
            # the one Socorro knows nothing about, one details fetch
            self.assertEqual(calls, {'socorro': [[lonely, shared]],
                                     'search': [[lonely]],
                                     'details': [[123, 456]]})
            stored = models.load_bugs([shared, lonely])
            self.assertEqual([b.bug_id for b in stored[shared]], [456, 123])
            self.assertEqual(stored[shared][0].source, 'socorro')
            self.assertEqual(stored[shared][1].resolution, 'FIXED')
            self.assertEqual([(b.bug_id, b.source) for b in stored[lonely]],
                             [(789, 'bugzilla')])
            # nothing is due again before bugs_refresh_hours
            self.assertEqual(bugs.refresh(TODAY, NOW + HOUR / 2), 0)
            self.assertEqual(len(calls['socorro']), 1)
            # ...then both are, and a bug no longer listed for the
            # signature goes
            later = NOW + datetime.timedelta(
                hours=config.get('bugs_refresh_hours', 2) + 1)
            with mock.patch.object(bugs, 'fetch_socorro', lambda s, f: {
                    x: ({456} if x == shared else set()) for x in s}):
                self.assertEqual(bugs.refresh(TODAY, later), 2)
            self.assertEqual([b.bug_id for b in
                              models.load_bugs([shared])[shared]], [456])
            checks = models.load_bug_checks([shared, lonely])
            self.assertEqual((checks[shared].checked_at, checks[shared].found),
                             (later, 1))
        finally:
            for p in patches:
                p.stop()

    def test_flags_carried_over_from_previous_days_count(self):
        """The page keeps a spike listed for flag_window_hours after the
        day it happened: those signatures are looked up too, not only the
        ones flagged in today's scores."""
        ids = models.series_ids('Firefox', 'nightly',
                                ['stepped down', 'yesterday', 'old', 'calm'])
        day = datetime.timedelta(days=1)

        def score(sgn, when, **kw):
            row = {'series_id': ids[sgn], 'day': when, 'as_of': NOW,
                   'partial': False, 'observed': 5, 'expected': 0.0,
                   'severity': 'ok', 'is_new': False, 'storm': False}
            row.update(kw)
            models.upsert(models.Score, [row], ['series_id', 'day'])
        # flagged two days ago, stepped down to ok since (peak kept)
        score('stepped down', TODAY - 2 * day, severity='ok',
              peak_severity='spike')
        score('stepped down', TODAY)
        score('yesterday', TODAY - day, severity='watch')
        score('yesterday', TODAY)
        score('old', TODAY - 5 * day, severity='major')
        score('old', TODAY)
        score('calm', TODAY)
        db.session.commit()
        self.assertEqual(bugs.due_signatures(TODAY, NOW),
                         ['stepped down', 'yesterday'])

    def test_failures_leave_signatures_for_the_next_run(self):
        self.flag('release', ['a', 'b'])
        # Socorro fails for everything: nothing is recorded
        with mock.patch.object(bugs, 'fetch_socorro', lambda s, f: {}), \
                mock.patch.object(bugs, 'search_bugzilla',
                                  lambda s, f: self.fail('not reached')):
            self.assertEqual(bugs.refresh(TODAY, NOW), 0)
        self.assertEqual(models.load_bug_checks(['a', 'b']), {})
        # Socorro has ids but their details cannot be fetched: the
        # signature waits, the one without bugs is searched and recorded
        with mock.patch.object(bugs, 'fetch_socorro',
                               lambda s, f: {'a': {1}, 'b': set()}), \
                mock.patch.object(bugs, 'fetch_details', lambda i, f: None), \
                mock.patch.object(bugs, 'search_bugzilla',
                                  lambda s, f: {x: {} for x in s}):
            self.assertEqual(bugs.refresh(TODAY, NOW), 1)
        self.assertEqual(sorted(models.load_bug_checks(['a', 'b'])), ['b'])
        self.assertEqual(models.load_bugs(['a', 'b']), {})
        # a passed deadline stops before any query
        counter = bugs.Counter()
        counter.deadline = 0
        self.assertIsNone(bugs.refresh(TODAY, NOW, counter))


if __name__ == '__main__':
    unittest.main()
