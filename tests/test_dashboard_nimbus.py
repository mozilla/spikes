# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Nimbus parsing, polling and serving with inline fixtures; no network."""

import datetime
import os
import unittest

from spikes import app, db
from spikes.dashboard import events, models, nimbus


NOW = datetime.datetime(2026, 10, 28, 9, 0, 0)
TODAY = NOW.date()
NIGHTLY = ('(browserSettings.update.channel in ["nightly"]) && '
           "((version|versionCompare('160.!') >= 0))")
DESKTOP = 'nimbus-desktop-experiments'
MOBILE = 'nimbus-mobile-experiments'


def ms(text):
    """Remote Settings timestamp (ms) of a naive UTC ISO time."""
    dt = datetime.datetime.fromisoformat(text).replace(
        tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def recipe(slug, app_name='firefox_desktop', channel='', targeting='',
           rollout=True, count=10000, paused=False,
           published='2026-10-27T17:16:04.802693Z', start='2026-10-27',
           end=None, value=True, modified=None):
    r = {'slug': slug, 'id': slug, 'appName': app_name, 'channel': channel,
         'targeting': targeting, 'isRollout': rollout,
         'bucketConfig': {'count': count, 'total': 10000, 'start': 0,
                          'namespace': slug + '-1'},
         'isEnrollmentPaused': paused, 'publishedDate': published,
         'startDate': start, 'endDate': end,
         'userFacingName': slug.replace('-', ' ').title(),
         'featureIds': ['some-feature'],
         'branches': [{'slug': 'control', 'ratio': 1, 'features': [
             {'featureId': 'some-feature', 'value': {'enabled': value}}]}]}
    if modified is not None:
        r['last_modified'] = ms(modified)
    return r


def monitor(**stamps):
    changes = [{'bucket': 'main', 'collection': c, 'last_modified': ts}
               for c, ts in stamps.items()]
    # Preview entries must be ignored.
    changes.append({'bucket': 'main-preview', 'collection': DESKTOP,
                    'last_modified': 1})
    return {'changes': changes}


class Fetch:
    """Serve fixture responses and record requested URLs."""

    def __init__(self, monitor, changesets):
        self.monitor = monitor
        self.changesets = changesets
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if url == nimbus.MONITOR:
            return self.monitor
        for collection, changes in self.changesets.items():
            if '/collections/{}/'.format(collection) in url:
                return {'changes': changes}
        raise AssertionError(url)


class RecipeTest(unittest.TestCase):

    def test_channels(self):
        self.assertEqual(nimbus.channels_of(
            recipe('a', targeting=NIGHTLY), 'Firefox'), ('nightly',))
        self.assertEqual(nimbus.channels_of(recipe('a', targeting=(
            'browserSettings.update.channel in ["release", "beta", '
            '"nightly", "aurora"]')), 'Firefox'),
            ('nightly', 'beta', 'release'))  # the dashboard's order
        self.assertEqual(nimbus.channels_of(recipe('a', targeting=(
            "browserSettings.update.channel == 'esr'")), 'Firefox'),
            ('esr',))
        # No configured channel matches aurora.
        self.assertEqual(nimbus.channels_of(recipe('a', targeting=(
            'browserSettings.update.channel in ["aurora"]')), 'Firefox'), ())
        # no channel targeting: every channel
        self.assertIsNone(nimbus.channels_of(
            recipe('a', targeting="'en-US' in locales"), 'Firefox'))
        self.assertEqual(nimbus.channels_of(
            recipe('a', app_name='fenix', channel='beta'), 'Fenix'),
            ('beta',))

    def test_formatting(self):
        self.assertEqual(nimbus.fmt_share(1.0), '100 %')
        self.assertEqual(nimbus.fmt_share(0.05), '5 %')
        self.assertEqual(nimbus.fmt_share(0.005), '0.5 %')
        self.assertEqual(nimbus.fmt_share(None), '? %')
        long_a = 'x' * 70 + '-a'
        long_b = 'x' * 70 + '-b'
        self.assertEqual(len(nimbus._ref(long_a)), 64)
        self.assertNotEqual(nimbus._ref(long_a), nimbus._ref(long_b))
        self.assertEqual(nimbus._ref('short'), 'short')

    def test_parse_experimenter(self):
        rows = nimbus.parse_experimenter([
            recipe('addresses-storage-migration-nightly-rollout',
                   targeting=NIGHTLY),
            # Fall back to startDate when publishedDate is absent.
            recipe('old-experiment', rollout=False, count=500,
                   published=None, start='2025-03-03', end='2025-04-01'),
            recipe('fenix-beta-rollout', app_name='fenix', channel='beta'),
            recipe('ios-rollout', app_name='firefox_ios'),
            recipe('devedition-only', targeting=(
                'browserSettings.update.channel == "aurora"')),
            {'appName': 'firefox_desktop'},  # no slug
        ])
        by = {(r['kind'], r['ref']): r for r in rows}
        self.assertEqual(sorted(by), [
            ('nimbus-end', 'old-experiment'),
            ('nimbus-launch', 'addresses-storage-migration-nightly-rollout'),
            ('nimbus-launch', 'fenix-beta-rollout'),
            ('nimbus-launch', 'old-experiment')])
        r = by[('nimbus-launch',
                'addresses-storage-migration-nightly-rollout')]
        self.assertEqual(r['at'], datetime.datetime(2026, 10, 27, 17, 16, 4))
        self.assertEqual(r['day'], datetime.date(2026, 10, 27))
        self.assertEqual((r['source'], r['product'], r['channels']),
                         ('nimbus', 'Firefox', 'nightly'))
        self.assertEqual(r['title'], 'Launched: Addresses Storage Migration '
                                     'Nightly Rollout')
        self.assertEqual(r['detail'], 'rollout · 100 % of eligible clients '
                         '(now) · nightly · addresses-storage-migration-'
                         'nightly-rollout')
        self.assertEqual(r['url'], 'https://experimenter.services.mozilla.com'
                         '/nimbus/addresses-storage-migration-nightly-rollout'
                         '/summary')
        old = by[('nimbus-launch', 'old-experiment')]
        self.assertIsNone(old['at'])
        self.assertEqual(old['day'], datetime.date(2025, 3, 3))
        self.assertIsNone(old['channels'])  # every channel
        self.assertIn('experiment · 5 % of eligible clients (at the end)',
                      old['detail'])
        ended = by[('nimbus-end', 'old-experiment')]
        self.assertEqual((ended['day'], ended['at']),
                         (datetime.date(2025, 4, 1), None))
        self.assertEqual(ended['title'], 'Ended: Old Experiment')
        fenix = by[('nimbus-launch', 'fenix-beta-rollout')]
        self.assertEqual((fenix['product'], fenix['channels']),
                         ('Fenix', 'beta'))


class DBTestCase(unittest.TestCase):

    def setUp(self):
        if os.environ.get('DATABASE_URL') and \
                not os.environ.get('DASHBOARD_TEST_ALLOW_DB'):
            self.skipTest('DATABASE_URL is set; refusing to drop its tables'
                          ' (set DASHBOARD_TEST_ALLOW_DB=1 to allow)')
        self.ctx = app.app_context()
        self.ctx.push()
        models.drop_all()
        models.create_all()
        nimbus._applied.clear()

    def tearDown(self):
        db.session.rollback()
        models.drop_all()
        self.ctx.pop()
        nimbus._applied.clear()

    def events(self):
        return {(e.kind, e.ref): e for e in models.load_events(
            TODAY - datetime.timedelta(days=30), product='Firefox')}


class PollTest(DBTestCase):

    def baseline(self):
        launched = recipe('new-rollout', targeting=NIGHTLY,
                          modified='2026-10-27T17:16:14')
        # Published earlier; the first poll cannot identify what changed.
        ramped = recipe('ramped-rollout', count=500,
                        published='2026-10-20T17:39:46Z',
                        modified='2026-10-27T17:19:17')
        ending = recipe('ending-experiment', rollout=False,
                        published='2026-10-21T10:00:00Z',
                        modified='2026-10-21T10:00:09')
        fenix = recipe('fenix-rollout', app_name='fenix', channel='release',
                       published='2026-10-25T08:00:00Z',
                       modified='2026-10-25T08:00:05')
        ios = recipe('ios-rollout', app_name='firefox_ios',
                     modified='2026-10-26T08:00:05')
        # Missing publication time must not move the launch date.
        undated = recipe('undated-rollout', published=None,
                         start='2026-10-08', modified='2026-10-22T21:22:39')
        same_day = recipe('same-day-rollout', published=None,
                          start='2026-10-26', modified='2026-10-26T08:00:00')
        fetch = Fetch(monitor(**{DESKTOP: ms('2026-10-27T17:19:17'),
                                 MOBILE: ms('2026-10-26T08:00:05')}),
                      {DESKTOP: [launched, ramped, ending, undated, same_day],
                       MOBILE: [fenix, ios]})
        res = nimbus.poll(NOW, fetch=fetch)
        return fetch, res

    def test_first_poll_records_the_live_recipes(self):
        fetch, res = self.baseline()
        self.assertEqual(res, {'collections': 2, 'events': 8})
        # No stored state: request full collections.
        self.assertNotIn('_since', fetch.urls[1] + fetch.urls[2])
        ev = self.events()
        self.assertEqual(sorted(ev), [
            ('nimbus-change', 'ramped-rollout@20261027171917'),
            ('nimbus-change', 'undated-rollout@20261022212239'),
            ('nimbus-launch', 'ending-experiment'),
            ('nimbus-launch', 'new-rollout'),
            ('nimbus-launch', 'ramped-rollout'),
            ('nimbus-launch', 'same-day-rollout'),
            ('nimbus-launch', 'undated-rollout')])
        # Keep the launch day separate from the later modification.
        undated = ev[('nimbus-launch', 'undated-rollout')]
        self.assertEqual((undated.day, undated.at),
                         (datetime.date(2026, 10, 8), None))
        self.assertEqual(ev[('nimbus-change', 'undated-rollout@'
                             '20261022212239')].at,
                         datetime.datetime(2026, 10, 22, 21, 22, 39))
        # Both feeds identify and date the same launch.
        [exp] = [r for r in nimbus.parse_experimenter([recipe(
            'undated-rollout', published=None, start='2026-10-08')])
            if r['kind'] == 'nimbus-launch']
        self.assertEqual((exp['ref'], exp['day'], exp['at']),
                         ('undated-rollout', undated.day, None))
        self.assertEqual(ev[('nimbus-launch', 'new-rollout')].at,
                         datetime.datetime(2026, 10, 27, 17, 16, 4))
        change = ev[('nimbus-change', 'ramped-rollout@20261027171917')]
        self.assertEqual(change.at,
                         datetime.datetime(2026, 10, 27, 17, 19, 17))
        self.assertIn('what changed is not known', change.detail)
        self.assertIn('rollout now at 5 %', change.detail)
        fenix = models.load_events(TODAY - datetime.timedelta(days=30),
                                   product='Fenix')
        self.assertEqual([(e.kind, e.channels) for e in fenix],
                         [('nimbus-launch', 'release')])
        # Track unsupported recipes for deletions, but hide their events.
        state = models.load_nimbus_recipes()
        self.assertEqual(len(state), 7)
        self.assertIsNone(state['ios-rollout'].product)
        self.assertEqual(state['ramped-rollout'].population, 0.05)
        feed = models.load_feeds()[nimbus.FEED]
        self.assertTrue(feed.ok)
        # An unchanged timestamp requires only the monitor request.
        self.assertIsNone(nimbus.poll(NOW, fetch=fetch))
        self.assertEqual(fetch.urls[-1], nimbus.MONITOR)
        # The stored recipe timestamp also avoids a refetch after restart.
        nimbus._applied.clear()
        n = len(fetch.urls)
        self.assertIsNone(nimbus.poll(NOW, fetch=fetch))
        self.assertEqual(len(fetch.urls), n + 1)

    def test_changes_are_diffed(self):
        self.baseline()
        later = NOW + datetime.timedelta(minutes=5)
        changes = [
            # 5 % -> 100 %, enrollment ended
            recipe('ramped-rollout', count=10000, paused=True,
                   published='2026-10-20T17:39:46Z',
                   modified='2026-10-28T08:10:00'),
            # Only last_modified changed.
            recipe('new-rollout', targeting=NIGHTLY,
                   modified='2026-10-28T08:20:00'),
            # A launch followed by a feature-value change.
            recipe('fresh-rollout', published='2026-10-28T08:30:00Z',
                   modified='2026-10-28T08:30:06'),
            recipe('fresh-rollout', published='2026-10-28T08:30:00Z',
                   value=False, modified='2026-10-28T08:40:00'),
            {'id': 'ending-experiment', 'deleted': True,
             'last_modified': ms('2026-10-28T08:50:00')},
            {'id': 'never-seen', 'deleted': True,
             'last_modified': ms('2026-10-28T08:51:00')},
        ]
        fetch = Fetch(monitor(**{DESKTOP: ms('2026-10-28T08:51:00'),
                                 MOBILE: ms('2026-10-26T08:00:05')}),
                      {DESKTOP: changes})
        res = nimbus.poll(later, fetch=fetch)
        self.assertEqual(res, {'collections': 1, 'events': 4})
        # Request changes since the newest stored recipe.
        self.assertEqual(fetch.urls[1], nimbus.changeset_url(
            DESKTOP, ms('2026-10-28T08:51:00'), ms('2026-10-27T17:19:17')))
        self.assertIn('&_since=%22', fetch.urls[1])
        ev = self.events()
        ramp = ev[('nimbus-change', 'ramped-rollout@20261028081000')]
        self.assertEqual(ramp.detail.split(' · ')[:3],
                         ['population 5 % → 100 %', 'enrollment ended',
                          'rollout'])
        self.assertNotIn(('nimbus-change', 'new-rollout@20261028082000'), ev)
        self.assertIn(('nimbus-launch', 'fresh-rollout'), ev)
        value = ev[('nimbus-change', 'fresh-rollout@20261028084000')]
        self.assertTrue(value.detail.startswith('feature values changed'))
        gone = ev[('nimbus-end', 'ending-experiment')]
        self.assertEqual(gone.at, datetime.datetime(2026, 10, 28, 8, 50))
        self.assertIn('experiment · 100 % of eligible clients', gone.detail)
        state = models.load_nimbus_recipes()
        self.assertNotIn('ending-experiment', state)
        self.assertEqual(state['ramped-rollout'].population, 1.0)
        self.assertTrue(state['ramped-rollout'].paused)
        self.assertEqual(state['fresh-rollout'].last_modified,
                         ms('2026-10-28T08:40:00'))
        # Experimenter's day-only end must not replace the poller's timestamp.
        events.refresh(later, fetch=lambda url, *a: [recipe(
            'ending-experiment', rollout=False,
            published='2026-10-21T10:00:00Z', end='2026-10-29')]
            if 'firefox-desktop' in url else [], names=['nimbus-desktop'])
        db.session.commit()
        gone = self.events()[('nimbus-end', 'ending-experiment')]
        self.assertEqual(gone.at, datetime.datetime(2026, 10, 28, 8, 50))

    def test_failure_is_recorded(self):
        def fetch(url, timeout=None):
            raise RuntimeError('down')
        self.assertEqual(nimbus.poll(NOW, fetch=fetch), {'ok': False})
        feed = models.load_feeds()[nimbus.FEED]
        self.assertFalse(feed.ok)
        self.assertEqual(feed.message, 'down')
        # Success clears the failure even without new records.
        later = NOW + datetime.timedelta(minutes=5)
        self.assertIsNone(nimbus.poll(later, fetch=Fetch(monitor(), {})))
        db.session.expire_all()
        feed = models.load_feeds()[nimbus.FEED]
        self.assertTrue(feed.ok)
        self.assertIsNone(feed.message)
        self.assertEqual(feed.fetched_at, later)


class MarksTest(DBTestCase):

    def test_marks_per_channel(self):
        def row(ref, day, at, product='Firefox', channels=None):
            return {'source': 'nimbus', 'kind': 'nimbus-launch', 'ref': ref,
                    'day': day, 'at': at, 'title': 'Launched: ' + ref,
                    'detail': 'rollout', 'url': nimbus.summary_url(ref),
                    'product': product, 'channels': channels,
                    'updated_at': NOW}
        y = TODAY - datetime.timedelta(days=1)
        models.upsert(models.Event, [
            row('nightly-only', y, datetime.datetime(2026, 10, 27, 17, 16),
                channels='nightly'),
            row('everywhere', y, datetime.datetime(2026, 10, 27, 9, 0)),
            row('day-only', y, None, channels='beta,nightly'),
            row('fenix', y, None, product='Fenix', channels='release'),
            row('too-old', TODAY - datetime.timedelta(days=100), None),
        ], ['kind', 'ref'])
        models.upsert(models.Event, [{
            'source': 'windows', 'kind': 'windows-update', 'ref': 'KB1',
            'day': y, 'title': 'KB1', 'updated_at': NOW}], ['kind', 'ref'])
        db.session.commit()
        since = TODAY - datetime.timedelta(days=90)
        nightly = nimbus.marks('Firefox', 'nightly@strict', since)
        self.assertEqual(len(nightly), 1)
        self.assertEqual(nightly[0]['day'], y.isoformat())
        self.assertEqual((nightly[0]['source'], nightly[0]['label']),
                         ('nimbus', 'Nimbus'))
        # Day-only items first, followed by timed items in order.
        self.assertEqual([it['title'] for it in nightly[0]['items']],
                         ['Launched: day-only', 'Launched: everywhere',
                          'Launched: nightly-only'])
        self.assertEqual(nightly[0]['items'][2]['at'], '2026-10-27T17:16:00Z')
        release = nimbus.marks('Firefox', 'release', since)
        self.assertEqual([it['title'] for g in release for it in g['items']],
                         ['Launched: everywhere'])
        self.assertEqual([it['title'] for g in nimbus.marks(
            'Fenix', 'release', since) for it in g['items']],
            ['Launched: fenix'])
        self.assertEqual(nimbus.marks('Thunderbird', 'release', since), [])
        # Platform payloads and their ETag inputs exclude Nimbus events.
        platform = events.grouped(since)
        self.assertEqual([g['source'] for g in platform], ['windows'])
        self.assertEqual(models.events_version()[0], 1)


if __name__ == '__main__':
    unittest.main()
