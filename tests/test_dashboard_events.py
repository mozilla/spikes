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



def driver_payload(days, series, total=3000):
    """SuperSearch histogram: *series* is ``{version: [count per day]}``."""
    buckets = []
    for i, d in enumerate(days):
        facets = [{'term': v, 'count': c[i]} for v, c in series.items()
                  if c[i]]
        buckets.append({'term': d.isoformat() + 'T00:00:00+00:00',
                        'count': total,
                        'facets': {'adapter_driver_version': facets}})
    return {'facets': {'histogram_date': buckets}}


# ten complete days, then today (partial)
DRIVER_DAYS = [TODAY - datetime.timedelta(days=10 - i) for i in range(11)]
DRIVERS_NVIDIA = driver_payload(DRIVER_DAYS, {
    # established
    '32.0.15.6094': [150] * 11,
    # new on day 7 (3 %), holds the next day
    '32.0.16.1656': [0, 0, 0, 0, 0, 0, 0, 90, 200, 260, 300],
    # release day under 1 % (0.8 %), crosses on day 7: dated day 6
    '32.0.16.1700': [0, 0, 0, 0, 0, 0, 25, 100, 150, 200, 220],
    # takes off after only 2 quiet days: established before the window
    '32.0.16.1088': [0, 0, 60, 80, 90, 90, 90, 90, 90, 90, 90],
    # hovers at 0.3 %, one day at 1.3 %: not new
    '31.0.15.4601': [10, 12, 9, 11, 10, 10, 11, 40, 12, 10, 9],
    # one-day blip (a machine crashing in a loop): not new
    '31.0.15.5000': [0, 0, 0, 0, 0, 0, 0, 40, 0, 0, 0],
    # crosses on the last complete day: not confirmed yet
    '32.0.16.1900': [0, 0, 0, 0, 0, 0, 0, 0, 0, 60, 300],
    # today only (partial day): ignored
    '32.0.16.2000': [0] * 10 + [400],
})
DRIVERS_AMD = driver_payload(DRIVER_DAYS, {
    # a stray 0.1 % day keeps the history quiet; new on day 8
    '32.0.21045.5002': [0, 0, 0, 0, 0, 3, 0, 0, 45, 120, 200],
})
DRIVERS_NONE = {'facets': {'histogram_date': []}}

PAYLOADS = {'windows-updates': WINDOWS, 'nvidia-geforce': NVIDIA,
            'drivers-nvidia': DRIVERS_NVIDIA, 'drivers-amd': DRIVERS_AMD,
            'drivers-intel': DRIVERS_NONE,
            'macos-sofa': SOFA, 'linux-kernel': EOL_LINUX,
            'ubuntu': EOL_UBUNTU, 'fedora': EOL_FEDORA, 'mesa': MESA,
            'android-versions': EOL_ANDROID}
BY_URL = {(s.url(TODAY) if callable(s.url) else s.url): s.name
          for s in events.SOURCES}


def fake_fetch(failing=(), override=None):
    def fetch(url, timeout=None, fmt='json', headers=None):
        name = BY_URL[url]
        if name in failing:
            raise RuntimeError('boom')
        if override and name in override:
            return override[name]
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


    def test_nvidia_name(self):
        self.assertEqual(events.nvidia_name('32.0.16.1656'), '616.56')
        self.assertEqual(events.nvidia_name('27.21.14.5671'), '456.71')
        self.assertIsNone(events.nvidia_name('32.0.21045.5002'))  # AMD shape
        self.assertIsNone(events.nvidia_name('9.17.10.4459'))

    def test_drivers_seen(self):
        evs = events.parse_drivers('nvidia')(DRIVERS_NVIDIA, TODAY)
        self.assertEqual([e['ref'] for e in evs],
                         ['nvidia/32.0.16.1656', 'nvidia/32.0.16.1700'])
        e = evs[0]
        self.assertEqual(e['day'], DRIVER_DAYS[7])
        self.assertEqual(e['title'], 'NVIDIA driver 616.56 (32.0.16.1656) '
                                     'appears in crash reports')
        self.assertTrue(e['detail'].startswith(
            'first seen this day, 3.0 % of NVIDIA crashes'))
        self.assertIn('the same day', e['detail'])
        self.assertIn('adapter_vendor_id=0x10de', e['search'])
        self.assertIn('adapter_driver_version=32.0.16.1656', e['search'])
        self.assertIsNone(e['url'])
        # the ramp: dated the release day, adoption confirmed a day later
        self.assertEqual(evs[1]['day'], DRIVER_DAYS[6])
        self.assertIn('1 day later', evs[1]['detail'])
        # AMD: a stray 0.1 % day is under the sighting threshold
        evs = events.parse_drivers('amd')(DRIVERS_AMD, TODAY)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]['day'], DRIVER_DAYS[8])
        self.assertEqual(evs[0]['title'],
                         'AMD driver 32.0.21045.5002 appears in crash reports')
        self.assertEqual(events.parse_drivers('intel')(DRIVERS_NONE, TODAY),
                         [])
        url = events.driver_url('intel')(TODAY)
        self.assertIn('adapter_vendor_id=0x8086', url)
        self.assertIn('_histogram.date=adapter_driver_version', url)


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
        self.assertEqual(res['drivers-nvidia']['items'], 2)
        self.assertEqual(res['drivers-amd']['items'], 1)
        self.assertEqual(res['drivers-intel']['items'], 0)
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

    def test_driver_sightings_keep_their_first_day(self):
        events.refresh(NOW, fetch=fake_fetch())
        db.session.commit()
        # a later window sees the same version take off a day later (the
        # earlier data has aged out): the stored sighting does not move
        shifted = driver_payload(DRIVER_DAYS, {
            '32.0.16.1656': [0, 0, 0, 0, 0, 0, 0, 0, 200, 260, 300]})
        events.refresh(NOW + datetime.timedelta(hours=6),
                       fetch=fake_fetch(override={'drivers-nvidia': shifted}))
        db.session.commit()
        rows = [e for e in models.load_events(TODAY - datetime.timedelta(
            days=10)) if e.kind == 'driver-seen' and e.source == 'nvidia']
        self.assertEqual([r.day for r in rows],
                         [DRIVER_DAYS[6], DRIVER_DAYS[7]])
        # untouched by the second refresh (feed events are updated in place)
        self.assertEqual({r.updated_at for r in rows}, {NOW})

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
        # the release from the GeForce feed, then its appearance in the
        # crash reports a few days later: two badges
        nvidia = [g for g in d['events'] if g['source'] == 'nvidia']
        self.assertEqual([(g['day'], g['items'][0]['kind']) for g in nvidia],
                         [('2026-08-26', 'nvidia-driver'),
                          (DRIVER_DAYS[6].isoformat(), 'driver-seen'),
                          (DRIVER_DAYS[7].isoformat(), 'driver-seen')])
        self.assertEqual(nvidia[0]['items'][0]['title'],
                         'GeForce Game Ready Driver 616.56')
        self.assertIn('search', nvidia[0]['items'][0])
        self.assertIn('adapter_driver_version=32.0.16.1656',
                      nvidia[2]['items'][0]['search'])
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
