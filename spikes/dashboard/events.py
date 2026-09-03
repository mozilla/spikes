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
* **Graphics drivers seen in crash reports** (NVIDIA, AMD, Intel): no
  vendor publishes a usable feed for AMD or Intel, and the drivers users
  run also come from Windows Update and OEMs, so they are detected in
  Socorro instead: one SuperSearch query per vendor gives the daily counts
  of every ``adapter_driver_version``; a version becomes an event, dated
  the day it first shows up (0.2 % of its vendor's crashes, after at least
  five days without it), once it has reached 1 % within two weeks and held
  half of that the next day (an established version never qualifies, a
  one-day blip from a crash-looping machine neither).  Never moved
  afterwards.
* **Antivirus** (one badge for all vendors): Norton from the Norton
  Community announcements RSS (the monthly "Norton Security N for Windows"
  posts), Avast and Malwarebytes from their Chocolatey packages (published
  a day or two after the release), ESET from the winget-pkgs commit
  history, Microsoft Defender from the platform version shown on the
  Defender updates page (dated when first seen).  Kaspersky, McAfee and
  Bitdefender publish nothing usable and are absent.

Every feed is fetched in parallel with a 15 s timeout (60 s for the slow
GeForce lookup and the Socorro queries); a failure keeps the previous
rows, is retried after ``events_retry_hours`` and reported in the run.
Successful feeds are refreshed every ``events_refresh_hours``.
"""

import concurrent.futures
import datetime
import email.utils
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

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
              '&numberOfResults=40')
# the GeForce lookup takes tens of seconds for a few dozen results
NVIDIA_TIMEOUT = 60

# source -> platform whose products the badge is shown for, and label
PLATFORM = {'windows': 'windows', 'nvidia': 'windows', 'amd': 'windows',
            'intel': 'windows', 'antivirus': 'windows', 'apple': 'mac',
            'linux': 'linux', 'android': 'android'}
LABEL = {'windows': 'Windows update', 'nvidia': 'NVIDIA driver',
         'amd': 'AMD driver', 'intel': 'Intel driver',
         'antivirus': 'Antivirus', 'apple': 'macOS release',
         'linux': 'Linux release', 'android': 'Android'}
# dated by first sighting: a later refresh must not move them
IMMUTABLE_KINDS = {'driver-seen', 'defender-platform'}
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

def parse_windows(payload, today=None):
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
        # the feed's ArticleUrl is a mangled relative path; Microsoft
        # redirects /help/<number> to the KB article
        digits = re.sub(r'\D', '', kb)
        url = 'https://support.microsoft.com/help/{}'.format(digits) \
            if digits else None
        out.append(event('windows', 'windows-update',
                         '{}/{}'.format(kb, build or ''), day, title,
                         detail=WINDOWS_TYPES.get(rtype, rtype), url=url))
    return out


def nvidia_suffix(version):
    """``616.56`` -> ``6.1656``: the end of the Windows driver string
    (``32.0.16.1656``) reported as ``adapter_driver_version``."""
    digits = re.sub(r'\D', '', version or '')
    if len(digits) != 5:
        return None
    return digits[0] + '.' + digits[1:]


def parse_nvidia(payload, today=None):
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


def parse_sofa(payload, today=None):
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
    def parse(payload, today=None):
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


def parse_mesa(payload, today=None):
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
# Graphics drivers seen in crash reports
# --------------------------------------------------------------------------

SOCORRO = 'https://crash-stats.mozilla.org/api/SuperSearch/'
VENDORS = {'nvidia': ('0x10de', 'NVIDIA'), 'amd': ('0x1002', 'AMD'),
           'intel': ('0x8086', 'Intel')}
DRIVER_WINDOW_DAYS = 45
DRIVER_QUIET_SHARE = 0.002   # under this the version is not around yet
DRIVER_MIN_SHARE = 0.01      # adoption: reached within DRIVER_RAMP_DAYS...
DRIVER_MIN_COUNT = 20
DRIVER_RAMP_DAYS = 14
DRIVER_CONFIRM_SHARE = 0.005  # ... and held (half of it) the next day
DRIVER_MIN_QUIET_DAYS = 5
DRIVER_TIMEOUT = 60


def driver_url(vendor):
    """SuperSearch: daily counts of every driver version of *vendor* on
    Firefox release, Windows, over the last ``DRIVER_WINDOW_DAYS``."""
    vendor_id = VENDORS[vendor][0]

    def url(today):
        since = today - datetime.timedelta(days=DRIVER_WINDOW_DAYS)
        return SOCORRO + '?' + urllib.parse.urlencode([
            ('product', 'Firefox'), ('release_channel', 'release'),
            ('platform', 'Windows'), ('adapter_vendor_id', vendor_id),
            ('date', '>=' + since.isoformat()),
            ('_histogram.date', 'adapter_driver_version'),
            ('_histogram_interval.date', '1d'), ('_facets_size', '100'),
            ('_results_number', '0')])
    return url


def socorro_headers():
    token = os.environ.get('LIBMOZDATA_CFG_SOCORRO_TOKEN')
    return {'Auth-Token': token} if token else {}


def nvidia_name(version):
    """``32.0.16.1656`` -> ``616.56``, the GeForce name of a Windows driver
    string; None when the string is not shaped like one."""
    parts = version.split('.')
    if len(parts) != 4 or len(parts[2]) != 2 or len(parts[3]) != 4 or \
            not (parts[2] + parts[3]).isdigit():
        return None
    digits = parts[2][-1] + parts[3]
    if int(digits[:3]) < 100:  # GeForce releases are in the hundreds
        return None
    return digits[:3] + '.' + digits[3:]


def _driver_event(vendor, version, day, share, ramp_days):
    vendor_id, name = VENDORS[vendor]
    pretty = nvidia_name(version) if vendor == 'nvidia' else None
    label = '{} ({})'.format(pretty, version) if pretty else version
    search = CRASH_STATS + '?' + urllib.parse.urlencode([
        ('product', 'Firefox'), ('adapter_vendor_id', vendor_id),
        ('adapter_driver_version', version), ('date', '>=' + day.isoformat()),
        ('_facets', 'signature'), ('_facets', 'version')])
    when = 'the same day' if ramp_days == 0 else \
        '{} day{} later'.format(ramp_days, '' if ramp_days == 1 else 's')
    return event(vendor, 'driver-seen', '{}/{}'.format(vendor, version), day,
                 '{} driver {} appears in crash reports'.format(name, label),
                 detail='first seen this day, {:.1f} % of {} crashes on '
                        'Firefox release (Windows) {}; drivers reach users '
                        'through vendor installers, Windows Update and OEMs'
                        .format(share * 100, name, when),
                 search=search)


def parse_drivers(vendor):
    """Parser of the SuperSearch histogram: the versions that are new."""
    def parse(payload, today=None):
        today = today or models.utctoday()
        days = []
        for b in (payload.get('facets') or {}).get('histogram_date', []):
            day = _day(b.get('term'))
            total = b.get('count') or 0
            if day is None or day >= today or total <= 0:
                continue  # the current day is partial
            counts = {f['term']: f['count'] for f in
                      (b.get('facets') or {}).get('adapter_driver_version',
                                                  [])}
            days.append((day, total, counts))
        days.sort()
        versions = set()
        for _, _, counts in days:
            versions.update(counts)
        out = []
        for version in sorted(versions):
            series = [(day, counts.get(version, 0),
                       counts.get(version, 0) / float(total))
                      for day, total, counts in days]
            # first sighting: the version was not around before it
            first = next((i for i, (_, _, s) in enumerate(series)
                          if s >= DRIVER_QUIET_SHARE), None)
            if first is None or first < DRIVER_MIN_QUIET_DAYS:
                continue  # never shows up, or established before the window
            # adoption: reaches the share within the ramp, and holds it the
            # next day (a one-day blip is one machine crashing in a loop)
            for i in range(first, min(first + DRIVER_RAMP_DAYS + 1,
                                      len(series) - 1)):
                _, n, share = series[i]
                if share >= DRIVER_MIN_SHARE and n >= DRIVER_MIN_COUNT:
                    if series[i + 1][2] >= DRIVER_CONFIRM_SHARE:
                        out.append(_driver_event(vendor, version,
                                                 series[first][0], share,
                                                 i - first))
                    break
        return out
    return parse


# --------------------------------------------------------------------------
# Antivirus releases
# --------------------------------------------------------------------------

DEFENDER_URL = 'https://www.microsoft.com/en-us/wdsi/defenderupdates'
NORTON_RSS = 'https://community.norton.com/c/announcements/1713.rss'
ATOM_NS = {'a': 'http://www.w3.org/2005/Atom',
           'd': 'http://schemas.microsoft.com/ado/2007/08/dataservices',
           'm': 'http://schemas.microsoft.com/ado/2007/08/dataservices/'
                'metadata'}


def _xml(text):
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def parse_norton(text, today=None):
    """Norton Community "Announcements" RSS: the monthly "Norton Security
    26.8 for Windows is now available!" posts (Norton 360 is the same
    product line; Family, VPN and Utilities posts are skipped)."""
    out = []
    root = _xml(text)
    if root is None:
        return out
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        if not re.match(r'Norton (Security|360|AntiVirus)\b.*\bWindows',
                        title, re.I):
            continue
        try:
            when = email.utils.parsedate_to_datetime(
                item.findtext('pubDate') or '')
        except (TypeError, ValueError):
            when = None
        if when is None:
            continue
        clean = re.sub(r'\s*(is now available!?|-?\s*Release!?)\s*$', '',
                       title, flags=re.I).strip(' -')
        out.append(event('antivirus', 'norton', clean, when.date(), clean,
                         detail='Norton Community announcement',
                         url=(item.findtext('link') or '').strip() or None))
    return out


def chocolatey_url(package):
    return ('https://community.chocolatey.org/api/v2/Packages()'
            '?$filter=Id%20eq%20%27{}%27&$orderby=Published%20desc&$top=12'
            .format(package))


def parse_chocolatey(kind, package, name):
    """Chocolatey package feed (Atom): one event per version, dated its
    publication, a day or two after the vendor's release."""
    def parse(text, today=None):
        out = []
        root = _xml(text)
        if root is None:
            return out
        for entry in root.findall('a:entry', ATOM_NS):
            props = entry.find('m:properties', ATOM_NS)
            if props is None:
                continue
            version = (props.findtext('d:Version', '', ATOM_NS) or '').strip()
            day = _day(props.findtext('d:Published', '', ATOM_NS))
            pre = (props.findtext('d:IsPrerelease', 'false', ATOM_NS) or
                   '').strip().lower() == 'true'
            if not version or day is None or pre:
                continue
            out.append(event('antivirus', kind, version, day,
                             '{} {}'.format(name, version),
                             detail='as published on Chocolatey, usually a '
                                    'day or two after the release',
                             url='https://community.chocolatey.org/packages/'
                                 '{}/{}'.format(package, version)))
        return out
    return parse


def winget_url(path):
    return ('https://api.github.com/repos/microsoft/winget-pkgs/commits'
            '?path=manifests/{}&per_page=30'.format(path))


def parse_winget(kind, package, name):
    """winget-pkgs commit history of a package: the "New version: ESET.Nod32
    version 19.2.10.0" commits, dated when merged (within days of the
    release)."""
    pattern = re.compile(r'(?:New|Add) version: {} version ([0-9][0-9.]*)'
                         .format(re.escape(package)), re.I)
    def parse(payload, today=None):
        out = []
        for c in payload if isinstance(payload, list) else []:
            commit = c.get('commit') or {}
            m = pattern.search((commit.get('message') or '').split('\n')[0])
            day = _day((commit.get('committer') or {}).get('date'))
            if not m or day is None:
                continue
            version = m.group(1).rstrip('.')
            out.append(event('antivirus', kind, version, day,
                             '{} {}'.format(name, version),
                             detail='as added to winget, usually within days '
                                    'of the release',
                             url=c.get('html_url')))
        return out
    return parse


def parse_defender(text, today=None):
    """Defender updates page: the current platform and engine versions; the
    event is dated the day a new platform version is first seen."""
    today = today or models.utctoday()
    plain = re.sub(r'<[^>]+>', ' ', text)
    platform = re.search(r'Platform Version:\s*([\d.]+)', plain)
    if not platform:
        return []
    engine = re.search(r'Engine Version:\s*([\d.]+)', plain)
    title = 'Microsoft Defender platform {}'.format(platform.group(1))
    if engine:
        title += ', engine {}'.format(engine.group(1))
    return [event('antivirus', 'defender-platform', platform.group(1), today,
                  title,
                  detail='monthly Defender Antivirus platform update, dated '
                         'when first seen on the Defender updates page',
                  url=DEFENDER_URL)]


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------

class Source:
    """A feed: *url* (a string, or a callable of today's date), *parse*
    ``(payload, today) -> events``, the payload being JSON (``fmt='json'``)
    or text; *headers* is a dict or a callable returning one."""

    def __init__(self, name, url, parse, timeout=TIMEOUT, fmt='json',
                 headers=None):
        self.name = name
        self.url = url
        self.parse = parse
        self.timeout = timeout
        self.fmt = fmt
        self.headers = headers


SOURCES = [
    Source('windows-updates',
           'https://api.datafornerds.io/v2/microsoft/'
           'windows-update-history.json', parse_windows),
    Source('nvidia-geforce', NVIDIA_URL, parse_nvidia, timeout=NVIDIA_TIMEOUT),
    Source('drivers-nvidia', driver_url('nvidia'), parse_drivers('nvidia'),
           timeout=DRIVER_TIMEOUT, headers=socorro_headers),
    Source('drivers-amd', driver_url('amd'), parse_drivers('amd'),
           timeout=DRIVER_TIMEOUT, headers=socorro_headers),
    Source('drivers-intel', driver_url('intel'), parse_drivers('intel'),
           timeout=DRIVER_TIMEOUT, headers=socorro_headers),
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
    Source('norton', NORTON_RSS, parse_norton, fmt='text'),
    Source('avast', chocolatey_url('avastfreeantivirus'),
           parse_chocolatey('avast', 'avastfreeantivirus',
                            'Avast Free Antivirus'), fmt='text'),
    Source('malwarebytes', chocolatey_url('malwarebytes'),
           parse_chocolatey('malwarebytes', 'malwarebytes', 'Malwarebytes'),
           fmt='text'),
    Source('eset', winget_url('e/ESET/Nod32'),
           parse_winget('eset', 'ESET.Nod32', 'ESET NOD32 Antivirus'),
           headers={'Accept': 'application/vnd.github+json'}),
    Source('defender', DEFENDER_URL, parse_defender, fmt='text'),
]
COMPUTED = 'android-bulletins'
FEED_NAMES = [s.name for s in SOURCES] + [COMPUTED]


def _get(url, timeout=TIMEOUT, fmt='json', headers=None):
    h = {'User-Agent': USER_AGENT,
         'Accept': 'application/json' if fmt == 'json' else '*/*'}
    h.update(headers or {})
    r = requests.get(url, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.json() if fmt == 'json' else r.text


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
        def run(s):
            url = s.url(today) if callable(s.url) else s.url
            headers = s.headers() if callable(s.headers) else s.headers
            return s.parse(fetch(url, s.timeout, s.fmt, headers), today)
        futures = {pool.submit(run, s): s for s in sources}
        for fut, source in futures.items():
            try:
                events = fut.result(timeout=source.timeout + 10)
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
    models.upsert(models.Event, [r for r in unique.values()
                                 if r['kind'] not in IMMUTABLE_KINDS],
                  ['kind', 'ref'])
    models.upsert(models.Event, [r for r in unique.values()
                                 if r['kind'] in IMMUTABLE_KINDS],
                  ['kind', 'ref'], ignore_conflicts=True)
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
