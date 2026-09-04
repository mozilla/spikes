# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Which version is *current* on a channel on a given day.

The ``current`` version scope (``config.scopes()``) collects, for every
channel, only the crashes of the version that is current on each day:
between the releases of 152 and 153 the release channel is 152.x only,
the beta channel 153.0bN, nightly 154.0a1, ESR its current point release.
The ``strict`` scope goes one step further and keeps only the *exact*
version current on each day: 152.0.1 once it ships (and no longer 152.0),
153.0b3 and no longer b2, 140.15.1esr.  Nightly's version string (154.0a1)
lasts the whole cycle, so its strict view keeps the *day's builds* only:
build ids are timestamps, and ``build_id >= <day>000000`` is the day's
(see :func:`cycle_params`).  A channel's history is therefore a sequence
of *cycles*, one per version, and the seasonal model of these scopes
counts its 28-day cycle from the version's release (the rollout ramp)
instead of the calendar; in the strict scope the ramp is the few days of a
beta or of a dot release.

Sources, fetched by the scheduler every ``versions_refresh_hours`` and
stored in ``dashboard_cycles`` (the web process never fetches):

* product-details: major releases (the release channel's boundaries and
  labels), the betas (``N.0b1`` bounds the beta channel, every ``N.0bK``
  the strict scope), the stability releases (ESR point releases ``X.N.0``;
  the dot releases ``N.0.y`` and ``X.N.yesr`` for the strict scope);
* whattrainisitnow: per version, the day nightly became that version
  (``nightly_start``) and the planned boundaries of the versions still to
  come (the release, each beta, the first dot release), so the forecast
  shows the next cycle start; without it the nightly boundary is the day
  before the previous version's first beta.

Fenix follows the Firefox train (same versions, same days); Thunderbird
has its own calendar and follows Firefox's merge days for nightly.  A new
ESR train (``153.0esr`` next to ``140.x``) becomes the current one
``esr_overlap_weeks`` (12) after its first release, when the old train
gets its last point release.  A cycle starts at 00:00 UTC of its day; the
few hours before the actual ship time make the first day of a cycle small,
which the cycle factors learn like anything else.

Socorro filters: ``major_version`` for release, beta and nightly (it
covers ``155.0``, ``155.0.1``, ``155.0b3``, ``155.0rc2``, ``155.0a1``
alike); the ESR channel needs the exact ``version`` strings of the point
release (``140.15.0esr``, ``140.15.1esr``, ...), since SuperSearch matches
``version`` exactly.  The strict scope always filters on the one exact
``version`` string of its cycle, plus the day's builds on nightly.
(Release candidates build from the release branch and report as
``release`` / ``155.0``, so the beta channel just thins out during the RC
week in every scope.)
"""

import collections
import datetime
import re
import time

import numpy as np
import requests

from spikes.logger import logger
from . import config, models, seasonal


PRODUCT_DETAILS = 'https://product-details.mozilla.org/1.0/'
SCHEDULE = 'https://whattrainisitnow.com/api/release/schedule/'
# product-details feed per product (Fenix follows the Firefox train)
FEEDS = {'Firefox': 'firefox', 'Fenix': 'firefox',
         'Thunderbird': 'thunderbird'}
FEED_NAME = 'versions'          # dashboard_feeds bookkeeping row
NPHASES = 28                    # phases of the cycle component
ESR_DOTS = 10                   # X.N.0esr .. X.N.9esr
SCHEDULE_TIMEOUT = 10
FEED_TIMEOUT = 15
CACHE_SECONDS = 60              # in-process cache of the stored cycles

_cache = {}


def _get(url, timeout):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _date(s):
    return datetime.date.fromisoformat(str(s)[:10])


# --------------------------------------------------------------------------
# Calendars
# --------------------------------------------------------------------------

class Calendar:
    """Release calendar of one product-details feed.

    Attributes (``int major -> date`` unless said otherwise):
        majors: release day of ``N.0``.
        betas: day of ``N.0b1``.
        nightly_starts: day nightly became ``N.0a1`` (from whattrainisitnow,
            else the day before ``(N-1).0b1``).
        esr_points: ``major -> {minor -> date}`` of the ESR point releases
            (``X.0`` included as minor 0).
        future: ``channel -> {major: date}`` of boundaries still to come.
        all_betas: ``major -> {k -> date}`` of every ``N.0bK``.
        dots: ``major -> {y -> date}`` of the release channel's dot
            releases ``N.0.y`` (y >= 1).
        esr_dots: ``major -> {minor -> {y -> date}}`` of the ESR dot
            releases ``X.N.yesr`` (y >= 1).
        planned: ``channel -> {version: date}`` of the exact versions
            still to come (``157.0``, ``156.0b4``, ``155.0.1``, ``158.0a1``).
    The last four serve the strict scope.
    """

    def __init__(self, majors=None, betas=None, nightly_starts=None,
                 esr_points=None, future=None):
        self.majors = dict(majors or {})
        self.betas = dict(betas or {})
        self.nightly_starts = dict(nightly_starts or {})
        self.esr_points = {int(k): dict(v) for k, v in
                           (esr_points or {}).items()}
        self.future = {k: dict(v) for k, v in (future or {}).items()}
        self.all_betas = {}
        self.dots = {}
        self.esr_dots = {}
        self.planned = {}

    def fill_nightly(self):
        """Nightly boundaries not known from a schedule: the day before the
        previous version's first beta (nightly N starts on the merge day
        of N-1, whose b1 ships a day or so later)."""
        for n, b1 in self.betas.items():
            self.nightly_starts.setdefault(
                n + 1, b1 - datetime.timedelta(days=1))

    def plan_esr_points(self, today, max_age_days=90):
        """Plan the ESR point releases still to come: one ships on every
        major's release day, so each train still shipping (a point release
        within *max_age_days*) gets one per future release day of
        ``future['release']``, the minor numbers continuing.  Idempotent."""
        future = sorted(self.future.get('release', {}).values())
        recent = today - datetime.timedelta(days=max_age_days)
        for pts in self.esr_points.values():
            last, minor = max((d, m) for m, d in pts.items())
            if last < recent:
                continue
            for d in future:
                if d > last:
                    minor += 1
                    pts[minor] = d


def parse_version(v):
    """``(major, minor, dot)`` of ``'140.15.0'`` (dot None for 2 parts)."""
    m = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?$', v)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)),
            int(m.group(3)) if m.group(3) is not None else None)


def calendar_from_feeds(major, development, stability):
    """Calendar from the three product-details history files."""
    cal = Calendar()
    for v, d in major.items():
        p = parse_version(v)
        if p and p[1] == 0 and p[2] is None:
            cal.majors[p[0]] = _date(d)
    for v, d in development.items():
        m = re.match(r'^(\d+)\.0b(\d+)$', v)
        if m:
            n, k = int(m.group(1)), int(m.group(2))
            cal.all_betas.setdefault(n, {})[k] = _date(d)
            if k == 1:
                cal.betas[n] = _date(d)
    # X.N.0 with N >= 1 is an ESR point release (the trains are the majors
    # that have some) and X.N.y one of its dot releases; X.0.y is a dot
    # release of the release channel
    for v, d in stability.items():
        p = parse_version(v)
        if not p or p[2] is None:
            continue
        major, minor, dot = p
        if minor == 0:
            if dot >= 1:
                cal.dots.setdefault(major, {})[dot] = _date(d)
        elif dot == 0:
            cal.esr_points.setdefault(major, {})[minor] = _date(d)
        else:
            cal.esr_dots.setdefault(major, {}).setdefault(minor, {})[dot] = \
                _date(d)
    for x in list(cal.esr_points):
        if x in cal.majors:
            cal.esr_points[x][0] = cal.majors[x]
    return cal


def planned_version(major, key):
    """``(channel, exact version)`` of a schedule entry (``beta_3`` ->
    ``156.0b3``, ``dot_release_1`` -> ``156.0.1``), or None for the other
    milestones."""
    m = re.match(r'^beta_(\d+)$', key)
    if m:
        return 'beta', '{}.0b{}'.format(major, m.group(1))
    return {'release': ('release', '{}.0'.format(major)),
            'dot_release_1': ('release', '{}.0.1'.format(major)),
            'nightly_start': ('nightly', '{}.0a1'.format(major))}.get(key)


def apply_schedules(cal, schedules, today):
    """Add whattrainisitnow schedules (``major -> json``): nightly starts,
    the boundaries still to come and the exact versions still to come."""
    for n, sched in schedules.items():
        if not sched:
            continue
        ns = sched.get('nightly_start')
        if ns:
            cal.nightly_starts[n] = _date(ns)
        for channel, key in (('release', 'release'), ('beta', 'beta_1'),
                             ('nightly', 'nightly_start')):
            if sched.get(key):
                d = _date(sched[key])
                if d > today:
                    cal.future.setdefault(channel, {})[n] = d
        for key, value in sched.items():
            planned = planned_version(n, key) if value else None
            if planned is not None and _date(value) > today:
                channel, label = planned
                cal.planned.setdefault(channel, {})[label] = _date(value)
    cal.fill_nightly()
    cal.plan_esr_points(today)
    return cal


def fetch_calendar(feed, today, schedules_for=(), get=_get):
    """Fetch a feed's calendar (and the schedules of *schedules_for*)."""
    base = PRODUCT_DETAILS + feed
    cal = calendar_from_feeds(
        get(base + '_history_major_releases.json', FEED_TIMEOUT),
        get(base + '_history_development_releases.json', FEED_TIMEOUT),
        get(base + '_history_stability_releases.json', FEED_TIMEOUT))
    schedules = {}
    for n in schedules_for:
        try:
            schedules[n] = get('{}?version={}'.format(SCHEDULE, n),
                               SCHEDULE_TIMEOUT)
        except Exception as ex:  # the fallback boundary is used
            logger.warning('Dashboard: schedule of %s unavailable: %s', n, ex)
    return apply_schedules(cal, schedules, today)


# --------------------------------------------------------------------------
# Cycles
# --------------------------------------------------------------------------

def _ranges(starts, since):
    """``[(start, end, major)]`` from ``{major: start}``, oldest first,
    the last one open; only the cycles reaching *since*."""
    items = sorted((d, n) for n, d in starts.items())
    res = []
    for i, (d, n) in enumerate(items):
        end = items[i + 1][0] if i + 1 < len(items) else None
        if end is not None and end <= since:
            continue
        res.append((d, end, n))
    return res


def _train_cycles(cal, channel, since):
    if channel == 'release':
        starts = dict(cal.majors)
    elif channel == 'beta':
        starts = dict(cal.betas)
    else:
        starts = dict(cal.nightly_starts)
    starts.update(cal.future.get(channel, {}))
    return [{'start': s, 'end': e, 'label': str(n),
             'params': {'major_version': n}}
            for s, e, n in _ranges(starts, since)]


def version_key(v):
    """Sort key of a version string: ``154.0.1 < 155.0a1 < 155.0b3 <
    155.0 < 155.0.1``; ``140.15.1esr`` sorts by its numbers."""
    m = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?(?:([ab])(\d+))?', v)
    if not m:
        return (0, 0, 0, 0, 0)
    stage = {'a': 0, 'b': 1}.get(m.group(4), 2)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), stage,
            int(m.group(5) or 0))


def _strict_ranges(starts, since):
    """``[(start, end, version)]`` from ``{version: start}``, oldest first,
    the last one open; only the cycles reaching *since*.  A version of an
    older major shipping after a newer major (154.0.2 after 155.0) is not
    current, and of two versions shipping the same day the higher is."""
    kept = []
    major = 0
    for d, key, v in sorted((d, version_key(v), v)
                            for v, d in starts.items()):
        if key[0] < major:
            continue
        major = key[0]
        if kept and kept[-1][0] == d:
            kept.pop()
        kept.append((d, v))
    res = []
    for i, (d, v) in enumerate(kept):
        end = kept[i + 1][0] if i + 1 < len(kept) else None
        if end is not None and end <= since:
            continue
        res.append((d, end, v))
    return res


BUILD_DAY = 'build_day'   # cycle param: the day's builds only (nightly)


def cycle_params(params, day):
    """SuperSearch filter of a cycle on *day*: its *params*, where
    ``build_day`` (strict nightly) becomes the builds of that day,
    ``build_id >= <day>000000`` (build ids are ``YYYYMMDDHHMMSS``)."""
    params = dict(params)
    if params.pop(BUILD_DAY, False):
        params['build_id'] = '>=' + day.strftime('%Y%m%d') + '000000'
    return params


def _strict_train_cycles(cal, channel, since):
    """One cycle per exact version string, its filter that string (and
    the day's builds on nightly, whose string lasts the whole cycle)."""
    if channel == 'release':
        starts = {'{}.0'.format(n): d for n, d in cal.majors.items()}
        for n, dots in cal.dots.items():
            for y, d in dots.items():
                starts['{}.0.{}'.format(n, y)] = d
    elif channel == 'beta':
        starts = {'{}.0b{}'.format(n, k): d
                  for n, betas in cal.all_betas.items()
                  for k, d in betas.items()}
    else:
        starts = {'{}.0a1'.format(n): d
                  for n, d in cal.nightly_starts.items()}
    for label, d in cal.planned.get(channel, {}).items():
        starts.setdefault(label, d)  # what shipped beats what was planned
    res = [{'start': s, 'end': e, 'label': v, 'params': {'version': [v]}}
           for s, e, v in _strict_ranges(starts, since)]
    if channel == 'nightly':
        for c in res:
            c['params'][BUILD_DAY] = True
    return res


def esr_versions(major, minor):
    """Exact ``version`` strings of an ESR point release."""
    return ['{}.{}esr'.format(major, minor)] + [
        esr_version(major, minor, k) for k in range(ESR_DOTS)]


def esr_version(major, minor, dot):
    """The ``version`` string of one ESR release: ``153.0esr`` for a
    train's first, ``140.15.0esr`` for a point release, ``140.15.1esr``
    for its dot release."""
    if minor == 0 and dot == 0:
        return '{}.0esr'.format(major)
    return '{}.{}.{}esr'.format(major, minor, dot)


def _esr_cycles(cal, since, overlap_weeks, strict=False):
    """One cycle per point release of the current ESR train (per point or
    dot release with *strict*); a new train becomes current
    *overlap_weeks* after its first release."""
    trains = sorted(x for x, pts in cal.esr_points.items() if 0 in pts)
    if not trains:
        return []
    overlap = datetime.timedelta(weeks=overlap_weeks)
    switches = {x: cal.esr_points[x][0] + overlap for x in trains}
    switches[trains[0]] = cal.esr_points[trains[0]][0]  # the first: at once
    # every day something changes: a point release, a dot release (strict)
    # or a train switch
    boundaries = {d for x in trains for d in cal.esr_points[x].values()}
    boundaries |= set(switches.values())
    if strict:
        boundaries |= {d for x in trains
                       for dots in cal.esr_dots.get(x, {}).values()
                       for d in dots.values()}
    boundaries = sorted(boundaries)
    res = []
    for i, day in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else None
        train = max((x for x in trains if switches[x] <= day),
                    default=trains[0])
        points = [(d, m) for m, d in cal.esr_points[train].items()
                  if d <= day]
        if not points:
            continue
        _, minor = max(points)
        if strict:
            dots = [(d, y) for y, d in
                    cal.esr_dots.get(train, {}).get(minor, {}).items()
                    if d <= day]
            label = esr_version(train, minor, max(dots)[1] if dots else 0)
            params = {'version': [label]}
        else:
            label = '{}.{}'.format(train, minor)
            params = {'version': esr_versions(train, minor)}
        if res and res[-1]['label'] == label:
            res[-1]['end'] = end
        else:
            res.append({'start': day, 'end': end, 'label': label,
                        'params': params})
    return [c for c in res if c['end'] is None or c['end'] > since]


def compute_cycles(cal, channel, since, overlap_weeks=12, strict=False):
    """Cycles of a real *channel* reaching *since*, oldest first: one per
    version (the ``current`` scope), or one per exact version string with
    *strict* (the ``strict`` scope)."""
    if channel == 'esr':
        return _esr_cycles(cal, since, overlap_weeks, strict)
    if strict:
        return _strict_train_cycles(cal, channel, since)
    return _train_cycles(cal, channel, since)


# --------------------------------------------------------------------------
# Refresh (scheduler)
# --------------------------------------------------------------------------

def _since(today):
    return today - datetime.timedelta(days=config.fit_history_days())


def schedules_wanted(cal, today):
    """Majors whose schedule is worth fetching: those whose cycle can
    touch the backfilled history, up to the current nightly plus one."""
    first = today - datetime.timedelta(
        days=config.get('history_days', 180) + 60)
    majors = sorted(cal.majors)
    if not majors:
        return []
    low = max([n for n in majors if cal.majors[n] <= first] or [majors[0]])
    return list(range(low, majors[-1] + 4))


def refresh(now, get=_get):
    """Fetch the calendars and store the cycles of every channel.  Returns
    a summary; raises when a feed cannot be fetched."""
    today = now.date()
    calendars = {}
    firefox = None
    for feed in sorted(set(FEEDS.values())):
        probe = calendar_from_feeds(
            get(PRODUCT_DETAILS + feed + '_history_major_releases.json',
                FEED_TIMEOUT), {}, {})
        wanted = schedules_wanted(probe, today) if feed == 'firefox' else []
        cal = fetch_calendar(feed, today, wanted, get=get)
        if feed == 'firefox':
            firefox = cal
        calendars[feed] = cal
    # Thunderbird ships on Firefox's days: its nightly follows Firefox's
    # merge days and its planned releases (ESR point releases included)
    # Firefox's schedule
    if firefox is not None:
        for feed, cal in calendars.items():
            if feed != 'firefox':
                cal.nightly_starts.update(firefox.nightly_starts)
                for channel in ('nightly', 'release'):
                    for n, d in firefox.future.get(channel, {}).items():
                        cal.future.setdefault(channel, {})[n] = d
                    for v, d in firefox.planned.get(channel, {}).items():
                        # not the dot releases: Thunderbird has its own
                        if channel == 'nightly' or v.endswith('.0'):
                            cal.planned.setdefault(channel, {}).setdefault(
                                v, d)
                cal.plan_esr_points(today)
    since = _since(today)
    overlap = config.get('esr_overlap_weeks', 12)
    changed = 0
    total = 0
    for product in config.products():
        cal = calendars.get(FEEDS.get(product, 'firefox'))
        for channel in config.channels(product):
            for scope in versioned_scopes():
                rows = compute_cycles(cal, channel, since, overlap,
                                      strict=scope == config.SCOPE_STRICT)
                total += len(rows)
                if models.replace_cycles(product, cycles_key(channel, scope),
                                         rows, now):
                    changed += 1
    _cache.clear()
    return {'cycles': total, 'changed_channels': changed}


def versioned_scopes():
    """The scopes that need version cycles (every one but ``all``)."""
    return [s for s in config.scopes() if s != config.SCOPE_ALL]


def cycles_key(channel, scope):
    """The ``dashboard_cycles`` channel a (real channel, scope) is stored
    under: the channel itself for the ``current`` scope, the channel key
    (``beta@strict``) otherwise."""
    return channel if scope == config.SCOPE_CURRENT else \
        config.channel_key(channel, scope)


def maybe_refresh(now):
    """Refresh when due (every ``versions_refresh_hours``, sooner after a
    failure).  Records the outcome in ``dashboard_feeds``; returns the
    summary or None when nothing was due."""
    if not versioned_scopes():
        return None
    feed = models.load_feeds().get(FEED_NAME)
    if feed is not None:
        hours = config.get('versions_refresh_hours', 6) if feed.ok else \
            config.get('events_retry_hours', 1)
        if now - feed.fetched_at < datetime.timedelta(hours=hours):
            return None
    try:
        res = refresh(now)
        ok, message, items = True, None, res['cycles']
    except Exception as ex:  # noqa: BLE001
        db_rollback()
        logger.warning('Dashboard: version calendars unavailable: %s', ex)
        res = {'error': str(ex)}
        ok, message, items = False, str(ex)[:200], 0
    models.upsert(models.Feed, [{'name': FEED_NAME, 'fetched_at': now,
                                 'ok': ok, 'items': items,
                                 'message': message}], ['name'])
    return res


def db_rollback():
    from spikes import db
    db.session.rollback()


# --------------------------------------------------------------------------
# Reading (scheduler and web)
# --------------------------------------------------------------------------

# a stored cycle, detached from the database session: the cache outlives
# the request (and the session) that loaded it
CycleData = collections.namedtuple('CycleData', 'start end label params')


class Cycles:
    """The stored cycles of one (product, real channel)."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: r.start)

    def __bool__(self):
        return bool(self.rows)

    def at(self, day):
        """The cycle *day* falls in, or None."""
        for row in reversed(self.rows):
            if row.start <= day and (row.end is None or day < row.end):
                return row
        return None

    def split(self, start, end):
        """``[(start, end, cycle)]`` covering ``[start, end)`` with one
        cycle (or None) per range."""
        res = []
        day = start
        one = datetime.timedelta(days=1)
        while day < end:
            c = self.at(day)
            if res and res[-1][2] is c:
                res[-1][1] = day + one
            else:
                res.append([day, day + one, c])
            day += one
        return [(a, b, c) for a, b, c in res]

    def next_start(self, today):
        """First cycle start after *today* (a boundary still to come)."""
        starts = [r.start for r in self.rows if r.start > today]
        return min(starts) if starts else None

    @property
    def per_day(self):
        """Whether the filter changes every day (the day's builds): the
        history is then fetched day by day."""
        return any(r.params.get(BUILD_DAY) for r in self.rows)

    def phase(self, dates):
        """Days since the cycle start, 0..27 (the calendar phase for days
        outside any known cycle)."""
        fallback = seasonal.cycle_phase(dates)
        res = np.array(fallback, dtype=np.int64)
        for i, d in enumerate(dates):
            c = self.at(d)
            if c is not None:
                res[i] = min(NPHASES - 1, (d - c.start).days)
        return res


def cycles_for(product, channel_key):
    """The :class:`Cycles` of a versioned channel key (cached a minute),
    None for the ``all`` scope."""
    channel, scope = config.split_channel(channel_key)
    if scope == config.SCOPE_ALL:
        return None
    key = (product, channel, scope)
    hit = _cache.get(key)
    now = time.time()
    if hit is None or now - hit[0] > CACHE_SECONDS:
        rows = [CycleData(c.start, c.end, c.label, c.params)
                for c in models.load_cycles(product,
                                            cycles_key(channel, scope))]
        hit = (now, Cycles(rows))
        _cache[key] = hit
    return hit[1]


def components_for(product, channel_key):
    """Seasonal components of a channel key: the calendar cycle for the
    ``all`` scope, the days since the version's release otherwise."""
    cycles = cycles_for(product, channel_key)
    if cycles is None:
        return seasonal.COMPONENTS
    return seasonal.with_cycle_phase(cycles.phase)


def label_for(product, channel_key, day):
    """Version label current on *day* (``'155'``, ``'140.15'``; the exact
    version, ``'156.0b3'``, in the strict scope), or None."""
    cycles = cycles_for(product, channel_key)
    c = cycles.at(day) if cycles else None
    return c.label if c is not None else None


def params_for(product, channel_key, day):
    """SuperSearch filter of the version current on *day* (``{}`` for the
    ``all`` scope or an unknown day)."""
    cycles = cycles_for(product, channel_key)
    c = cycles.at(day) if cycles else None
    return cycle_params(c.params, day) if c is not None else {}
