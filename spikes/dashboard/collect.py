# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Decide what to fetch from Socorro and store the results.

:func:`plan` looks at the ``dashboard_days`` bookkeeping of a channel and
returns the fetch *units* still needed, most urgent first:

1. today: the merged ``day`` query the first time and then every
   ``installs_refresh_minutes`` (distinct installs are not additive, and a
   full pass also picks up reports the processor indexed late into an
   earlier hour); in between, an incremental ``recent`` query over the
   last hours (from one hour before the previous fetch) whose buckets
   *replace* the stored hours;
2. every recent day (``day_backfill_days``) that is not *final* yet and any
   older day left non-final, with the merged ``day`` query;
3. older days without any data, in ``daily_chunk_days`` chunks of the
   ``daily`` + ``hourly_total`` queries (history for the seasonal model),
   and days whose channel total still lacks its hourly split.

A day becomes final once it has been fetched at least ``final_grace_hours``
after its end *and* its total did not change since the previous fetch
(the SuperSearch ``date`` is the collector receipt time; the processor can
index reports into past buckets for a while).  Days older than the recent
window are final as soon as they are fetched.

Every unit is written in its own transaction, the ``dashboard_days`` row
last, so a crash or a killed dyno never leaves a half-written day that
looks complete; the next run simply re-plans.
"""

import datetime

from spikes import db
from spikes.logger import logger
from . import config, models, socorro


class Unit:
    """A query to run: ``kind`` over ``[start, end)`` (dates, or naive UTC
    datetimes for ``recent``)."""

    def __init__(self, kind, product, channel, start, end):
        self.kind = kind
        self.product = product
        self.channel = channel
        self.start = start
        self.end = end
        self.result = None

    @property
    def day(self):
        return self.start.date() if isinstance(
            self.start, datetime.datetime) else self.start

    def __repr__(self):
        return '<Unit {} {}/{} {}..{}>'.format(
            self.kind, self.product, self.channel, self.start, self.end)

    @property
    def nqueries(self):
        return 1

    def params(self):
        return socorro.query_params(self.kind, self.product, self.channel,
                                    self.start, self.end)


def is_final(day, as_of, crashes, prev_crashes, today, grace_hours,
             recent_days):
    """Whether *day* can be considered complete."""
    end = datetime.datetime(day.year, day.month, day.day) + \
        datetime.timedelta(days=1, hours=grace_hours)
    if as_of < end:
        return False
    if (today - day).days > recent_days:
        return True
    return prev_crashes is not None and prev_crashes == crashes


def plan(product, channel, today, history_days=None, recent_days=None,
         chunk_days=None, now=None):
    """Units still needed for a channel, most urgent first."""
    if history_days is None:
        history_days = config.get('history_days', 180)
    if recent_days is None:
        recent_days = config.get('day_backfill_days', 7)
    if chunk_days is None:
        chunk_days = config.get('daily_chunk_days', 14)
    if now is None:
        now = models.utcnow()
    first = today - datetime.timedelta(days=history_days)
    rows = {r.day: r for r in models.load_days(product, channel, first)}
    units = []
    one = datetime.timedelta(days=1)
    # 1. today: full fetch the first time, incremental afterwards
    row = rows.get(today)
    if row is None or not row.complete or row.as_of is None:
        units.append(Unit('day', product, channel, today, today + one))
    else:
        stale = now - datetime.timedelta(
            minutes=config.get('installs_refresh_minutes', 30))
        if row.installs_as_of is None or row.installs_as_of < stale:
            # periodic full refresh: the distinct installs (not additive)
            # and any report the processor indexed late into an hour the
            # incremental window no longer covers
            units.append(Unit('day', product, channel, today, today + one))
        else:
            overlap = datetime.timedelta(
                hours=config.get('recent_overlap_hours', 1))
            day_start = datetime.datetime(today.year, today.month,
                                          today.day)
            start = max(day_start, (row.as_of - overlap).replace(
                minute=0, second=0, microsecond=0))
            units.append(Unit('recent', product, channel, start, now))
    # 2. the recent days that are not final / not complete
    for i in range(1, recent_days + 1):
        day = today - i * one
        row = rows.get(day)
        if row is None or not (row.final and row.complete):
            units.append(Unit('day', product, channel, day, day + one))
    # 1b. older days left non-final (e.g. a run cut short before an
    #     outage): fetch them once more, they become final on that fetch
    for row in rows.values():
        if row.day < today - recent_days * one and not row.final:
            units.append(Unit('day', product, channel, row.day,
                              row.day + one))
    # 2. history: days with no row at all -> daily + hourly_total chunks;
    #    days with a row but no hourly split of the total -> hourly_total
    total_id = models.total_series(product, channel, create=False)
    have_hourly = set()
    if total_id is not None:
        have_hourly = set(models.hourly_days(total_id, first))
    missing, missing_hourly = [], []
    day = first
    while day < today - recent_days * one:
        if day not in rows:
            missing.append(day)
        elif day not in have_hourly and not rows[day].complete:
            missing_hourly.append(day)
        day += one
    for start, end in _chunks_of(missing, chunk_days):
        units.append(Unit('daily', product, channel, start, end))
        units.append(Unit('hourly_total', product, channel, start, end))
    for start, end in _chunks_of(missing_hourly, chunk_days):
        units.append(Unit('hourly_total', product, channel, start, end))
    return units


def _chunks_of(days, chunk_days):
    """Group sorted days into consecutive ``[start, end)`` ranges."""
    one = datetime.timedelta(days=1)
    chunks = []
    for day in days:
        if chunks and day == chunks[-1][1] and \
                (chunks[-1][1] - chunks[-1][0]).days < chunk_days:
            chunks[-1][1] = day + one
        else:
            chunks.append([day, day + one])
    return [(a, b) for a, b in chunks]


def plan_all(today, pairs=None, now=None):
    """Units for all channels, interleaved so every channel progresses."""
    if pairs is None:
        pairs = config.pairs()
    per_channel = [plan(p, c, today, now=now) for p, c in pairs]
    units = []
    for i in range(max((len(u) for u in per_channel), default=0)):
        for lst in per_channel:
            if i < len(lst):
                units.append(lst[i])
    return units


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _noise_checker(channel):
    patterns = socorro.noise_patterns(channel)
    return lambda s: socorro.is_noise(s, patterns)


def write_day(unit, parsed, as_of, today):
    """Store a merged ``day`` result."""
    product, channel = unit.product, unit.channel
    day = parsed['day'] or unit.start
    grace = config.get('final_grace_hours', 6)
    recent = config.get('day_backfill_days', 7)
    sgns = parsed['signatures']
    ids = models.series_ids(product, channel, sgns.keys(),
                            noise=_noise_checker(channel))
    total_id = models.total_series(product, channel)
    daily = [{'series_id': ids[s], 'day': day, 'crashes': info['crashes'],
              'installs': info['installs'],
              'installs_crashes': info['crashes']}
             for s, info in sgns.items()]
    daily.append({'series_id': total_id, 'day': day,
                  'crashes': parsed['total'],
                  'installs': parsed.get('installs'),
                  'installs_crashes': parsed['total']})
    models.upsert(models.Daily, daily, ['series_id', 'day'])
    hourly = [{'series_id': ids[s], 'day': day, 'hourly': info['hourly']}
              for s, info in sgns.items() if sum(info['hourly']) > 0]
    hourly.append({'series_id': total_id, 'day': day,
                   'hourly': parsed['hourly_total'],
                   'installs': parsed.get('hourly_installs')})
    models.upsert(models.Hourly, hourly, ['series_id', 'day'])
    models.update_seen([ids[s] for s, i in sgns.items() if i['crashes'] > 0],
                       day)
    row = models.get_day(product, channel, day)
    prev = row.crashes if row is not None else None
    final = is_final(day, as_of, parsed['total'], prev, today, grace, recent)
    models.upsert_day(product, channel, day, crashes=parsed['total'],
                      prev_crashes=prev, cutoff=parsed['cutoff'],
                      as_of=as_of, installs_as_of=as_of, final=final,
                      complete=True, hours_capped=parsed['hours_capped'])


def write_recent(unit, parsed, as_of, today):
    """Apply a ``recent`` result: replace the hours of the window.

    Every hour bucket that lies inside ``[unit.start, unit.end)`` is
    replaced for every series of the channel (a series absent from the
    response in such an hour had no crash there), then the day counts are
    recomputed as the sum of the hourly arrays.  Installs are left to the
    ``installs`` query.
    """
    product, channel = unit.product, unit.channel
    day = unit.day
    # a window never crosses midnight: a new day starts with a full fetch
    first_hour = unit.start.hour
    end = unit.end
    if end.date() > day:
        last_hour = HOURS_LAST
    else:
        last_hour = end.hour if (end.minute or end.second) else end.hour - 1
    hours = list(range(first_hour, min(last_hour, HOURS_LAST) + 1))
    if not hours:
        return
    data = parsed.get(day, {'hourly_total': {}, 'hourly_installs': None,
                            'signatures': {}})
    total_id = models.total_series(product, channel)
    ids = models.series_ids(product, channel, data['signatures'].keys(),
                            noise=_noise_checker(channel))
    stored = models.channel_hourly(product, channel, day)
    changed = set()
    for sid in set(ids.values()) - set(stored):
        stored[sid] = [0] * 24
        changed.add(sid)
    total = stored.setdefault(total_id, [0] * 24)
    for h in hours:
        for sid, arr in stored.items():
            if arr[h] != 0:
                arr[h] = 0
                changed.add(sid)
        total[h] = data['hourly_total'].get(h, 0)
    changed.add(total_id)
    for sgn, per_hour in data['signatures'].items():
        arr = stored[ids[sgn]]
        for h, count in per_hour.items():
            if h in hours:
                arr[h] = count
        changed.add(ids[sgn])
    installs_row = None
    if data['hourly_installs'] is not None:
        current = models.load_hourly([total_id], [day], installs=True)
        inst = list(current.get(total_id, {}).get(day) or [0] * 24)
        for h in hours:
            inst[h] = data['hourly_installs'].get(h, 0)
        installs_row = inst
    # signatures first seen today in this window: the window's distinct
    # installs are the day's so far (the periodic full refresh corrects it)
    new_today = set(ids.values()) - set(
        models.load_daily(list(ids.values()), day, day))
    window_installs = data.get('installs') or {}
    hourly_rows, daily_rows = [], []
    for sid in changed:
        row = {'series_id': sid, 'day': day, 'hourly': stored[sid]}
        if sid == total_id and installs_row is not None:
            row['installs'] = installs_row
        hourly_rows.append(row)
        drow = {'series_id': sid, 'day': day,
                'crashes': int(sum(stored[sid]))}
        if sid in new_today:
            sgn = next((k for k, v in ids.items() if v == sid), None)
            if sgn in window_installs:
                drow['installs'] = window_installs[sgn]
                drow['installs_crashes'] = drow['crashes']
        daily_rows.append(drow)
    models.upsert(models.Hourly, hourly_rows, ['series_id', 'day'])
    models.upsert(models.Daily, daily_rows, ['series_id', 'day'])
    models.update_seen([sid for sid in changed if sid != total_id and
                        sum(stored[sid]) > 0], day)
    models.upsert_day(product, channel, day, crashes=int(sum(total)),
                      as_of=as_of)


def write_installs(unit, parsed, as_of, today):
    """Apply an ``installs`` result: the distinct installs so far today."""
    product, channel = unit.product, unit.channel
    day = unit.day
    sgns = parsed['signatures']
    ids = models.series_ids(product, channel, sgns.keys(),
                            noise=_noise_checker(channel))
    total_id = models.total_series(product, channel)
    # counts stay the hourly path's business (Daily.crashes == sum of the
    # hourly array); the crash count seen with the installs is kept apart
    rows = [{'series_id': ids[sgn], 'day': day, 'installs': installs,
             'installs_crashes': crashes}
            for sgn, (crashes, installs) in sgns.items()]
    rows.append({'series_id': total_id, 'day': day,
                 'installs': parsed['installs'],
                 'installs_crashes': parsed['total']})
    models.upsert(models.Daily, rows, ['series_id', 'day'])
    models.upsert_day(product, channel, day, installs_as_of=as_of,
                      cutoff=parsed['cutoff'])


def write_daily(unit, parsed, as_of, today):
    """Store a ``daily`` history chunk (per-day counts)."""
    product, channel = unit.product, unit.channel
    grace = config.get('final_grace_hours', 6)
    recent = config.get('day_backfill_days', 7)
    total_id = models.total_series(product, channel)
    noise = _noise_checker(channel)
    day = unit.start
    while day < unit.end:
        info = parsed.get(day, {'total': 0, 'signatures': {}, 'cutoff': None})
        ids = models.series_ids(product, channel, info['signatures'].keys(),
                                noise=noise)
        rows = [{'series_id': ids[s], 'day': day, 'crashes': c}
                for s, c in info['signatures'].items()]
        rows.append({'series_id': total_id, 'day': day,
                     'crashes': info['total']})
        models.upsert(models.Daily, rows, ['series_id', 'day'])
        models.update_seen([ids[s] for s, c in info['signatures'].items()
                            if c > 0], day)
        row = models.get_day(product, channel, day)
        if row is None or not row.complete:
            final = is_final(day, as_of, info['total'], None, today, grace,
                             recent)
            models.upsert_day(product, channel, day, crashes=info['total'],
                              cutoff=info['cutoff'], as_of=as_of,
                              final=final, complete=False)
        day += datetime.timedelta(days=1)


def write_hourly_total(unit, parsed, as_of, today):
    """Store an ``hourly_total`` history chunk (channel total per hour)."""
    total_id = models.total_series(unit.product, unit.channel)
    rows = []
    day = unit.start
    while day < unit.end:
        # SuperSearch omits empty buckets: a day without crashes is zeros
        # (a row per day makes the planner converge)
        rows.append({'series_id': total_id, 'day': day,
                     'hourly': parsed.get(day, [0] * socorro.HOURS)})
        day += datetime.timedelta(days=1)
    models.upsert(models.Hourly, rows, ['series_id', 'day'])


HOURS_LAST = socorro.HOURS - 1

WRITERS = {'day': write_day, 'recent': write_recent,
           'installs': write_installs, 'daily': write_daily,
           'hourly_total': write_hourly_total}
PARSERS = {'day': socorro.parse_day, 'recent': socorro.parse_recent,
           'installs': socorro.parse_installs, 'daily': socorro.parse_daily,
           'hourly_total': socorro.parse_hourly_total}


def execute(units, fetcher, today, now=None):
    """Fetch *units* (within the fetcher's budget) and store them.

    Returns:
        (written, failed, skipped): numbers of units; skipped units were
        not attempted (budget or deadline) and stay pending.
    """
    if now is None:
        now = models.utcnow()
    units = list(units)
    if not units:
        return 0, 0, 0
    n = int(min(len(units), fetcher.remaining()))
    skipped = len(units) - n
    units = units[:n]

    def make_cb(unit):
        parser = PARSERS[unit.kind]

        def cb(json):
            unit.result = parser(json)
        return cb

    _, attempted = fetcher.run([(u.params(), make_cb(u)) for u in units])
    skipped += len(units) - attempted
    units = units[:attempted]
    written = failed = 0
    for unit in units:
        if unit.result is None:
            failed += 1
            continue
        try:
            WRITERS[unit.kind](unit, unit.result, now, today)
            db.session.commit()
            written += 1
        except Exception as ex:  # keep the run alive
            db.session.rollback()
            failed += 1
            logger.exception('Dashboard: failed to store %r: %s', unit, ex)
    return written, failed, skipped
