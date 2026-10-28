# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Nimbus launch, change and end events for channel and signature charts.

Experimenter supplies launch and end dates. Each scheduler run also polls
Remote Settings, compares tracked fields with ``dashboard_nimbus`` and
records deletions as ends. Earlier recipe revisions are not reconstructed.

Events are stored in ``dashboard_events`` with product/channel metadata
and excluded from the platform events endpoint.
"""

import datetime
import hashlib
import json
import re

import requests

from spikes import db
from spikes.logger import logger
from . import config, models


TIMEOUT = 15
EXPERIMENTER_TIMEOUT = 60
USER_AGENT = 'crash-spikes dashboard (+https://github.com/mozilla/spikes)'
EXPERIMENTER = 'https://experimenter.services.mozilla.com'
SETTINGS = 'https://firefox.settings.services.mozilla.com/v1'
MONITOR = (SETTINGS + '/buckets/monitor/collections/changes/changeset'
           '?_expected=0')
COLLECTIONS = ('nimbus-desktop-experiments', 'nimbus-secure-experiments',
               'nimbus-mobile-experiments')
# Nimbus app names differ from Experimenter's application names.
PRODUCTS = {'firefox_desktop': 'Firefox', 'fenix': 'Fenix'}
APPLICATIONS = ('firefox-desktop', 'fenix')
SOURCE = 'nimbus'
LABEL = 'Nimbus'
FEED = 'nimbus-live'
# Heuristic: treat modifications within an hour of publication as part of
# the launch when a recipe is first seen.
PUBLISH_SLACK = datetime.timedelta(hours=1)
CHANNEL_RE = re.compile(r'browserSettings\.update\.channel\s*(?:in|==)\s*'
                        r'''(\[[^\]]*\]|"[^"]*"|'[^']*')''')

# collection -> its timestamp last applied by this process
_applied = {}


def experimenter_url(application):
    return '{}/api/v6/experiments/?application={}'.format(EXPERIMENTER,
                                                          application)


def summary_url(slug):
    return '{}/nimbus/{}/summary'.format(EXPERIMENTER, slug)


def changeset_url(collection, timestamp, since=None):
    url = '{}/buckets/main/collections/{}/changeset?_expected={}'.format(
        SETTINGS, collection, timestamp)
    return url if since is None else url + '&_since=%22{}%22'.format(since)


# --------------------------------------------------------------------------
# Recipes
# --------------------------------------------------------------------------

def channels_of(recipe, product):
    """Detected channels in configured order; () for no configured match,
    None when no channel constraint is recognized."""
    if recipe.get('channel'):
        found = {recipe['channel']}
    else:
        found = set()
        for m in CHANNEL_RE.findall(recipe.get('targeting') or ''):
            found.update(re.findall(r'''["'](\w+)["']''', m))
    if not found:
        return None
    return tuple(c for c in config.channels(product) if c in found)


def population(recipe):
    """Enrollment bucket fraction, or None without a positive total."""
    b = recipe.get('bucketConfig') or {}
    total = b.get('total') or 0
    return (b.get('count') or 0) / total if total > 0 else None


def fmt_share(share):
    if share is None:
        return '? %'
    pct = share * 100
    return '{:g} %'.format(round(pct, 1 if pct < 10 else 0))


def _digest(value):
    text = json.dumps(value, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _ref(text):
    """Hash long refs to fit Event.ref's 64-character limit."""
    if len(text) <= 64:
        return text
    return text[:55] + '~' + hashlib.sha1(text.encode()).hexdigest()[:8]


def _timestamp(value):
    """Parse a timestamp as naive UTC seconds; None if absent or invalid."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace('Z',
                                                                '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _ms(value):
    return datetime.datetime.fromtimestamp(
        value / 1000, datetime.timezone.utc).replace(tzinfo=None,
                                                     microsecond=0)


def _day(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _start(recipe):
    """Publication time, falling back to startDate."""
    return (_timestamp(recipe.get('publishedDate')) or
            _day(recipe.get('startDate')))


def snapshot(recipe):
    """Recipe fields stored in dashboard_nimbus, without bookkeeping."""
    product = PRODUCTS.get(recipe.get('appName'))
    channels = channels_of(recipe, product) if product else None
    return {'product': product,
            'channels': None if channels is None else ','.join(channels),
            'name': (recipe.get('userFacingName') or recipe.get('slug') or
                     recipe.get('id') or '?')[:160],
            'is_rollout': bool(recipe.get('isRollout')),
            'population': population(recipe),
            'paused': bool(recipe.get('isEnrollmentPaused')),
            'targeting': _digest(recipe.get('targeting') or ''),
            'branches': _digest(recipe.get('branches') or [])}


def _what(state):
    return 'rollout' if state['is_rollout'] else 'experiment'


def _row(kind, slug, ref, state, when, title, detail):
    """An event row; *when* is a datetime (known time) or a date."""
    timed = isinstance(when, datetime.datetime)
    return {'source': SOURCE, 'kind': kind, 'ref': _ref(ref),
            'day': when.date() if timed else when,
            'at': when if timed else None, 'title': title[:160],
            'detail': detail[:400], 'url': summary_url(slug)[:400],
            'search': None, 'product': state['product'],
            'channels': state['channels']}


def _tail(slug, state):
    """Channel labels and slug for event details."""
    channels = state['channels']
    parts = [channels.replace(',', ', ')] if channels else []
    return parts + [slug]


def launch(slug, state, when, live):
    share = '{} of eligible clients ({})'.format(
        fmt_share(state['population']), 'now' if live else 'at the end')
    return _row('nimbus-launch', slug, slug, state, when,
                'Launched: ' + state['name'],
                ' · '.join([_what(state), share] + _tail(slug, state)))


def end(slug, state, when):
    return _row('nimbus-end', slug, slug, state, when,
                'Ended: ' + state['name'],
                ' · '.join([_what(state), fmt_share(state['population']) +
                            ' of eligible clients'] + _tail(slug, state)))


def change(slug, state, when, changes):
    """*changes*: what changed (strings), or None when unknown."""
    if changes is None:
        what = ['last change before the dashboard followed Nimbus (what '
                'changed is not known)', '{} now at {}'.format(
                    _what(state), fmt_share(state['population']))]
    else:
        what = changes + [_what(state)]
    return _row('nimbus-change', slug, '{}@{}'.format(
        slug, when.strftime('%Y%m%d%H%M%S')), state, when,
        'Changed: ' + state['name'], ' · '.join(what + _tail(slug, state)))


def diff(previous, state):
    """Describe changes to tracked fields; ignore other recipe fields."""
    out = []
    if previous.population != state['population']:
        out.append('population {} → {}'.format(
            fmt_share(previous.population), fmt_share(state['population'])))
    if bool(previous.paused) != state['paused']:
        out.append('enrollment ended' if state['paused']
                   else 'enrollment reopened')
    if previous.targeting != state['targeting']:
        out.append('targeting changed')
    if previous.branches != state['branches']:
        out.append('feature values changed' if state['is_rollout']
                   else 'branches changed')
    return out


def shown(state):
    """Whether the recipe has a supported product and channel."""
    return state['product'] is not None and state['channels'] != ''


# --------------------------------------------------------------------------
# Experimenter (history, an event feed of events.py)
# --------------------------------------------------------------------------

def parse_experimenter(payload, today=None):
    """Launches and dated ends from an Experimenter list."""
    out = []
    for r in payload if isinstance(payload, list) else []:
        slug = r.get('slug')
        if not slug:
            continue
        state = snapshot(r)
        if not shown(state):
            continue
        ended = _day(r.get('endDate'))
        when = _start(r)
        if when is not None:
            out.append(launch(slug, state, when, live=ended is None))
        if ended is not None:
            out.append(end(slug, state, ended))
    return out


# --------------------------------------------------------------------------
# Remote Settings (changes of the live recipes, every run)
# --------------------------------------------------------------------------

def _get(url, timeout=TIMEOUT):
    r = requests.get(url, timeout=timeout,
                     headers={'User-Agent': USER_AGENT,
                              'Accept': 'application/json'})
    r.raise_for_status()
    return r.json()


def apply_changes(collection, changes, known, now):
    """Apply records oldest first, updating *known* (slug -> NimbusRecipe).
    Return ``(events, states, deleted slugs)``."""
    events, states, deleted = [], {}, []
    for rec in sorted(changes, key=lambda c: c.get('last_modified') or 0):
        slug, modified = rec.get('id'), rec.get('last_modified')
        if not slug or modified is None:
            continue
        at = _ms(modified)
        previous = known.get(slug)
        if rec.get('deleted'):
            if previous is not None:
                state = {c: getattr(previous, c) for c in (
                    'product', 'channels', 'name', 'is_rollout',
                    'population')}
                if shown(state):
                    events.append(end(slug, state, at))
                deleted.append(slug)
                known.pop(slug)
                states.pop(slug, None)
            continue
        if slug in deleted:  # deleted, then published again
            deleted.remove(slug)
        state = snapshot(rec)
        if shown(state):
            if previous is None:
                # Both feeds must use the same launch date.
                started = _start(rec)
                if started is not None:
                    events.append(launch(slug, state, started, live=True))
                # A later modification is known, but its previous state is not.
                if isinstance(started, datetime.datetime):
                    later = at - started > PUBLISH_SLACK
                else:
                    later = started is None or at.date() > started
                if later:
                    events.append(change(slug, state, at, None))
            else:
                what = diff(previous, state)
                if what:
                    events.append(change(slug, state, at, what))
        row = dict(state, slug=slug, collection=collection,
                   last_modified=modified, updated_at=now)
        states[slug] = row
        # Compare later records in this batch against the updated state.
        known[slug] = models.NimbusRecipe(**row)
    return events, list(states.values()), deleted


def poll(now, fetch=None):
    """Poll and commit changes and feed status. Return counts for changed
    collections, None if unchanged, or ``{'ok': False}`` on fetch failure."""
    fetch = fetch or _get
    try:
        monitor = fetch(MONITOR)
        stamps = {c.get('collection'): c.get('last_modified')
                  for c in monitor.get('changes', [])
                  if c.get('bucket') == 'main'}
        known = models.load_nimbus_recipes()
        changed, rows, states, deleted = [], [], [], []
        for collection in COLLECTIONS:
            stamp = stamps.get(collection)
            if stamp is None or _applied.get(collection) == stamp:
                continue
            mine = {s: r for s, r in known.items()
                    if r.collection == collection}
            since = max((r.last_modified for r in mine.values()),
                        default=None)
            if since is not None and since >= stamp:
                _applied[collection] = stamp
                continue
            payload = fetch(changeset_url(collection, stamp, since))
            e, s, d = apply_changes(collection, payload.get('changes', []),
                                    mine, now)
            rows += e
            states += s
            deleted += d
            changed.append((collection, stamp))
    except Exception as ex:  # the charts work without the lane
        db.session.rollback()
        logger.warning('Dashboard: Nimbus poll failed: %s', ex)
        models.upsert(models.Feed, [{'name': FEED, 'fetched_at': now,
                                     'ok': False, 'items': 0,
                                     'message': str(ex)[:200]}], ['name'])
        db.session.commit()
        return {'ok': False}
    retention = config.get('events_retention_days', 800)
    start = now.date() - datetime.timedelta(days=retention)
    rows = [dict(r, updated_at=now) for r in rows if r['day'] >= start]
    models.upsert(models.Event, rows, ['kind', 'ref'])
    models.upsert(models.NimbusRecipe, states, ['slug'])
    models.delete_nimbus_recipes(deleted)
    models.upsert(models.Feed, [{'name': FEED, 'fetched_at': now, 'ok': True,
                                 'items': len(rows), 'message': None}],
                  ['name'])
    db.session.commit()  # Clear previous failures even when nothing changed.
    _applied.update(changed)
    if not changed:
        return None
    return {'collections': len(changed), 'events': len(rows)}


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

def _ts(dt):
    return dt.isoformat() + 'Z' if dt else None


def marks(product, channel, since, until=None):
    """Channel events in ``[since, until]``, shared across scopes.
    Group by day; day-only items precede items ordered by time."""
    base = config.split_channel(channel)[0]
    groups = {}
    for e in models.load_events(since, until, product=product):
        if e.channels is not None and base not in e.channels.split(','):
            continue
        g = groups.get(e.day)
        if g is None:
            g = groups[e.day] = {'day': e.day.isoformat(), 'source': SOURCE,
                                 'label': LABEL, 'items': []}
        g['items'].append({'kind': e.kind, 'title': e.title,
                           'detail': e.detail, 'url': e.url,
                           'at': _ts(e.at)})
    for g in groups.values():
        g['items'].sort(key=lambda it: (it['at'] or '', it['title']))
    return [groups[d] for d in sorted(groups)]
