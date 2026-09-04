# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Bugs filed for the flagged signatures.

Per signature (shared by every product, channel and scope), the bugs whose
*Crash Signature* field lists it, with when they were filed and their
status, so a row can say whether a bug was filed *after* its spike started
(someone is on it) or only before it (a known crash, spiking again).

Sources, cheapest first:

1. Socorro's ``Bugs`` API: the bug ids Socorro extracted from Bugzilla's
   crash-signature fields (synced every hour), many signatures per query.
2. When Socorro knows no bug for a signature, a Bugzilla search on the
   crash-signature field (``substring``), several signatures OR-ed in one
   query.  Only bugs whose field lists the signature exactly (``[@ ... ]``)
   count: a substring match alone would attach other crashes' bugs.
3. Bugzilla for the details of the ids of 1 (filed when, status, summary),
   100 per query; 2 returns them with the search.

Signatures are looked up while flagged, at most ``bugs_max_signatures`` per
run and again ``bugs_refresh_hours`` after the previous look-up, so a bug
filed for a spike shows up within a couple of hours and idle signatures
cost nothing.  A failed query leaves its signatures unchecked: they are
looked up again next run.
"""

import datetime
import re
import time
import urllib.parse

from libmozdata import socorro as lmdsocorro
from libmozdata.bugzilla import Bugzilla

from spikes import db
from spikes.logger import logger
from . import config, models, scoring


# a longer Bugs URL is refused with 400: characters of signatures per query
SOCORRO_URL_CHARS = 2000
SEARCH_BATCH = 8            # signatures OR-ed in one Bugzilla search
# newest first: an old, generic signature can have hundreds of bugs
SEARCH_LIMIT = 200
FIELDS = ['id', 'status', 'resolution', 'creation_time', 'summary']
SUMMARY_CHARS = 200
FLAGGED = ('major', 'spike', 'watch', 'drop')
_SIGNATURE = re.compile(r'\[@\s*(.*?)\s*\]', re.S)


def normalize(signature):
    return ' '.join((signature or '').split())


def signatures_of(field):
    """Signatures listed in a bug's crash-signature field (``[@ sig ]``
    entries, whitespace collapsed)."""
    return [normalize(s) for s in _SIGNATURE.findall(field or '')]


def chunks_by_chars(signatures, chars=SOCORRO_URL_CHARS):
    """Batches of signatures whose URL-encoded length stays under *chars*
    (a batch always holds at least one)."""
    batch, size = [], 0
    for s in signatures:
        n = len(urllib.parse.quote(s)) + len('&signatures=')
        if batch and size + n > chars:
            yield batch
            batch, size = [], 0
        batch.append(s)
        size += n
    if batch:
        yield batch


def parse_time(value):
    """``2026-09-04T02:09:30Z`` -> naive UTC datetime (None when absent)."""
    if not value:
        return None
    return datetime.datetime.strptime(str(value)[:19], '%Y-%m-%dT%H:%M:%S')


def details_of(bug):
    return {'created_at': parse_time(bug.get('creation_time')),
            'status': bug.get('status'),
            'resolution': bug.get('resolution') or None,
            'summary': (bug.get('summary') or '')[:SUMMARY_CHARS]}


def attach(bug, wanted, found):
    """Record *bug* (a Bugzilla search hit) for every wanted signature its
    crash-signature field lists exactly.  *wanted* maps the normalized
    signatures to the requested ones."""
    for listed in signatures_of(bug.get('cf_crash_signature')):
        s = wanted.get(listed)
        if s is not None:
            found.setdefault(s, {})[int(bug['id'])] = details_of(bug)


def search_query(signatures):
    """Query string of one Bugzilla search for *signatures*."""
    params = [('j_top', 'OR')]
    for i, s in enumerate(signatures, 1):
        params += [('f%d' % i, 'cf_crash_signature'), ('o%d' % i, 'substring'),
                   ('v%d' % i, s)]
    params += [('include_fields', ','.join(FIELDS + ['cf_crash_signature'])),
               ('order', 'bug_id DESC'), ('limit', SEARCH_LIMIT)]
    return urllib.parse.urlencode(params)


class Counter:
    """Stands in for the run's Fetcher (queries, failures, deadline)."""

    def __init__(self):
        self.count = 0
        self.failures = 0
        self.deadline = None


def past_deadline(fetcher):
    return fetcher.deadline is not None and time.time() > fetcher.deadline


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def fetch_socorro(signatures, fetcher):
    """``signature -> set(bug ids)`` from Socorro's Bugs API.  Signatures
    of a failed query are absent (unknown, not "no bug")."""
    found = {s: set() for s in signatures}

    def handler(json, data):
        for hit in json.get('hits', []):
            if hit.get('signature') in data:
                data[hit['signature']].add(int(hit['id']))

    for chunk in chunks_by_chars(signatures):
        try:
            lmdsocorro.Bugs(params={'signatures': chunk}, handler=handler,
                            handlerdata=found).wait()
        except Exception as ex:  # keep the run alive
            logger.warning('Dashboard: Socorro bugs query failed: %s', ex)
            fetcher.failures += 1
            for s in chunk:
                found.pop(s, None)
        fetcher.count += 1
    return found


def search_bugzilla(signatures, fetcher):
    """``signature -> {bug id: details}`` from Bugzilla searches on the
    crash-signature field (an empty dict: searched, nothing listed).
    Signatures of a failed search are absent."""
    found = {}
    wanted = {normalize(s): s for s in signatures}

    def handler(bug, data):
        attach(bug, wanted, data)

    for chunk in models._chunks(signatures, SEARCH_BATCH):
        try:
            Bugzilla(bugids=search_query(chunk), bughandler=handler,
                     bugdata=found).wait()
        except Exception as ex:  # keep the run alive
            logger.warning('Dashboard: Bugzilla search failed: %s', ex)
            fetcher.failures += 1
            continue
        fetcher.count += 1
        for s in chunk:
            found.setdefault(s, {})
    return found


def fetch_details(bug_ids, fetcher):
    """``bug id -> details`` from Bugzilla (100 ids per query); None when
    the query failed.  A bug Bugzilla hides (security) is absent."""
    found = {}
    ids = sorted(set(bug_ids))
    if not ids:
        return found

    def handler(bug, data):
        data[int(bug['id'])] = details_of(bug)

    try:
        Bugzilla(bugids=ids, include_fields=FIELDS, bughandler=handler,
                 bugdata=found).wait()
    except Exception as ex:  # keep the run alive
        logger.warning('Dashboard: Bugzilla query failed: %s', ex)
        fetcher.failures += 1
        return None
    fetcher.count += (len(ids) + 99) // 100
    return found


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

def flagged_days(today):
    """Today and the previous days whose flags the page still shows
    (``flag_window_hours``, see api.flag_of)."""
    back = -(-config.get('flag_window_hours', 48) // 24)
    return [today - datetime.timedelta(days=k) for k in range(back + 1)]


def due_signatures(today, now):
    """Signatures shown flagged (not noise) whose look-up is due, the never
    looked-up and the oldest first, at most ``bugs_max_signatures``.  A
    flag can come from a previous day within the flag window (a spike on
    Tuesday is listed until Thursday), so those days count too."""
    hours = config.get('bugs_refresh_hours', 2)
    limit = config.get('bugs_max_signatures', 150)
    rows = models.flagged_scores(flagged_days(today), FLAGGED,
                                 peaks=scoring.UPWARD)
    signatures = sorted({series.signature for _, series in rows
                         if not series.noise})
    if not signatures:
        return []
    checks = models.load_bug_checks(signatures)
    cutoff = now - datetime.timedelta(hours=hours)
    never = datetime.datetime.min
    due = [s for s in signatures
           if s not in checks or checks[s].checked_at <= cutoff]
    due.sort(key=lambda s: (checks[s].checked_at if s in checks else never,
                            s))
    return due[:limit]


def refresh(today, now, fetcher=None):
    """Look up the bugs of the flagged signatures that are due and store
    them.  Returns the number of signatures looked up (None when the
    run's deadline cut it short)."""
    fetcher = fetcher or Counter()
    todo = due_signatures(today, now)
    if not todo:
        return 0
    if past_deadline(fetcher):
        return None
    by_socorro = fetch_socorro(todo, fetcher)
    if past_deadline(fetcher):
        return None
    without = [s for s in todo if s in by_socorro and not by_socorro[s]]
    searched = search_bugzilla(without, fetcher) if without else {}
    ids = {b for bugs in by_socorro.values() for b in bugs}
    details = fetch_details(ids, fetcher) if ids else {}
    if details is None:
        # no details this run: keep the Socorro ids for the next one
        by_socorro = {s: bugs for s, bugs in by_socorro.items() if not bugs}
        details = {}
    checked = {}
    for s in todo:
        if s not in by_socorro:
            continue
        bugs = {b: dict(details.get(b, {}), source='socorro')
                for b in by_socorro[s]}
        if not bugs:
            if s not in searched:
                continue
            bugs = {b: dict(d, source='bugzilla')
                    for b, d in searched[s].items()}
        models.replace_bugs(s, bugs, now)
        checked[s] = len(bugs)
    models.mark_bugs_checked(checked, now)
    db.session.commit()
    return len(checked)
