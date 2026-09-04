# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Database tables of the dashboard.

All tables are prefixed with ``dashboard_``.  Only portable column types
are used so the tests can run against the in-memory SQLite database that
``spikes`` falls back to when ``DATABASE_URL`` is not set.

The per-(product, channel) total is stored as a regular series whose
signature is the empty string (:data:`TOTAL`), so every model/score row has
a non-NULL series id and the same code path scores totals and signatures.
"""

import datetime

import sqlalchemy as sa

from spikes import db


TOTAL = ''
CHUNK = 500


def utcnow():
    """Naive UTC ``datetime`` (all timestamps are stored naive, in UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def utctoday():
    return utcnow().date()


class Series(db.Model):
    __tablename__ = 'dashboard_series'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    product = db.Column(db.String(32), nullable=False)
    channel = db.Column(db.String(32), nullable=False)
    signature = db.Column(db.Text, nullable=False)
    first_seen = db.Column(db.Date)
    last_seen = db.Column(db.Date)
    # matches config/skiplist.json: displayed, never alerted on
    noise = db.Column(db.Boolean, nullable=False, default=False)
    bug_open = db.Column(db.Integer)
    bug_closed = db.Column(db.Integer)
    bugs_as_of = db.Column(db.DateTime)

    __table_args__ = (
        sa.UniqueConstraint('product', 'channel', 'signature',
                            name='uq_dashboard_series'),
        sa.Index('ix_dashboard_series_seen', 'product', 'channel',
                 'last_seen'),
    )

    @property
    def is_total(self):
        return self.signature == TOTAL


class Daily(db.Model):
    """One row per (series, day): crashes and distinct installs."""
    __tablename__ = 'dashboard_daily'

    series_id = db.Column(db.Integer,
                          db.ForeignKey('dashboard_series.id',
                                        ondelete='CASCADE'),
                          primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    crashes = db.Column(db.Integer, nullable=False, default=0)
    installs = db.Column(db.Integer)
    # the crash count seen by the query that produced `installs` (the two
    # are fetched at the same instant; `crashes` may be fresher)
    installs_crashes = db.Column(db.Integer)

    __table_args__ = (sa.Index('ix_dashboard_daily_day', 'day'),)


class Hourly(db.Model):
    """One row per (series, day): 24 crash counts per UTC hour."""
    __tablename__ = 'dashboard_hourly'

    series_id = db.Column(db.Integer,
                          db.ForeignKey('dashboard_series.id',
                                        ondelete='CASCADE'),
                          primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    hourly = db.Column(db.JSON, nullable=False)
    # distinct installs per hour (channel totals only)
    installs = db.Column(db.JSON)

    __table_args__ = (sa.Index('ix_dashboard_hourly_day', 'day'),)


class Day(db.Model):
    """One row per (product, channel, day): fetch bookkeeping."""
    __tablename__ = 'dashboard_days'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    product = db.Column(db.String(32), nullable=False)
    channel = db.Column(db.String(32), nullable=False)
    day = db.Column(db.Date, nullable=False)
    crashes = db.Column(db.Integer)
    # total at the previous fetch: a day is final once it stops changing
    prev_crashes = db.Column(db.Integer)
    # count of the last signature returned: smaller ones may be missing
    cutoff = db.Column(db.Integer)
    as_of = db.Column(db.DateTime)
    # when the distinct-install counts of the day were last refreshed
    installs_as_of = db.Column(db.DateTime)
    final = db.Column(db.Boolean, nullable=False, default=False)
    # fetched with the merged per-day query (hourly split + installs)
    complete = db.Column(db.Boolean, nullable=False, default=False)
    hours_capped = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        sa.UniqueConstraint('product', 'channel', 'day',
                            name='uq_dashboard_days'),
    )


class Model(db.Model):
    """Cached seasonal fit of a series (refreshed every few hours)."""
    __tablename__ = 'dashboard_models'

    series_id = db.Column(db.Integer,
                          db.ForeignKey('dashboard_series.id',
                                        ondelete='CASCADE'),
                          primary_key=True)
    fitted_at = db.Column(db.DateTime, nullable=False)
    # last day included in the fit
    last_day = db.Column(db.Date)
    history_days = db.Column(db.Integer, nullable=False, default=0)
    level = db.Column(db.Float, nullable=False, default=0.0)
    trend = db.Column(db.Float, nullable=False, default=0.0)
    dispersion = db.Column(db.Float, nullable=False, default=1.0)
    c2 = db.Column(db.Float, nullable=False, default=0.0)
    install_share = db.Column(db.Float)
    factors = db.Column(db.JSON)
    borrowed = db.Column(db.JSON)
    components = db.Column(db.JSON)
    level_change_28 = db.Column(db.Float)


class Score(db.Model):
    """Latest score of a series for a day (today and yesterday)."""
    __tablename__ = 'dashboard_scores'

    series_id = db.Column(db.Integer,
                          db.ForeignKey('dashboard_series.id',
                                        ondelete='CASCADE'),
                          primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    as_of = db.Column(db.DateTime, nullable=False)
    partial = db.Column(db.Boolean, nullable=False, default=True)
    elapsed = db.Column(db.Float)
    observed = db.Column(db.Integer, nullable=False, default=0)
    installs = db.Column(db.Integer)
    expected_installs = db.Column(db.Float)
    z_installs = db.Column(db.Float)
    expected_day = db.Column(db.Float)
    expected = db.Column(db.Float)
    z = db.Column(db.Float)
    ratio = db.Column(db.Float)
    excess = db.Column(db.Float)
    projected = db.Column(db.Float)
    projected_lo = db.Column(db.Float)
    projected_hi = db.Column(db.Float)
    recent_hours = db.Column(db.Integer)
    observed_recent = db.Column(db.Integer)
    expected_recent = db.Column(db.Float)
    z_recent = db.Column(db.Float)
    recent_reason = db.Column(db.String(64))
    severity = db.Column(db.String(8), nullable=False, default='ok')
    is_new = db.Column(db.Boolean, nullable=False, default=False)
    storm = db.Column(db.Boolean, nullable=False, default=False)
    first_flagged_at = db.Column(db.DateTime)
    # last run in which the live severity was flagged (the page keeps a
    # past day's flag visible for `flag_window_hours` after it)
    last_flagged_at = db.Column(db.DateTime)
    peak_severity = db.Column(db.String(8))
    peak_z = db.Column(db.Float)
    peak_excess = db.Column(db.Float)
    peak_at = db.Column(db.DateTime)
    details = db.Column(db.JSON)

    __table_args__ = (sa.Index('ix_dashboard_scores_day', 'day'),)


class Run(db.Model):
    __tablename__ = 'dashboard_runs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    started = db.Column(db.DateTime, nullable=False)
    finished = db.Column(db.DateTime)
    status = db.Column(db.String(16), nullable=False, default='running')
    queries = db.Column(db.Integer, nullable=False, default=0)
    failures = db.Column(db.Integer, nullable=False, default=0)
    fetched = db.Column(db.Integer, nullable=False, default=0)
    scored = db.Column(db.Integer, nullable=False, default=0)
    pruned = db.Column(db.Boolean, nullable=False, default=False)
    lag_suspected = db.Column(db.Boolean, nullable=False, default=False)
    message = db.Column(db.Text)


class Event(db.Model):
    """A platform event shown as a badge on the charts (a Windows update,
    an NVIDIA driver, a macOS release...), see ``events.py``."""
    __tablename__ = 'dashboard_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source = db.Column(db.String(16), nullable=False)   # windows, nvidia...
    kind = db.Column(db.String(24), nullable=False)     # windows-update...
    ref = db.Column(db.String(64), nullable=False)      # KB5120998/26200.9278
    day = db.Column(db.Date, nullable=False)
    at = db.Column(db.DateTime)                          # when known
    title = db.Column(db.String(160), nullable=False)
    detail = db.Column(db.String(400))
    url = db.Column(db.String(400))
    search = db.Column(db.String(400))                   # crash-stats link
    updated_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint('kind', 'ref', name='uq_dashboard_events_ref'),
        sa.Index('ix_dashboard_events_day', 'day'),
    )


class Feed(db.Model):
    """Fetch bookkeeping of one event feed."""
    __tablename__ = 'dashboard_feeds'

    name = db.Column(db.String(32), primary_key=True)
    fetched_at = db.Column(db.DateTime, nullable=False)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    items = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.String(200))


class Mark(db.Model):
    """A signed-in user's mark on a series: "done", the spike is handled.

    Every change is a new row (the latest per series wins), so the table
    is also the audit trail, and its highest id versions the API responses
    (a mark changes what the page shows without a scheduler run).
    """
    __tablename__ = 'dashboard_marks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    series_id = db.Column(db.Integer,
                          db.ForeignKey('dashboard_series.id',
                                        ondelete='CASCADE'),
                          nullable=False, index=True)
    done = db.Column(db.Boolean, nullable=False, default=True)
    # the severity the row was flagged with when marked ('ok' for a row
    # that was only new): a mark does not cover a later, higher severity
    severity = db.Column(db.String(8), nullable=False, default='ok')
    by = db.Column(db.String(255), nullable=False)
    at = db.Column(db.DateTime, nullable=False)


TABLES = [Series.__table__, Daily.__table__, Hourly.__table__,
          Day.__table__, Model.__table__, Score.__table__, Run.__table__,
          Event.__table__, Feed.__table__, Mark.__table__]


def create_all():
    """Create the dashboard tables if they do not exist and add columns
    that were added to the models since (additive schema evolution, so a
    deploy with a new column does not need a manual ``ALTER TABLE``)."""
    db.metadata.create_all(bind=db.engine, tables=TABLES, checkfirst=True)
    inspector = sa.inspect(db.engine)
    for table in TABLES:
        existing = {c['name'] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = sa.schema.CreateColumn(column).compile(
                dialect=db.engine.dialect)
            ddl = str(ddl)
            if not column.nullable and column.server_default is None:
                default = column.default.arg if column.default is not None \
                    else None
                if isinstance(default, bool):
                    ddl += ' DEFAULT {}'.format('true' if default
                                                else 'false')
                elif isinstance(default, (int, float)):
                    ddl += ' DEFAULT {}'.format(default)
                elif isinstance(default, str):
                    ddl += " DEFAULT '{}'".format(default.replace("'",
                                                                  "''"))
                else:
                    ddl = ddl.replace(' NOT NULL', '')
            db.session.execute(sa.text('ALTER TABLE {} ADD COLUMN {}'.format(
                table.name, ddl)))
    db.session.commit()


def drop_all():
    db.metadata.drop_all(bind=db.engine, tables=TABLES, checkfirst=True)


def _chunks(items, size=CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def upsert(model, rows, keys, ignore_conflicts=False):
    """Insert *rows* into *model*, updating the non-key columns on conflict
    (or leaving the existing row alone with ``ignore_conflicts``).

    Uses ``ON CONFLICT`` on PostgreSQL and SQLite.  Rows may have different
    key sets: they are grouped so every statement is homogeneous.
    """
    rows = [r for r in rows if r]
    if not rows:
        return
    dialect = db.engine.dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError('Unsupported database: {}'.format(dialect))
    groups = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    for cols, group in groups.items():
        update_cols = [] if ignore_conflicts else \
            [c for c in cols if c not in keys]
        for chunk in _chunks(group, 200):
            stmt = insert(model.__table__).values(chunk)
            if update_cols:
                # subscript, not getattr: a column may be named like a
                # method of the collection (`items`)
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(keys),
                    set_={c: stmt.excluded[c] for c in update_cols})
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
            db.session.execute(stmt)


# --------------------------------------------------------------------------
# Series
# --------------------------------------------------------------------------

def series_ids(product, channel, signatures, create=True, noise=None):
    """Map signatures to series ids, creating the missing ones.

    Args:
        noise (callable): ``signature -> bool`` used for new series.

    Returns:
        dict: ``signature -> id``
    """
    signatures = set(signatures)
    res = {}
    for chunk in _chunks(signatures):
        q = sa.select(Series.signature, Series.id).where(
            Series.product == product, Series.channel == channel,
            Series.signature.in_(chunk))
        res.update(dict(db.session.execute(q).all()))
    missing = signatures - set(res)
    if missing and create:
        # another process (the clock job next to a one-off backfill) may
        # create the same series concurrently: ignore the conflict
        upsert(Series, [
            {'product': product, 'channel': channel, 'signature': s,
             'noise': bool(noise(s)) if noise and s != TOTAL else False}
            for s in sorted(missing)],
            ['product', 'channel', 'signature'], ignore_conflicts=True)
        for chunk in _chunks(missing):
            q = sa.select(Series.signature, Series.id).where(
                Series.product == product, Series.channel == channel,
                Series.signature.in_(chunk))
            res.update(dict(db.session.execute(q).all()))
    return res


def total_series(product, channel, create=True):
    """Id of the series holding the (product, channel) total (None when
    it does not exist and ``create`` is False)."""
    return series_ids(product, channel, [TOTAL], create=create).get(TOTAL)


def hourly_days(series_id, since):
    """Days for which *series_id* has an hourly split."""
    q = sa.select(Hourly.day).where(Hourly.series_id == series_id,
                                    Hourly.day >= since)
    return [d for (d,) in db.session.execute(q)]


def get_series(product, channel, signature):
    return db.session.execute(sa.select(Series).where(
        Series.product == product, Series.channel == channel,
        Series.signature == signature)).scalar_one_or_none()


def load_series(ids):
    """``id -> Series`` for *ids*."""
    res = {}
    for chunk in _chunks(ids):
        for s in db.session.execute(
                sa.select(Series).where(Series.id.in_(chunk))).scalars():
            res[s.id] = s
    return res


def channel_series(product, channel, since=None):
    """All series of a channel (optionally seen since *since*)."""
    q = sa.select(Series).where(Series.product == product,
                                Series.channel == channel)
    if since is not None:
        q = q.where(Series.last_seen >= since)
    return list(db.session.execute(q).scalars())


def update_seen(ids, day):
    """Extend ``first_seen``/``last_seen`` of *ids* to include *day*."""
    for chunk in _chunks(ids):
        db.session.execute(sa.update(Series).where(
            Series.id.in_(chunk),
            sa.or_(Series.last_seen.is_(None), Series.last_seen < day)
        ).values(last_seen=day))
        db.session.execute(sa.update(Series).where(
            Series.id.in_(chunk),
            sa.or_(Series.first_seen.is_(None), Series.first_seen > day)
        ).values(first_seen=day))


# --------------------------------------------------------------------------
# Counts
# --------------------------------------------------------------------------

def load_daily(series_ids, since, until=None):
    """``series_id -> {day: (crashes, installs, installs_crashes)}`` in
    ``[since, until]`` (the tuple's first two items are what most callers
    use)."""
    res = {}
    for chunk in _chunks(series_ids):
        q = sa.select(Daily.series_id, Daily.day, Daily.crashes,
                      Daily.installs, Daily.installs_crashes).where(
            Daily.series_id.in_(chunk), Daily.day >= since)
        if until is not None:
            q = q.where(Daily.day <= until)
        for sid, day, crashes, installs, ic in db.session.execute(q):
            res.setdefault(sid, {})[day] = (crashes, installs, ic)
    return res


def load_hourly(series_ids, days, installs=False):
    """``series_id -> {day: [24 ints]}`` for *days* (crashes, or distinct
    installs per hour with ``installs=True``)."""
    res = {}
    days = list(days)
    col = Hourly.installs if installs else Hourly.hourly
    for chunk in _chunks(series_ids):
        q = sa.select(Hourly.series_id, Hourly.day, col).where(
            Hourly.series_id.in_(chunk), Hourly.day.in_(days))
        for sid, day, hourly in db.session.execute(q):
            if hourly is not None:
                res.setdefault(sid, {})[day] = hourly
    return res


def channel_hourly(product, channel, day):
    """``series_id -> [24 ints]`` for every series of a channel on *day*."""
    q = sa.select(Hourly.series_id, Hourly.hourly).join(
        Series, Series.id == Hourly.series_id).where(
        Series.product == product, Series.channel == channel,
        Hourly.day == day)
    return {sid: list(h) for sid, h in db.session.execute(q)}


def channel_daily(product, channel, days):
    """``series_id -> {day: (crashes, installs)}`` for all series of a
    channel on *days* (used to find what to score)."""
    q = sa.select(Daily.series_id, Daily.day, Daily.crashes,
                  Daily.installs, Daily.installs_crashes).join(
        Series, Series.id == Daily.series_id).where(
        Series.product == product, Series.channel == channel,
        Daily.day.in_(list(days)))
    res = {}
    for sid, day, crashes, installs, ic in db.session.execute(q):
        res.setdefault(sid, {})[day] = (crashes, installs, ic)
    return res


def recent_max(product, channel, since):
    """``series_id -> max daily crashes since *since*`` for a channel."""
    q = sa.select(Daily.series_id, sa.func.max(Daily.crashes)).join(
        Series, Series.id == Daily.series_id).where(
        Series.product == product, Series.channel == channel,
        Daily.day >= since).group_by(Daily.series_id)
    return dict(db.session.execute(q).all())


# --------------------------------------------------------------------------
# Days (fetch bookkeeping)
# --------------------------------------------------------------------------

def get_day(product, channel, day):
    return db.session.execute(sa.select(Day).where(
        Day.product == product, Day.channel == channel,
        Day.day == day)).scalar_one_or_none()


def upsert_day(product, channel, day, **fields):
    row = get_day(product, channel, day)
    if row is None:
        row = Day(product=product, channel=channel, day=day)
        db.session.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    return row


def load_days(product, channel, since, until=None):
    q = sa.select(Day).where(Day.product == product, Day.channel == channel,
                             Day.day >= since)
    if until is not None:
        q = q.where(Day.day <= until)
    return list(db.session.execute(q.order_by(Day.day)).scalars())


# --------------------------------------------------------------------------
# Models and scores
# --------------------------------------------------------------------------

def load_models(series_ids):
    res = {}
    for chunk in _chunks(series_ids):
        for m in db.session.execute(
                sa.select(Model).where(Model.series_id.in_(chunk))).scalars():
            res[m.series_id] = m
    return res


def load_scores(product, channel, days):
    """``list[(Score, Series)]`` of a channel for *days*."""
    q = sa.select(Score, Series).join(
        Series, Series.id == Score.series_id).where(
        Series.product == product, Series.channel == channel,
        Score.day.in_(list(days)))
    return list(db.session.execute(q).all())


def load_series_scores(series_id, since):
    q = sa.select(Score).where(Score.series_id == series_id,
                               Score.day >= since).order_by(Score.day)
    return list(db.session.execute(q).scalars())


def flagged_scores(days, severities):
    """``list[(Score, Series)]`` across channels with a flagged severity."""
    q = sa.select(Score, Series).join(
        Series, Series.id == Score.series_id).where(
        Score.day.in_(list(days)),
        sa.or_(Score.severity.in_(list(severities)),
               Score.is_new.is_(True)),
        Series.signature != TOTAL)
    return list(db.session.execute(q).all())


# --------------------------------------------------------------------------
# Runs and housekeeping
# --------------------------------------------------------------------------

def start_run():
    """Create a run row; runs left 'running' for too long become aborted."""
    stale = utcnow() - datetime.timedelta(minutes=15)
    db.session.execute(sa.update(Run).where(
        Run.status == 'running', Run.started < stale).values(
        status='aborted', finished=utcnow()))
    run = Run(started=utcnow())
    db.session.add(run)
    db.session.commit()
    return run


def last_runs(n=5, status=None, finished_only=False):
    q = sa.select(Run).order_by(Run.started.desc()).limit(n)
    if status is not None:
        q = q.where(Run.status == status)
    if finished_only:
        q = q.where(Run.status.not_in(('running', 'aborted')))
    return list(db.session.execute(q).scalars())


def pruned_today(today):
    start = datetime.datetime(today.year, today.month, today.day)
    q = sa.select(sa.func.count(Run.id)).where(Run.started >= start,
                                               Run.pruned.is_(True))
    return db.session.execute(q).scalar_one() > 0


def prune(today, prune_after_days, prune_min_crashes, long_after_days,
          long_min_crashes, hourly_retention_days, scores_retention_days,
          runs_retention_days):
    """Retention: drop old low-volume rows, old hourly splits, old scores."""
    total_ids = sa.select(Series.id).where(Series.signature == TOTAL)
    old = today - datetime.timedelta(days=prune_after_days)
    db.session.execute(sa.delete(Daily).where(
        Daily.day < old, Daily.crashes < prune_min_crashes,
        Daily.series_id.not_in(total_ids)))
    old = today - datetime.timedelta(days=long_after_days)
    db.session.execute(sa.delete(Daily).where(
        Daily.day < old, Daily.crashes < long_min_crashes,
        Daily.series_id.not_in(total_ids)))
    old = today - datetime.timedelta(days=hourly_retention_days)
    db.session.execute(sa.delete(Hourly).where(
        Hourly.day < old, Hourly.series_id.not_in(total_ids)))
    old = today - datetime.timedelta(days=scores_retention_days)
    db.session.execute(sa.delete(Score).where(Score.day < old))
    old = utcnow() - datetime.timedelta(days=runs_retention_days)
    db.session.execute(sa.delete(Run).where(Run.started < old))
    # series left without any data (a signature seen a few times half a
    # year ago, its daily rows pruned above) and their cached fits would
    # otherwise stay forever, one row per channel key; marks are kept
    empty = sa.select(Series.id).where(
        Series.signature != TOTAL,
        ~sa.exists().where(Daily.series_id == Series.id),
        ~sa.exists().where(Hourly.series_id == Series.id),
        ~sa.exists().where(Score.series_id == Series.id),
        ~sa.exists().where(Mark.series_id == Series.id))
    ids = list(db.session.execute(empty).scalars())
    for chunk in _chunks(ids):
        db.session.execute(sa.delete(Model).where(Model.series_id.in_(chunk)))
        db.session.execute(sa.delete(Series).where(Series.id.in_(chunk)))
    return len(ids)


# --------------------------------------------------------------------------
# Platform events
# --------------------------------------------------------------------------

def load_events(since, until=None):
    q = sa.select(Event).where(Event.day >= since)
    if until is not None:
        q = q.where(Event.day <= until)
    return list(db.session.execute(
        q.order_by(Event.day, Event.source, Event.title)).scalars())


def events_version():
    """``(count, latest update)`` of the events: the ETag of the events
    endpoint changes only when a refresh wrote something."""
    n, latest = db.session.execute(sa.select(
        sa.func.count(Event.id), sa.func.max(Event.updated_at))).one()
    return int(n or 0), latest


def load_feeds():
    return {f.name: f for f in db.session.execute(sa.select(Feed)).scalars()}


def prune_events(before):
    db.session.execute(sa.delete(Event).where(Event.day < before))


# --------------------------------------------------------------------------
# Marks (done)
# --------------------------------------------------------------------------

def add_mark(series_id, done, severity, by, at=None):
    mark = Mark(series_id=series_id, done=bool(done), severity=severity,
                by=by, at=at or utcnow())
    db.session.add(mark)
    db.session.commit()
    return mark


def load_marks(series_ids):
    """``series_id -> latest Mark`` for the series that have one."""
    res = {}
    ids = list(series_ids)
    for chunk in _chunks(ids):
        latest = sa.select(sa.func.max(Mark.id)).where(
            Mark.series_id.in_(chunk)).group_by(Mark.series_id)
        for m in db.session.execute(
                sa.select(Mark).where(Mark.id.in_(latest))).scalars():
            res[m.series_id] = m
    return res


def marks_version():
    """Highest mark id (0 when none): part of the API data version."""
    return int(db.session.execute(sa.select(sa.func.max(Mark.id))).scalar()
               or 0)
