# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Platform events: feed parsers, refresh bookkeeping and the endpoint.

The feeds are replaced by small inline payloads; nothing touches the
network.
"""

import datetime
import os
import unittest
from unittest import mock

from spikes import app, db
from spikes.dashboard import api, events, models


NOW = datetime.datetime(2026, 9, 3, 8, 0, 0)
TODAY = NOW.date()

WINDOWS = {'metadata': {}, 'data': [
    {'ArticleUrl': 'https://support.microsoft.com/help/5120998',
     'KBNumber': 'KB5120998', 'MajorVersion': '11', 'OSBuild': '26200.9278',
     'OSType': 'Client', 'ReleaseDate': '2026-08-27',
     'ReleaseType': 'Preview', 'WindowsVersion': '25H2'},
    {'KBNumber': 'KB5120998', 'MajorVersion': '11', 'OSBuild': '26100.9278',
     'OSType': 'Client', 'ReleaseDate': '2026-08-27',
     'ReleaseType': 'Preview', 'WindowsVersion': '24H2'},
    {'KBNumber': 'KB5120249', 'MajorVersion': '10', 'OSBuild': '19045.7663',
     'OSType': 'Client', 'ReleaseDate': '2026-08-11',
     'ReleaseType': 'Standard', 'WindowsVersion': '22H2'},
    # servers are not Firefox's platform
    {'KBNumber': 'KB5120238', 'OSBuild': '17763.9121', 'OSType': 'Server',
     'ReleaseDate': '2026-08-11', 'ReleaseType': 'Standard'},
    # older than the retention: parsed, dropped by refresh
    {'KBNumber': 'KB4000000', 'OSBuild': '1.1', 'OSType': 'Client',
     'ReleaseDate': '2020-01-14', 'ReleaseType': 'Standard'},
]}
NVIDIA_URL = 'https://www.nvidia.com/en-us/drivers/details/278153/'
NVIDIA = {'IDS': [
    {'downloadInfo': {'Version': '616.56',
                      'ReleaseDateTime': 'Wed Aug 26, 2026',
                      'Name': 'GeForce%20Game%20Ready%20Driver', 'IsWHQL': '1',
                      'IsBeta': '0', 'DetailsURL': NVIDIA_URL}},
    {'downloadInfo': {'Version': '610.88',
                      'ReleaseDateTime': 'Tue Jul 28, 2026',
                      'Name': 'GeForce%20Game%20Ready%20Driver', 'IsWHQL': '1',
                      'IsBeta': '0'}},
    {'downloadInfo': {'Version': 'x', 'ReleaseDateTime': 'garbage'}},
]}
SOFA = {'OSVersions': [
    {'OSVersion': 'Tahoe 26', 'SecurityReleases': [
        {'UpdateName': 'macOS Tahoe 26.6.2', 'ProductVersion': '26.6.2',
         'ReleaseDate': '2026-08-17T00:00:00Z',
         'SecurityInfo': 'https://support.apple.com/en-us/148281',
         'UniqueCVEsCount': 28, 'ActivelyExploitedCVEs': ['CVE-2026-1']}]},
    {'OSVersion': 'Sequoia 15', 'SecurityReleases': [
        {'UpdateName': 'macOS Sequoia 15.7.3', 'ProductVersion': '15.7.3',
         'ReleaseDate': '2026-08-17T00:00:00Z'}]},
]}
EOL_LINUX = [{'cycle': '7.2', 'releaseDate': '2026-08-16', 'lts': False},
             {'cycle': '6.12', 'releaseDate': '2024-11-17', 'lts': True}]
EOL_UBUNTU = [{'cycle': '26.04', 'codename': 'Resolute Raccoon',
               'releaseDate': '2026-04-23', 'lts': True,
               'link': 'https://wiki.ubuntu.com/ResoluteRaccoon/ReleaseNotes'}]
EOL_FEDORA = [{'cycle': '44', 'releaseDate': '2026-04-28'}]
MESA = [{'name': 'mesa-26.2.2',
         'commit': {'created_at': '2026-09-02T10:00:00Z'}},
        {'name': 'mesa-26.3.0-rc1',
         'commit': {'created_at': '2026-09-01T10:00:00Z'}}]
EOL_ANDROID = [{'cycle': '17', 'codename': 'Cinnamon Bun',
                'releaseDate': '2026-06-16'},
               {'cycle': '16', 'codename': 'Baklava',
                'releaseDate': '2025-06-10'}]

PAYLOADS = {'windows-updates': WINDOWS, 'nvidia-geforce': NVIDIA,
            'macos-sofa': SOFA, 'linux-kernel': EOL_LINUX,
            'ubuntu': EOL_UBUNTU, 'fedora': EOL_FEDORA, 'mesa': MESA,
            'android-versions': EOL_ANDROID}
BY_URL = {s.url: s.name for s in events.SOURCES}


def fake_fetch(failing=()):
    def fetch(url, timeout=None):
        name = BY_URL[url]
        if name in failing:
            raise RuntimeError('boom')
        return PAYLOADS[name]
    return fetch


class ParserTest(unittest.TestCase):

    def test_windows(self):
        evs = events.parse_windows(WINDOWS)
        self.assertEqual(len(evs), 4)  # the server row is skipped
        e = evs[0]
        self.assertEqual(e['ref'], 'KB5120998/26200.9278')
        self.assertEqual(e['day'], datetime.date(2026, 8, 27))
        self.assertEqual(e['title'],
                         'KB5120998 · Windows 11 25H2, build 26200.9278')
        self.assertEqual(e['detail'], 'optional non-security preview')
        self.assertEqual(evs[2]['detail'],
                         'monthly security update (Patch Tuesday)')
        # built from the KB number: the feed's ArticleUrl is broken
        self.assertEqual(e['url'], 'https://support.microsoft.com/help/5120998')
        self.assertEqual(evs[2]['url'],
                         'https://support.microsoft.com/help/5120249')

    def test_nvidia(self):
        self.assertEqual(events.nvidia_suffix('616.56'), '6.1656')
        self.assertEqual(events.nvidia_suffix('561.09'), '5.6109')
        self.assertIsNone(events.nvidia_suffix('1000.10'))
        evs = events.parse_nvidia(NVIDIA)
        self.assertEqual([e['ref'] for e in evs], ['616.56', '610.88'])
        e = evs[0]
        self.assertEqual(e['day'], datetime.date(2026, 8, 26))
        self.assertEqual(e['title'], 'GeForce Game Ready Driver 616.56')
        self.assertIn('WHQL', e['detail'])
        self.assertIn('*.6.1656', e['detail'])
        self.assertIn('adapter_driver_version=%246.1656', e['search'])
        self.assertIn('date=%3E%3D2026-08-26', e['search'])

    def test_sofa(self):
        evs = events.parse_sofa(SOFA)
        self.assertEqual([e['ref'] for e in evs], ['26.6.2', '15.7.3'])
        self.assertEqual(evs[0]['title'], 'macOS Tahoe 26.6.2')
        self.assertEqual(evs[0]['detail'],
                         '28 CVEs fixed, 1 actively exploited')
        self.assertIsNone(evs[1]['detail'])

    def test_endoflife_and_mesa(self):
        parse = events.parse_endoflife('ubuntu', 'linux', 'ubuntu',
                                       'Ubuntu {cycle} ({codename})')
        evs = parse(EOL_UBUNTU)
        self.assertEqual(evs[0]['title'], 'Ubuntu 26.04 (Resolute Raccoon)')
        self.assertEqual(evs[0]['detail'], 'LTS')
        self.assertEqual(evs[0]['url'],
                         'https://wiki.ubuntu.com/ResoluteRaccoon/ReleaseNotes')
        parse = events.parse_endoflife('fedora', 'linux', 'fedora',
                                       'Fedora {cycle}')
        self.assertEqual(parse(EOL_FEDORA)[0]['title'], 'Fedora 44')
        evs = events.parse_mesa(MESA)
        self.assertEqual([e['ref'] for e in evs], ['26.2.2'])  # no rc
        self.assertEqual(evs[0]['day'], datetime.date(2026, 9, 2))

    def test_android_bulletins(self):
        evs = events.android_bulletins(TODAY, 70)
        # first Mondays; September's (the 7th) is still ahead on the 3rd
        self.assertEqual([e['day'] for e in evs],
                         [datetime.date(2026, 6, 1), datetime.date(2026, 7, 6),
                          datetime.date(2026, 8, 3)])
        self.assertEqual(evs[-1]['ref'], '2026-08')
        self.assertTrue(evs[-1]['url'].endswith('/2026-08-01'))


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

    def tearDown(self):
        db.session.rollback()
        models.drop_all()
        self.ctx.pop()


class RefreshTest(DBTestCase):

    def test_refresh_and_grouping(self):
        res = events.refresh(NOW, fetch=fake_fetch())
        db.session.commit()
        self.assertTrue(all(v['ok'] for v in res.values()))
        self.assertEqual(res['windows-updates']['items'], 3)  # 2020 dropped
        self.assertEqual(res['nvidia-geforce']['items'], 2)
        self.assertEqual(res['linux-kernel']['items'], 2)
        self.assertGreater(res['android-bulletins']['items'], 20)
        count, latest = models.events_version()
        self.assertEqual(latest, NOW)
        # idempotent
        events.refresh(NOW + datetime.timedelta(hours=1), fetch=fake_fetch())
        db.session.commit()
        self.assertEqual(models.events_version()[0], count)
        groups = events.grouped(TODAY - datetime.timedelta(days=30))
        by_key = {(g['day'], g['source']): g for g in groups}
        win = by_key[('2026-08-27', 'windows')]
        self.assertEqual(len(win['items']), 2)
        self.assertEqual(win['platform'], 'windows')
        self.assertEqual(win['label'], 'Windows update')
        self.assertEqual(by_key[('2026-08-26', 'nvidia')]['platform'],
                         'windows')
        self.assertEqual(len(by_key[('2026-08-17', 'apple')]['items']), 2)
        self.assertEqual(by_key[('2026-09-02', 'linux')]['items'][0]['title'],
                         'Mesa 26.2.2')
        self.assertEqual([g['day'] for g in groups],
                         sorted(g['day'] for g in groups))
        # the computed Android bulletin of August (first Monday, the 3rd)
        wider = {(g['day'], g['source']): g
                 for g in events.grouped(TODAY - datetime.timedelta(days=40))}
        self.assertEqual(wider[('2026-08-03', 'android')]['items'][0]['kind'],
                         'android-bulletin')
        status = events.feed_status()
        self.assertEqual(status['macos-sofa']['items'], 2)
        self.assertTrue(status['mesa']['ok'])

    def test_failed_feed_keeps_rows_and_is_retried(self):
        events.refresh(NOW, fetch=fake_fetch())
        db.session.commit()
        later = NOW + datetime.timedelta(hours=7)
        self.assertEqual(sorted(events.due_feeds(later)),
                         sorted(events.FEED_NAMES))
        res = events.refresh(later, fetch=fake_fetch(failing=('macos-sofa',)))
        db.session.commit()
        self.assertFalse(res['macos-sofa']['ok'])
        self.assertTrue(res['nvidia-geforce']['ok'])
        # the previous macOS rows are still there
        groups = events.grouped(TODAY - datetime.timedelta(days=30))
        self.assertTrue(any(g['source'] == 'apple' for g in groups))
        status = events.feed_status()
        self.assertFalse(status['macos-sofa']['ok'])
        self.assertEqual(status['macos-sofa']['message'], 'boom')
        # only the failed feed is due again after the retry delay
        self.assertEqual(events.due_feeds(later + datetime.timedelta(
            minutes=30)), [])
        self.assertEqual(events.due_feeds(later + datetime.timedelta(hours=2)),
                         ['macos-sofa'])
        self.assertIsNone(events.maybe_refresh(later + datetime.timedelta(
            minutes=30)))

    def test_prune(self):
        events.refresh(NOW, fetch=fake_fetch())
        db.session.commit()
        events.prune(TODAY + datetime.timedelta(days=900))
        db.session.commit()
        self.assertEqual(models.events_version()[0], 0)


class ApiTest(DBTestCase):

    def setUp(self):
        super().setUp()
        events.refresh(NOW, fetch=fake_fetch())
        db.session.commit()
        self.client = app.test_client()
        self.patch = mock.patch.object(api, 'today_utc', return_value=TODAY)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_events_endpoint(self):
        r = self.client.get('/dashboard/api/events?days=30')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['since'], '2026-08-04')
        days = [g['day'] for g in d['events']]
        self.assertEqual(days, sorted(days))
        self.assertTrue(all(day >= '2026-08-04' for day in days))
        nvidia = [g for g in d['events'] if g['source'] == 'nvidia']
        self.assertEqual(len(nvidia), 1)
        self.assertEqual(nvidia[0]['items'][0]['title'],
                         'GeForce Game Ready Driver 616.56')
        self.assertIn('search', nvidia[0]['items'][0])
        self.assertIn('windows-updates', d['feeds'])
        etag = r.headers['ETag']
        r2 = self.client.get('/dashboard/api/events?days=30',
                             headers={'If-None-Match': etag})
        self.assertEqual(r2.status_code, 304)
        # another range is another tag
        r3 = self.client.get('/dashboard/api/events?days=90',
                             headers={'If-None-Match': etag})
        self.assertEqual(r3.status_code, 200)
        self.assertGreater(len(r3.get_json()['events']), len(d['events']))
        r4 = self.client.get('/dashboard/api/events?days=x')
        self.assertEqual(r4.status_code, 400)


if __name__ == '__main__':
    unittest.main()
