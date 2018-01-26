# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from dateutil.relativedelta import relativedelta
from spikes import app, db, tools
from spikes import utils as sputils
from spikes import datacollector as dc
from sqlalchemy import distinct
import sqlalchemy.dialects.postgresql as pg
from .logger import logger


NDAYS = 11
NSGNS = 100
NDAYS_OF_DATA = 4


class Signatures(db.Model):
    __tablename__ = 'signatures'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pc = db.Column(db.String(3))
    date = db.Column(db.Date)
    numbers = db.Column(pg.ARRAY(db.Integer))
    exp1 = db.Column(db.Float)
    exp3 = db.Column(db.Float)
    signature = db.Column(db.String(512))
    version = db.Column(db.String(196))
    bug_o = db.Column(db.Integer, default=0)
    bug_c = db.Column(db.Integer, default=0)

    def __init__(self, product, channel, version,
                 date, signature, numbers, bug_o, bug_c):
        self.pc = Signatures.get_pc(product, channel)
        self.version = sputils.get_versions_str(version)
        self.date = sputils.get_date(date)
        self.signature = signature
        self.numbers = numbers
        self.exp1 = tools.explosiveness(numbers, 1, 7)
        self.exp3 = tools.explosiveness(numbers, 3, 7)
        self.bug_o = bug_o
        self.bug_c = bug_c

    def __repr__(self):
        s = '<Signature id: {}, pc: {}, date: {}, sgn: {}, num: {}, ver: {}, b_o: {}, b_c: {}>' # NOQA
        return s.format(self.id,
                        self.pc,
                        self.date,
                        self.signature,
                        self.numbers,
                        self.version,
                        self.bug_o,
                        self.bug_c)

    @staticmethod
    def get_pc(product, channel):
        return product[:2] + channel[0].upper()

    @staticmethod
    def rm(date):
        date = sputils.get_date(date)
        date -= relativedelta(days=NDAYS_OF_DATA)
        q = db.session.query(Signatures).filter_by(date=date)
        if q:
            q.delete()
            db.session.commit()

    @staticmethod
    def put(product, channel, version, date,
            signature, numbers, bug_o, bug_c, commit=True):
        c = Signatures(product, channel, version, date,
                       signature, numbers, bug_o, bug_c)
        db.session.add(c)
        if commit:
            db.session.commit()

        return c

    @staticmethod
    def put_data(data, bugs, date, versions):
        d = sputils.get_date(date)
        if data:
            for product, info1 in data.items():
                for channel, info2 in info1.items():
                    pc = Signatures.get_pc(product, channel)
                    qs = db.session.query(Signatures).filter_by(pc=pc,
                                                                date=d)
                    new_sgns = set(info2.keys())
                    to_update = {}
                    for q in qs:
                        sgn = q.signature
                        if sgn in new_sgns:
                            to_update[sgn] = q
                        else:
                            db.session.delete(q)
                            db.session.commit()
                    to_create = new_sgns - set(to_update.keys())

                    for sgn, q in to_update.items():
                        numbers = info2[sgn]
                        bug = bugs[sgn]
                        if q.numbers != numbers:
                            q.numbers = numbers
                            q.exp1 = tools.explosiveness(numbers, 1, 7)
                            q.exp3 = tools.explosiveness(numbers, 3, 7)
                        bug_o = sputils.get_bug_number(bug['unresolved'])
                        if q.bug_o != bug_o:
                            q.bug_o = bug_o
                        bug_c = sputils.get_bug_number(bug['resolved'])
                        if q.bug_c != bug_c:
                            q.bug_c = bug_c

                    if versions and versions[product]:
                        v = versions[product][channel]
                    else:
                        v = []

                    for sgn in to_create:
                        bug = bugs[sgn]
                        bug_o = sputils.get_bug_number(bug['unresolved'])
                        bug_c = sputils.get_bug_number(bug['resolved'])
                        q = Signatures.put(product,
                                           channel,
                                           v,
                                           date,
                                           sgn,
                                           info2[sgn],
                                           bug_o,
                                           bug_c,
                                           commit=True)
            db.session.commit()
            return True
        return False

    @staticmethod
    def get(product, channel, date, sgn=''):
        date = sputils.get_date(date)
        if date:
            pc = Signatures.get_pc(product, channel)
            if sgn:
                cs = db.session.query(Signatures).filter_by(pc=pc,
                                                            date=date,
                                                            signature=sgn)
            else:
                cs = db.session.query(Signatures).filter_by(pc=pc,
                                                            date=date)

            sgns = {}
            date = date.strftime('%Y-%m-%d')
            r = {'versions': None,
                 'product': product,
                 'channel': channel,
                 'dates': sputils.make_dates(date, NDAYS),
                 'date': date,
                 'signatures': sgns}
            for c in cs:
                if not r['versions']:
                    r['versions'] = sputils.get_versions_list(c.version)
                sgns[c.signature] = {'numbers': c.numbers,
                                     'exp1': c.exp1,
                                     'exp3': c.exp3,
                                     'unresolved': c.bug_o,
                                     'resolved': c.bug_c}

            return r

        return {}

    @staticmethod
    def listdates():
        dates = db.session.query(distinct(Signatures.date))
        dates = map(lambda d: d[0], dates)
        dates = sorted(dates, reverse=True)
        dates = map(lambda d: d.strftime('%Y-%m-%d'), dates)

        return list(dates)


def update(date='today'):
    logger.info('Update data for {}: started.'.format(date))
    channels = sputils.get_channels()
    data = {p: None for p in sputils.get_products()}
    versions = {}
    signatures = set()
    for prod in data.keys():
        sgns, v = dc.get_sgns_by_install_time(channels,
                                              product=prod,
                                              date=date,
                                              ndays=NDAYS,
                                              version=True,
                                              N=NSGNS)
        data[prod] = sgns
        if v:
            versions[prod] = v
        for info in sgns.values():
            signatures |= set(info.keys())

    if signatures:
        bugs_by_signature = dc.get_bugs(signatures)
        Signatures.rm(date)
        Signatures.put_data(data, bugs_by_signature, date, versions)

    logger.info('Update data for {}: finished.'.format(date))


def redo(date='today'):
    d = sputils.get_date(date)
    for i in range(NDAYS_OF_DATA):
        update(date=d.strftime('%Y-%m-%d'))
        d -= relativedelta(days=1)
    

def create(date='today'):
    engine = db.get_engine(app)
    if not engine.dialect.has_table(engine, 'signatures'):
        db.create_all()
        redo()
