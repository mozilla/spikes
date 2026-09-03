# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Platform events shown as badges on the charts.

Crash volumes move when the platform under Firefox moves: a Windows
cumulative update, a new NVIDIA driver, a macOS point release, a Mesa or
kernel release, an Android security bulletin.  This module fetches those
from public feeds *in the scheduler* (never in a page request), stores them
in ``dashboard_events`` and serves them grouped per day and source
(``/dashboard/api/events``, one small cached payload for the whole page).

Feeds, all JSON:

* **Windows updates**: DataForNerds' machine-readable copy of Microsoft's
  update-history pages (KB, OS build, release type; 45 KB gzipped).
  Microsoft itself only offers the monthly MSRC document.
* **NVIDIA GeForce drivers**: the download page's own lookup endpoint
  (unofficial, unchanged for years).  Each driver gets a crash-stats link
  on the ``adapter_driver_version`` string it shows up as (``616.56`` is
  ``*.6.1656``).
* **macOS**: the MacAdmins SOFA feed (security releases, dates, CVEs).
* **Linux**: kernel series, Ubuntu and Fedora releases from endoflife.date,
  Mesa releases from the freedesktop GitLab tags.
* **Android**: major versions from endoflife.date; the monthly security
  bulletin has no feed and is computed from Google's schedule (published
  on the first Monday of the month).

Every feed is fetched in parallel with a 15 s timeout; a failure keeps the
previous rows, is retried after ``events_retry_hours`` and reported in the
run.  Successful feeds are refreshed every ``events_refresh_hours``.
"""

import concurrent.futures
import datetime
import re
import urllib.parse

import requests

from spikes import db
from spikes.logger import logger
from . import config, models


TIMEOUT = 15
USER_AGENT = 'crash-spikes dashboard (+https://github.com/mozilla/spikes)'
CRASH_STATS = 'https://crash-stats.mozilla.org/search/'
NVIDIA_URL = ('https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/'
              'services/AjaxDriverService.php?func=DriverManualLookup'
              '&psid=127&pfid=995&osID=135&languageCode=1033&beta=0'
              '&isWHQL=1&dltype=-1&dch=1&upCRD=0&qnf=0&sort1=0'
              '&numberOfResults=60')

# source -> platform whose products the badge is shown for, and label
PLATFORM = {'windows': 'windows', 'nvidia': 'windows', 'apple': 'mac',
            'linux': 'linux', 'android': 'android'}
LABEL = {'windows': 'Windows update', 'nvidia': 'NVIDIA driver',
         'apple': 'macOS release', 'linux': 'Linux release',
         'android': 'Android'}
WINDOWS_TYPES = {'Standard': 'monthly security update (Patch Tuesday)',
                 'Preview': 'optional non-security preview',
                 'Out-of-band': 'out-of-band update',
                 'Hotpatch': 'hotpatch',
                 'Hotpatch-OOB': 'out-of-band hotpatch'}


def event(source, kind, ref, day, title, detail=None, url=None, search=None,
          at=None):
    return {'source': source, 'kind': kind, 'ref': str(ref)[:64], 'day': day,
            'at': at, 'title': title[:160],
            'detail': detail[:400] if detail else None,
            'url': url[:400] if url else None,
            'search': search[:400] if search else None}


def _day(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Parsers (pure functions of a feed payload)
# --------------------------------------------------------------------------

def parse_windows(payload):
    rows = payload.get('data', []) if isinstance(payload, dict) else payload
    out = []
    for r in rows:
        if r.get('OSType', 'Client') != 'Client':
            continue
        day = _day(r.get('ReleaseDate'))
        kb, build = r.get('KBNumber'), r.get('OSBuild')
        if day is None or not kb:
            continue
        rtype = r.get('ReleaseType') or 'Standard'
        title = '{} · Windows {} {}, build {}'.format(
            kb, r.get('MajorVersion') or '', r.get('WindowsVersion') or '',
            build or '?').replace('  ', ' ')
        out.append(event('windows', 'windows-update',
                         '{}/{}'.format(kb, build or ''), day, title,
                         detail=WINDOWS_TYPES.get(rtype, rtype),
                         url=r.get('ArticleUrl')))
    return out


def nvidia_suffix(version):
    """``616.56`` -> ``6.1656``: the end of the Windows driver string
    (``32.0.16.1656``) reported as ``adapter_driver_version``."""
    digits = re.sub(r'\D', '', version or '')
    if len(digits) != 5:
        return None
    return digits[0] + '.' + digits[1:]


def parse_nvidia(payload):
    out = []
    for item in payload.get('IDS', []):
        info = item.get('downloadInfo') or {}
        version = info.get('Version')
        try:
            day = datetime.datetime.strptime(info.get('ReleaseDateTime', ''),
                                             '%a %b %d, %Y').date()
        except ValueError:
            continue
        if not version:
            continue
        name = urllib.parse.unquote(info.get('Name') or 'GeForce driver')
        title = '{} {}{}'.format(name, version,
                                 ' (beta)' if info.get('IsBeta') == '1'
                                 else '')
        parts = ['WHQL'] if info.get('IsWHQL') == '1' else []
        suffix = nvidia_suffix(version)
        search = None
        if suffix:
            parts.append('adapter_driver_version *.{} in crash reports'
                         .format(suffix))
            search = CRASH_STATS + '?' + urllib.parse.urlencode(
                [('product', 'Firefox'),
                 ('adapter_driver_version', '$' + suffix),
                 ('date', '>=' + day.isoformat()),
                 ('_facets', 'signature'), ('_facets', 'version')])
        out.append(event('nvidia', 'nvidia-driver', version, day, title,
                         detail=', '.join(parts) or None,
                         url=info.get('DetailsURL'), search=search))
    return out


def parse_sofa(payload):
    out = []
    for os_ in payload.get('OSVersions', []):
        for r in os_.get('SecurityReleases', []):
            day = _day(r.get('ReleaseDate'))
            version = r.get('ProductVersion')
            if day is None or not version:
                continue
            detail = None
            n = r.get('UniqueCVEsCount')
            if n:
                detail = '{} CVEs fixed'.format(n)
                exploited = len(r.get('ActivelyExploitedCVEs') or [])
                if exploited:
                    detail += ', {} actively exploited'.format(exploited)
            out.append(event('apple', 'macos', version, day,
                             r.get('UpdateName') or 'macOS {}'.format(version),
                             detail=detail, url=r.get('SecurityInfo')))
    return out


def parse_endoflife(product, source, kind, fmt):
    """Parser for an endoflife.date product: one event per released cycle."""
    def parse(payload):
        out = []
        for r in payload:
            day = _day(r.get('releaseDate'))
            cycle = r.get('cycle')
            if day is None or not cycle:
                continue
            title = fmt.format(cycle=cycle, codename=r.get('codename') or '')
            title = title.replace(' ()', '').strip()
            lts = r.get('lts')
            out.append(event(source, kind, cycle, day, title,
                             detail='LTS' if lts else None,
                             url=r.get('link') or
                             'https://endoflife.date/{}'.format(product)))
        return out
    return parse


def parse_mesa(payload):
    out = []
    for t in payload:
        m = re.match(r'^mesa-(\d+\.\d+\.\d+)$', t.get('name', ''))
        if not m:  # release candidates
            continue
        version = m.group(1)
        day = _day((t.get('commit') or {}).get('created_at'))
        if day is None:
            continue
        out.append(event('linux', 'mesa', version, day,
                         'Mesa {}'.format(version),
                         url='https://docs.mesa3d.org/relnotes/{}.html'
                         .format(version)))
    return out


def android_bulletins(today, days):
    """Android Security Bulletins: published on the first Monday of the
    month (Pixel devices get the patch that day, other vendors later)."""
    start = today - datetime.timedelta(days=days)
    year, month = start.year, start.month
    out = []
    while (year, month) <= (today.year, today.month):
        first = datetime.date(year, month, 1)
        monday = first + datetime.timedelta(days=(7 - first.weekday()) % 7)
        if monday <= today:
            ref = '{:04d}-{:02d}'.format(year, month)
            out.append(event(
                'android', 'android-bulletin', ref, monday,
                'Android Security Bulletin {}'.format(ref),
                detail='monthly bulletin, published on the first Monday; '
                       'Pixel devices get it that day, other vendors later',
                url='https://source.android.com/docs/security/bulletin/'
                    '{}-01'.format(ref)))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------

class Source:
    def __init__(self, name, url, parse):
        self.name = name
        self.url = url
        self.parse = parse


SOURCES = [
    Source('windows-updates',
           'https://api.datafornerds.io/v2/microsoft/'
           'windows-update-history.json', parse_windows),
    Source('nvidia-geforce', NVIDIA_URL, parse_nvidia),
    Source('macos-sofa',
           'https://sofafeed.macadmins.io/v1/macos_data_feed.json',
           parse_sofa),
    Source('linux-kernel', 'https://endoflife.date/api/linux.json',
           parse_endoflife('linux', 'linux', 'kernel', 'Linux {cycle}')),
    Source('ubuntu', 'https://endoflife.date/api/ubuntu.json',
           parse_endoflife('ubuntu', 'linux', 'ubuntu',
                           'Ubuntu {cycle} ({codename})')),
    Source('fedora', 'https://endoflife.date/api/fedora.json',
           parse_endoflife('fedora', 'linux', 'fedora', 'Fedora {cycle}')),
    Source('mesa',
           'https://gitlab.freedesktop.org/api/v4/projects/176/repository/'
           'tags?per_page=100', parse_mesa),
    Source('android-versions', 'https://endoflife.date/api/android.json',
           parse_endoflife('android', 'android', 'android',
                           'Android {cycle} ({codename})')),
]
COMPUTED = 'android-bulletins'
FEED_NAMES = [s.name for s in SOURCES] + [COMPUTED]


def _get(url):
    r = requests.get(url, timeout=TIMEOUT,
                     headers={'User-Agent': USER_AGENT,
                              'Accept': 'application/json'})
    r.raise_for_status()
    return r.json()


def refresh(now, fetch=None, names=None):
    """Fetch the feeds (all, or *names*) in parallel and upsert their
    events.  Returns ``{feed: {'ok', 'items'}}``; nothing is committed."""
    fetch = fetch or _get
    today = now.date()
    retention = config.get('events_retention_days', 800)
    start = today - datetime.timedelta(days=retention)
    horizon = today + datetime.timedelta(days=7)
    sources = [s for s in SOURCES if names is None or s.name in names]
    results = {}
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(lambda s: s.parse(fetch(s.url)), s): s
                   for s in sources}
        for fut, source in futures.items():
            try:
                events = fut.result(timeout=TIMEOUT + 10)
                kept = [e for e in events if start <= e['day'] <= horizon]
                results[source.name] = (True, len(kept), None)
                rows.extend(kept)
            except Exception as ex:  # a dead feed must not stop the others
                logger.warning('Dashboard: event feed %s failed: %s',
                               source.name, ex)
                results[source.name] = (False, 0, str(ex)[:200])
    if names is None or COMPUTED in names:
        computed = android_bulletins(today, retention)
        rows.extend(computed)
        results[COMPUTED] = (True, len(computed), None)
    unique = {}
    for r in rows:
        r['updated_at'] = now
        unique[(r['kind'], r['ref'])] = r
    models.upsert(models.Event, list(unique.values()), ['kind', 'ref'])
    models.upsert(models.Feed, [
        {'name': name, 'fetched_at': now, 'ok': ok, 'items': items,
         'message': message}
        for name, (ok, items, message) in results.items()], ['name'])
    return {name: {'ok': ok, 'items': items}
            for name, (ok, items, _) in results.items()}


def due_feeds(now):
    """Feeds never fetched, fetched more than ``events_refresh_hours`` ago,
    or failed more than ``events_retry_hours`` ago."""
    feeds = models.load_feeds()
    refresh_before = now - datetime.timedelta(
        hours=config.get('events_refresh_hours', 6))
    retry_before = now - datetime.timedelta(
        hours=config.get('events_retry_hours', 1))
    due = []
    for name in FEED_NAMES:
        f = feeds.get(name)
        if f is None or f.fetched_at < (refresh_before if f.ok
                                        else retry_before):
            due.append(name)
    return due


def maybe_refresh(now):
    """Refresh the due feeds (called by every scheduler run; most runs have
    nothing to do).  Returns the refresh summary, or None."""
    due = due_feeds(now)
    if not due:
        return None
    res = refresh(now, names=due)
    db.session.commit()
    logger.info('Dashboard: events refreshed: %s', res)
    return res


def prune(today):
    models.prune_events(today - datetime.timedelta(
        days=config.get('events_retention_days', 800)))


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

def _ts(dt):
    return dt.replace(microsecond=0).isoformat() + 'Z' if dt else None


def grouped(since, until=None):
    """Events in ``[since, until]`` grouped per (day, source), oldest first:
    one badge per group, its items in the tooltip."""
    groups = {}
    for e in models.load_events(since, until):
        key = (e.day, e.source)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                'day': e.day.isoformat(), 'source': e.source,
                'platform': PLATFORM.get(e.source, e.source),
                'label': LABEL.get(e.source, e.source), 'at': None,
                'items': []}
        g['items'].append({'kind': e.kind, 'title': e.title,
                           'detail': e.detail, 'url': e.url,
                           'search': e.search, 'at': _ts(e.at)})
        if e.at is not None and (g['at'] is None or _ts(e.at) < g['at']):
            g['at'] = _ts(e.at)
    return [groups[k] for k in sorted(groups)]


def feed_status():
    return {f.name: {'fetched_at': _ts(f.fetched_at), 'ok': bool(f.ok),
                     'items': f.items, 'message': f.message}
            for f in models.load_feeds().values()}
