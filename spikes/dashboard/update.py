# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""One scheduler run: fetch what is missing, score, enrich, prune.

``python -m spikes.dashboard.update`` runs it once from the command line
(``--loop`` keeps running until the backfill is complete); the clock
process calls :func:`run` every 10 minutes.
"""

import argparse
import json
import time

import sqlalchemy as sa

from spikes import app, db
from spikes.logger import logger
from . import bugs, collect, config, events, models, scoring, versions
from .socorro import Fetcher


def lag_guard(summaries, today):
    """Suppress ``drop`` when most channels drop at once (processing lag).

    Independent products and channels do not lose volume simultaneously;
    when they do, Socorro is late.  Only the ``all`` scope is looked at:
    every ``current`` channel legitimately restarts from nothing on the
    same release day.  Returns True when the guard fired.
    """
    summaries = [s for s in summaries if config.split_channel(
        s.get('channel') or '')[1] == config.SCOPE_ALL]
    suspicious = 0
    for s in summaries:
        t = s['total']
        ratio = t.get('ratio')
        expected = t.get('expected') or 0
        z_recent = t.get('z_recent')
        if (ratio is not None and expected >= 20 and ratio < 0.7) or \
                (z_recent is not None and z_recent <= -3):
            suspicious += 1
    if suspicious < min(config.get('lag_guard_channels', 4),
                        len(summaries)):
        return False
    db.session.execute(sa.update(models.Score).where(
        models.Score.day == today, models.Score.severity == 'drop'
    ).values(severity='ok'))
    return True


def maybe_prune(today, now):
    if now.hour < 3 or models.pruned_today(today):
        return False
    removed = models.prune(today,
                           config.get('prune_after_days', 120),
                           config.get('prune_min_crashes', 3),
                           config.get('long_after_days', 365),
                           config.get('long_min_crashes', 10),
                           config.get('hourly_retention_days', 60),
                           config.get('scores_retention_days', 30),
                           config.get('runs_retention_days', 30))
    if removed:
        logger.info('Dashboard: pruned %d series left without data', removed)
    events.prune(today)
    db.session.commit()
    return True


def run(now=None, budget=None, max_seconds=None):
    """One run.  Must be called inside a Flask application context.

    Returns:
        models.Run: the finished run row.
    """
    started = time.time()
    if now is None:
        now = models.utcnow()
    today = now.date()
    models.create_all()
    run_row = models.start_run()
    if budget is None:
        budget = config.get('max_queries_per_run', 60)
    if max_seconds is None:
        max_seconds = config.get('max_run_seconds', 420)
    fetcher = Fetcher(budget=budget, deadline=started + max_seconds)
    info = {}
    try:
        try:
            refreshed = versions.maybe_refresh(now)
            if refreshed is not None:
                info['versions'] = refreshed
                db.session.commit()
        except Exception as ex:  # the all scope does not need it
            db.session.rollback()
            logger.exception('Dashboard: version calendars failed: %s', ex)
            info.setdefault('errors', []).append('versions: {}'.format(ex))
        units = collect.plan_all(today, now=now)
        written, failed, skipped = collect.execute(units, fetcher, today,
                                                   now)
        info['units'] = len(units)
        info['written'] = written
        info['failed'] = failed
        info['pending_units'] = max(0, len(units) - written)
        run_row.fetched = written
        run_row.queries = fetcher.count
        db.session.commit()  # survive per-channel rollbacks below
        # fits made while history is still being fetched must not be
        # cached for hours: they are marked stale right away
        stale_fits = info['pending_units'] > 0
        summaries = []
        pending_fits = 0
        for product, channel in config.pairs():
            try:
                s = scoring.score_channel(product, channel, today, now,
                                          stale_fits=stale_fits)
                db.session.commit()
            except Exception as ex:  # keep the other channels going
                db.session.rollback()
                logger.exception('Dashboard: scoring %s/%s failed: %s',
                                 product, channel, ex)
                info.setdefault('errors', []).append(
                    '{}/{}: {}'.format(product, channel, ex))
                continue
            if s is not None:
                summaries.append(s)
                pending_fits += s['pending_fits']
        info['pending_fits'] = pending_fits
        run_row.scored = sum(s['scored'] for s in summaries)
        run_row.lag_suspected = bool(summaries) and lag_guard(summaries,
                                                              today)
        db.session.commit()
        try:
            info['bugs'] = bugs.refresh(today, now, fetcher)
        except Exception as ex:  # keep the run alive
            db.session.rollback()
            logger.exception('Dashboard: bug look-up failed: %s', ex)
        try:
            refreshed = events.maybe_refresh(now)
            if refreshed is not None:
                info['events'] = refreshed
        except Exception as ex:  # the charts work without badges
            db.session.rollback()
            logger.exception('Dashboard: events refresh failed: %s', ex)
        run_row.pruned = maybe_prune(today, now)
        run_row.status = 'ok' if not fetcher.failures and \
            'errors' not in info else 'partial'
    except Exception as ex:  # noqa: BLE001
        db.session.rollback()
        logger.exception('Dashboard: run failed: %s', ex)
        run_row.status = 'failed'
        info['error'] = str(ex)
    run_row.queries = fetcher.count
    run_row.failures = fetcher.failures
    run_row.finished = models.utcnow()
    info['seconds'] = round(time.time() - started, 1)
    run_row.message = json.dumps(info)
    db.session.commit()
    logger.info('Dashboard: run %d %s: %s', run_row.id, run_row.status,
                run_row.message)
    return run_row


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Fetch crash counts from Socorro and score them')
    parser.add_argument('--loop', action='store_true',
                        help='keep running until nothing is pending')
    parser.add_argument('--budget', type=int, default=None,
                        help='max Socorro queries for this run')
    parser.add_argument('--max-seconds', type=int, default=None)
    args = parser.parse_args(argv)
    with app.app_context():
        while True:
            row = run(budget=args.budget, max_seconds=args.max_seconds)
            info = json.loads(row.message or '{}')
            if not args.loop or row.status == 'failed' or \
                    not (info.get('pending_units') or
                         info.get('pending_fits')):
                break


if __name__ == '__main__':
    main()
