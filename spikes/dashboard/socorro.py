# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Socorro SuperSearch query shapes, response parsers and paced execution.

Three query kinds are used (see README.md), all with ``_results_number=0``:

``day``
    ``_histogram.date=signature`` with a 1-hour interval over ONE day,
    combined with ``_aggs.signature=_cardinality.install_time`` and
    ``_aggs.product=_cardinality.install_time``: exact hourly channel
    totals and distinct installs per hour, the hourly split of every
    signature (at most a few hundred distinct signatures per hour, far
    below the 1000 cap), the exact day count + distinct installs of the top
    1000 signatures, and the channel's distinct installs for the day.
    This is the only query of the steady state.
``daily``
    ``_histogram.date=signature`` with a 1-day interval over a range of
    days: per-day count of the top 1000 signatures + exact day totals.
    Used to backfill history.
``recent``
    the ``day`` histogram over the last few hours only (a datetime range):
    the incremental refresh of the current day, every few minutes.
``installs``
    ``_aggs.signature=_cardinality.install_time`` +
    ``_aggs.product=_cardinality.install_time`` over the whole day: the
    distinct installs so far (not additive, hence refreshed on their own,
    less often).
``hourly_total``
    ``_histogram.date=product`` with a 1-hour interval over a range:
    exact hourly channel totals for the history.

The SuperSearch ``date`` field is the collector receipt time of a crash
report; the processor may index a report later, so *past* buckets can grow
for a while.  The collector re-fetches recent days until their total stops
changing (see :mod:`spikes.dashboard.collect`).

All dates are UTC ``datetime.date`` objects; ranges are half-open
``[start, end)``.
"""

import datetime
import time

import dateutil.parser
from libmozdata import socorro as lmdsocorro
from libmozdata.connection import Query
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from spikes import config as spikes_config
from spikes.gather import ADDRESS, ADDRESS_PAT
from spikes.logger import logger
from . import config


HOURS = 24
EMPTY_SIGNATURE = '(empty signature)'
KINDS = ('day', 'recent', 'installs', 'daily', 'hourly_total')


class DashboardSearch(lmdsocorro.SuperSearch):
    """SuperSearch with a bounded worker pool and retry count.

    ``Connection.__init__`` reads ``MAX_WORKERS`` from ``self`` before it
    looks at keyword arguments (so a class attribute works) but builds the
    retry policy from the *base* class attribute: libmozdata's default of
    256 retries with exponential back-off can pin a request for hours on a
    429.  The adapter is therefore re-mounted with our own policy before
    the queries are issued.
    """

    MAX_WORKERS = 3
    MAX_RETRIES = 6
    BACKOFF_MAX = 30

    def exec_queries(self, queries=None):
        retries = Retry(total=self.MAX_RETRIES, backoff_factor=1,
                        backoff_max=self.BACKOFF_MAX,
                        status_forcelist=self.STATUS_FORCELIST)
        self.session.mount(self.CRASH_STATS_URL,
                           HTTPAdapter(max_retries=retries))
        super().exec_queries(queries)


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------

def normalize_signature(signature):
    """Merge signatures that only differ by a memory address.

    ``foo | 0x1a2b`` becomes ``"foo | "0x[0-9a-fA-F]+`` which is the form
    ``spikes.gather`` uses and which SuperSearch accepts as a regex (with the
    ``@`` operator, see :func:`search_term`).
    """
    # Socorro has reports with an empty signature; the empty string is the
    # channel total's key in dashboard_series, so give them a name
    if not signature or not signature.strip():
        return EMPTY_SIGNATURE
    parts = ADDRESS_PAT.split(signature)
    if len(parts) == 1:
        return signature
    return ADDRESS.join('"{}"'.format(p) if p else '' for p in parts)


def search_term(signature):
    """The SuperSearch ``signature`` value selecting *signature*."""
    if signature.startswith('"'):
        return '@' + signature
    return '=' + signature


def noise_patterns(channel):
    """Regexes of config/skiplist.json: noise signatures (never alerted).
    *channel* may be a channel key (the scopes share the skiplist)."""
    channel, _ = config.split_channel(channel)
    return spikes_config.get_skiplist_channel(channel)


def is_noise(signature, patterns):
    return any(p.match(signature) for p in patterns)


# --------------------------------------------------------------------------
# Query parameters
# --------------------------------------------------------------------------

def date_range(start, end):
    """SuperSearch ``date`` filter for ``[start, end)`` (dates or naive UTC
    datetimes)."""
    def fmt(x):
        if isinstance(x, datetime.datetime):
            return x.replace(microsecond=0).isoformat()
        return x.isoformat()
    return ['>=' + fmt(start), '<' + fmt(end)]


def base_params(product, channel, version=None):
    """Common filters.  *channel* is a channel key (``release`` or
    ``release@current``, see config.channel_key); *version* the SuperSearch
    filter of the version cycle (``{'major_version': 155}`` or exact
    ``version`` strings, see versions.py) for the ``current`` scope."""
    release_channel, _ = config.split_channel(channel)
    params = {'product': product,
              'release_channel': release_channel,
              '_results_number': 0}
    excluded = config.get('exclude_submitted_from') or []
    if excluded:
        params['submitted_from'] = ['!' + v for v in excluded]
    if version:
        params.update(version)
    return params


def query_params(kind, product, channel, start, end, version=None):
    """Build the SuperSearch parameters of a query.

    Args:
        kind (str): one of :data:`KINDS`.
        product, channel (str): channel is a channel key.
        start, end (datetime.date): half-open range (one day for ``day``).
        version (dict): version filter of the cycle (``current`` scope).
    """
    params = base_params(product, channel, version)
    params['date'] = date_range(start, end)
    if kind == 'day':
        params['_histogram.date'] = ['signature', '_cardinality.install_time']
        params['_histogram_interval.date'] = '1h'
        params['_aggs.signature'] = '_cardinality.install_time'
        params['_aggs.product'] = '_cardinality.install_time'
        params['_facets_size'] = config.get('facets_size', 1000)
    elif kind == 'recent':
        params['_histogram.date'] = ['signature', '_cardinality.install_time']
        params['_histogram_interval.date'] = '1h'
        # distinct installs of the window, for signatures new to the day
        params['_aggs.signature'] = '_cardinality.install_time'
        params['_facets_size'] = config.get('facets_size', 1000)
    elif kind == 'installs':
        params['_aggs.signature'] = '_cardinality.install_time'
        params['_aggs.product'] = '_cardinality.install_time'
        params['_facets_size'] = config.get('facets_size', 1000)
    elif kind == 'daily':
        params['_histogram.date'] = 'signature'
        params['_histogram_interval.date'] = '1d'
        params['_facets_size'] = config.get('facets_size', 1000)
    elif kind == 'hourly_total':
        params['_histogram.date'] = 'product'
        params['_histogram_interval.date'] = '1h'
        params['_facets_size'] = 1
    else:
        raise ValueError('Unknown query kind: {}'.format(kind))
    return params


def link_params(product, channel, day, signature=None):
    """Parameters of a crash-stats search page for a day (same filters,
    including the version of the day for the ``current`` scope)."""
    from . import versions
    params = base_params(product, channel,
                         versions.params_for(product, channel, day))
    del params['_results_number']
    params['date'] = date_range(day, day + datetime.timedelta(days=1))
    if signature is not None and signature != '':
        params['signature'] = search_term(signature)
    return params


def link(product, channel, day, signature=None):
    return lmdsocorro.SuperSearch.get_link(
        link_params(product, channel, day, signature)) + '#crash-reports'


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

def parse_term(term):
    """Parse a histogram bucket key like ``2026-09-01T13:00:00Z``."""
    dt = dateutil.parser.parse(term)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _cutoff(entries, size):
    """Count of the last returned facet when the list is full, else None.

    A full list means signatures with fewer crashes may exist but were not
    returned; ``None`` means everything was returned.
    """
    if size and entries and len(entries) >= size:
        return entries[-1]['count']
    return None


# Socorro keeps one Elasticsearch index per week (Monday-based week
# number, ``strftime``'s %W) and deletes the oldest at its retention edge
INDEX_TEMPLATE = 'socorro%Y%W'


def index_for(day):
    """Name of the weekly index *day* is stored in."""
    return day.strftime(INDEX_TEMPLATE)


def is_missing_index(error):
    return isinstance(error, dict) and error.get('type') == 'missing_index'


def missing_indices(json):
    """Indices SuperSearch reported missing (deleted at the retention
    edge).  Their days have no data; the rest of the response is complete
    (Socorro skips a missing index and goes on with the others)."""
    errors = json.get('errors') if isinstance(json, dict) else None
    return {e.get('index') for e in errors or [] if is_missing_index(e)}


def check_errors(json):
    errors = json.get('errors') if isinstance(json, dict) else None
    errors = [e for e in errors or [] if not is_missing_index(e)]
    if errors:
        raise ValueError('SuperSearch errors: {}'.format(errors))
    if not isinstance(json, dict) or 'facets' not in json:
        raise ValueError('Unexpected SuperSearch response')


def parse_day(json, size=None):
    """Parse a ``day`` response (one day).

    Returns:
        dict: ``{'day', 'total', 'installs', 'hourly_total': [24],
        'hourly_installs': [24], 'cutoff', 'hours_capped',
        'signatures': {sgn: {'crashes', 'installs', 'hourly': [24]}}}``.
        Signatures are normalized (addresses merged); ``installs`` are
        distinct ``install_time`` values (None when not returned).
    """
    check_errors(json)
    if size is None:
        size = config.get('facets_size', 1000)
    hourly_total = [0] * HOURS
    hourly_installs = [0] * HOURS
    has_hourly_installs = False
    hourly = {}
    day = None
    capped = 0
    for bucket in json['facets'].get('histogram_date', []):
        dt = parse_term(bucket['term'])
        if day is None:
            day = dt.date()
        elif dt.date() != day:
            continue
        hourly_total[dt.hour] += bucket['count']
        card = bucket['facets'].get('cardinality_install_time')
        if card is not None:
            has_hourly_installs = True
            hourly_installs[dt.hour] += card.get('value', 0)
        entries = bucket['facets'].get('signature', [])
        if _cutoff(entries, size) is not None:
            capped += 1
        for entry in entries:
            sgn = normalize_signature(entry['term'])
            hours = hourly.setdefault(sgn, [0] * HOURS)
            hours[dt.hour] += entry['count']
    signatures = {}
    entries = json['facets'].get('signature', [])
    for entry in entries:
        sgn = normalize_signature(entry['term'])
        card = entry.get('facets', {}).get('cardinality_install_time', {})
        info = signatures.setdefault(sgn, {'crashes': 0, 'installs': 0,
                                           'hourly': None})
        info['crashes'] += entry['count']
        # distinct installs are not additive across address variants (one
        # machine crashing at many addresses): keep the largest, a lower
        # bound that is exact in the storm case
        info['installs'] = max(info['installs'], card.get('value', 0))
    for sgn, info in signatures.items():
        info['hourly'] = hourly.get(sgn, [0] * HOURS)
    installs = None
    for entry in json['facets'].get('product', []):
        card = entry.get('facets', {}).get('cardinality_install_time')
        if card is not None:
            installs = (installs or 0) + card.get('value', 0)
    return {'day': day,
            'total': json.get('total', sum(hourly_total)),
            'installs': installs,
            'hourly_total': hourly_total,
            'hourly_installs': hourly_installs if has_hourly_installs
            else None,
            'cutoff': _cutoff(entries, size),
            'hours_capped': capped,
            'signatures': signatures}


def parse_recent(json):
    """Parse a ``recent`` response: the hourly buckets of a time window.

    Returns:
        dict: ``day -> {'hourly_total': {hour: count},
        'hourly_installs': {hour: installs} | None,
        'signatures': {sgn: {hour: count}}, 'installs': {sgn: installs}}``
        for the hours present in the response (SuperSearch omits empty
        buckets: absent hours inside the window mean zero).  ``installs``
        are the distinct installs of the whole window per signature.
    """
    check_errors(json)
    res = {}
    window_installs = {}
    for entry in json['facets'].get('signature', []):
        card = entry.get('facets', {}).get('cardinality_install_time')
        if card is not None:
            sgn = normalize_signature(entry['term'])
            window_installs[sgn] = max(window_installs.get(sgn, 0),
                                       card.get('value', 0))
    for bucket in json['facets'].get('histogram_date', []):
        dt = parse_term(bucket['term'])
        day = res.setdefault(dt.date(), {'hourly_total': {},
                                         'hourly_installs': None,
                                         'signatures': {},
                                         'installs': window_installs})
        hour = dt.hour
        day['hourly_total'][hour] = day['hourly_total'].get(hour, 0) + \
            bucket['count']
        card = bucket['facets'].get('cardinality_install_time')
        if card is not None:
            if day['hourly_installs'] is None:
                day['hourly_installs'] = {}
            day['hourly_installs'][hour] = \
                day['hourly_installs'].get(hour, 0) + card.get('value', 0)
        for entry in bucket['facets'].get('signature', []):
            sgn = normalize_signature(entry['term'])
            hours = day['signatures'].setdefault(sgn, {})
            hours[hour] = hours.get(hour, 0) + entry['count']
    return res


def parse_installs(json, size=None):
    """Parse an ``installs`` response (one day).

    Returns:
        dict: ``{'total', 'installs', 'signatures': {sgn: (crashes,
        installs)}, 'cutoff'}``.
    """
    check_errors(json)
    if size is None:
        size = config.get('facets_size', 1000)
    entries = json['facets'].get('signature', [])
    signatures = {}
    for entry in entries:
        sgn = normalize_signature(entry['term'])
        card = entry.get('facets', {}).get('cardinality_install_time', {})
        crashes, installs = signatures.get(sgn, (0, 0))
        signatures[sgn] = (crashes + entry['count'],
                           max(installs, card.get('value', 0)))
    installs = None
    for entry in json['facets'].get('product', []):
        card = entry.get('facets', {}).get('cardinality_install_time')
        if card is not None:
            installs = (installs or 0) + card.get('value', 0)
    return {'total': json.get('total', 0), 'installs': installs,
            'signatures': signatures, 'cutoff': _cutoff(entries, size)}


def parse_daily(json, size=None):
    """Parse a ``daily`` response.

    Returns:
        dict: ``day -> {'total', 'signatures': {sgn: count}, 'cutoff'}``
    """
    check_errors(json)
    if size is None:
        size = config.get('facets_size', 1000)
    res = {}
    for bucket in json['facets'].get('histogram_date', []):
        day = parse_term(bucket['term']).date()
        entries = bucket['facets'].get('signature', [])
        sgns = {}
        for entry in entries:
            sgn = normalize_signature(entry['term'])
            sgns[sgn] = sgns.get(sgn, 0) + entry['count']
        res[day] = {'total': bucket['count'],
                    'signatures': sgns,
                    'cutoff': _cutoff(entries, size)}
    return res


def parse_hourly_total(json):
    """Parse a ``hourly_total`` response into ``day -> [24 counts]``.

    Hours without crashes are absent from the histogram; they are 0 here.
    """
    check_errors(json)
    res = {}
    for bucket in json['facets'].get('histogram_date', []):
        dt = parse_term(bucket['term'])
        hours = res.setdefault(dt.date(), [0] * HOURS)
        hours[dt.hour] += bucket['count']
    return res


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

class Fetcher:
    """Run SuperSearch queries with pacing, a query budget and a deadline.

    Handlers run in libmozdata's worker threads: they must only parse the
    response into plain Python objects (no database access).  libmozdata
    retries 429/5xx responses with back-off; on top of that this class
    bounds the in-flight requests, spaces the batches so the run never
    exceeds ``60 / min_seconds_per_query`` requests per minute, counts the
    requests (``max_queries_per_run``) and stops issuing new ones after
    ``max_run_seconds``.
    """

    def __init__(self, budget=None, max_concurrent=None, deadline=None,
                 min_interval=None):
        self.budget = budget
        if max_concurrent is None:
            max_concurrent = config.get('max_concurrent', 3)
        self.max_concurrent = max(1, int(max_concurrent))
        DashboardSearch.MAX_WORKERS = self.max_concurrent
        if min_interval is None:
            min_interval = config.get('min_seconds_per_query', 0.7)
        self.min_interval = float(min_interval)
        self.deadline = deadline
        self.timeout = config.get('query_timeout', 60)
        self.count = 0
        self.failures = 0

    def remaining(self):
        if self.deadline is not None and time.time() > self.deadline:
            return 0
        if self.budget is None:
            return float('inf')
        return max(0, self.budget - self.count)

    def can_run(self, n=1):
        return self.remaining() >= n

    def run(self, jobs):
        """Execute *jobs*, a list of ``(params, callback)``.

        ``callback(json)`` receives the parsed response of each successful
        query.  Failures are logged and counted; the other queries still
        run.  Jobs beyond the budget/deadline are skipped.

        Returns:
            (int, int): number of successful queries and number of jobs
            attempted (the others were skipped for budget or deadline).
        """
        jobs = list(jobs)
        ok = attempted = 0
        for i in range(0, len(jobs), self.max_concurrent):
            if not self.can_run():
                logger.info('Dashboard: query budget exhausted, %d jobs left',
                            len(jobs) - i)
                break
            batch = jobs[i:i + self.max_concurrent]
            batch = batch[:int(min(len(batch), self.remaining()))]
            started = time.time()
            ok += self._run_batch(batch)
            attempted += len(batch)
            if len(batch) < self.max_concurrent:
                break
            pause = self.min_interval * len(batch) - (time.time() - started)
            if pause > 0 and i + self.max_concurrent < len(jobs):
                time.sleep(pause)
        return ok, attempted

    def _run_batch(self, batch):
        status = {}

        def make_handler(idx, callback):
            def handler(json):
                try:
                    callback(json)
                    status[idx] = True
                except Exception as ex:  # keep the run alive
                    logger.warning('Dashboard: bad response: %s', ex)
                    status[idx] = False
            return handler

        queries = [Query(DashboardSearch.URL, params, make_handler(i, cb))
                   for i, (params, cb) in enumerate(batch)]
        started = time.time()
        conn = DashboardSearch(queries=queries, timeout=self.timeout)
        for future in conn.results:
            try:
                future.result()
            except Exception as ex:  # keep the run alive
                logger.warning('Dashboard: query failed: %s', ex)
        self.count += len(batch)
        ok = sum(1 for v in status.values() if v)
        self.failures += len(batch) - ok
        logger.info('Dashboard: %d/%d queries ok in %.1fs',
                    ok, len(batch), time.time() - started)
        return ok
