# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import copy
from collections import defaultdict
from dateutil.relativedelta import relativedelta
import functools
from libmozdata import socorro, utils
from libmozdata.bugzilla import Bugzilla
from libmozdata.connection import Connection
import numpy as np
from . import tools
from . import differentiators as diftors


def get(channels, product='Firefox', date='today', query={}):
    today = utils.get_date_ymd(date)
    tomorrow = today + relativedelta(days=1)
    six_months_ago = today - relativedelta(weeks=25)
    search_date = socorro.SuperSearch.get_search_date(six_months_ago, tomorrow)
    data = {chan: {} for chan in channels}

    def handler(json, data):
        if not json['facets']['histogram_date']:
            return

        for facets in json['facets']['histogram_date']:
            date = utils.get_date_ymd(facets['term'])
            channels = facets['facets']['release_channel']
            for chan in channels:
                total = chan['count']
                channel = chan['term']
                data[channel][date] = total

    params = {'product': product,
              'date': search_date,
              'release_channel': channels,
              '_histogram.date': 'release_channel',
              '_results_number': 0}
    params.update(query)

    socorro.SuperSearch(params=params,
                        handler=handler,
                        handlerdata=data).wait()

    return data


def get_by_install_time(channels, product='Firefox',
                        date='today', query={}):
    today = utils.get_date_ymd(date)
    tomorrow = today + relativedelta(days=1)
    six_months_ago = today - relativedelta(weeks=25)
    search_date = socorro.SuperSearch.get_search_date(six_months_ago, tomorrow)
    data = {chan: {} for chan in channels}

    def handler(json, data):
        if not json['facets']['histogram_date']:
            return

        for facets in json['facets']['histogram_date']:
            date = utils.get_date_ymd(facets['term'])
            ninstalls = facets['facets']['cardinality_install_time']['value']
            data[date] = ninstalls

    params = {'product': product,
              'date': search_date,
              'release_channel': '',
              '_histogram.date': '_cardinality.install_time',
              '_results_number': 10}
    params.update(query)

    searches = []
    for chan in channels:
        params = copy.deepcopy(params)
        params['release_channel'] = chan
        searches.append(socorro.SuperSearch(params=params,
                                            handler=handler,
                                            handlerdata=data[chan]))

    for s in searches:
        s.wait()

    return data


def get_versions(channels, date, product='Firefox'):
    res = defaultdict(lambda: list())
    versions = socorro.ProductVersions.get_all_versions(product)
    date = utils.get_date_ymd(date)
    for chan in channels:
        infos = sorted(versions[chan].values(),
                       key=lambda p: p['dates'][0],
                       reverse=True)
        for info in infos:
            res[chan] += info['all']
            if date > info['dates'][0]:
                break

    return res


def get_total(channels, product='Firefox', date='today'):
    today = utils.get_date_ymd(date)
    tomorrow = today + relativedelta(days=1)
    search_date = socorro.SuperSearch.get_search_date(today, tomorrow)
    data = {chan: 0 for chan in channels}

    def handler(json, data):
        if not json['facets']['histogram_date']:
            return

        for facets in json['facets']['histogram_date']:
            channels = facets['facets']['release_channel']
            for chan in channels:
                total = chan['count']
                channel = chan['term']
                data[channel] = total

    socorro.SuperSearch(params={'product': product,
                                'date': search_date,
                                'release_channel': channels,
                                '_histogram.date': 'release_channel',
                                '_results_number': 0},
                        handler=handler,
                        handlerdata=data).wait()

    return data


def get_top_signatures(data, N=50):
    for chan, stats_by_sgn in data.items():
        sbs = {}
        for sgn, stats in stats_by_sgn.items():
            # replace dictionary with an array of numbers
            numbers = tools.get_array(stats)
            if numbers[-1] != 0:
                sbs[sgn] = numbers
        data[chan] = sbs

    if N:
        for chan, stats_by_sgn in data.items():
            # take only the top N signatures for the last day
            d = list(sorted(stats_by_sgn.items(),
                            key=lambda p: p[1][-1],
                            reverse=True))
            if len(d) > N:
                data[chan] = dict(d[:N])
            else:
                data[chan] = dict(d)

    return data


def get_signatures(channels, product='Firefox',
                   date='today', query={}, ndays=7, N=50):
    today = utils.get_date_ymd(date)
    tomorrow = today + relativedelta(days=1)
    few_days_ago = today - relativedelta(days=ndays)
    search_date = socorro.SuperSearch.get_search_date(few_days_ago, tomorrow)
    base = {few_days_ago + relativedelta(days=i): 0 for i in range(ndays + 1)}
    data = {chan: defaultdict(lambda: copy.copy(base)) for chan in channels}

    def handler(json, data):
        if json['errors'] or not json['facets']['histogram_date']:
            return

        for facets in json['facets']['histogram_date']:
            date = utils.get_date_ymd(facets['term'])
            signatures = facets['facets']['signature']
            for signature in signatures:
                total = signature['count']
                sgn = signature['term']
                data[sgn][date] += total

    params = {'product': product,
              'date': search_date,
              'release_channel': '',
              '_histogram.date': 'signature',
              '_results_number': 0,
              '_facets_size': 10000}
    params.update(query)

    searches = []
    for chan in channels:
        params = copy.deepcopy(params)
        params['release_channel'] = chan
        searches.append(socorro.SuperSearch(params=params,
                                            handler=handler,
                                            handlerdata=data[chan]))

    for s in searches:
        s.wait()

    return get_top_signatures(data, N=N)


def get_sgns_by_install_time(channels, product='Firefox',
                             date='today', query={},
                             ndays=7, version=False, N=50):
    today = utils.get_date_ymd(date)
    few_days_ago = today - relativedelta(days=ndays)
    base = {few_days_ago + relativedelta(days=i): 0 for i in range(ndays + 1)}
    data = {chan: defaultdict(lambda: copy.copy(base)) for chan in channels}
    if version:
        version = get_versions(channels, few_days_ago, product=product)

    def handler(date, json, data):
        if json['errors'] or not json['facets']['signature']:
            return

        for facets in json['facets']['signature']:
            sgn = facets['term']
            count = facets['facets']['cardinality_install_time']['value']
            data[sgn][date] = count

    params = {'product': product,
              'date': '',
              'release_channel': '',
              '_aggs.signature': '_cardinality.install_time',
              '_results_number': 0,
              '_facets_size': 10000}
    params.update(query)

    searches = []
    for chan in channels:
        params = copy.deepcopy(params)
        params['release_channel'] = chan
        if version:
            params['version'] = version[chan]
        for i in range(ndays + 1):
            day = few_days_ago + relativedelta(days=i)
            day_after = day + relativedelta(days=1)
            search_date = socorro.SuperSearch.get_search_date(day, day_after)
            params = copy.deepcopy(params)
            params['date'] = search_date
            hdler = functools.partial(handler, day)
            searches.append(socorro.SuperSearch(params=params,
                                                handler=hdler,
                                                handlerdata=data[chan]))

    for s in searches:
        s.wait()

    return get_top_signatures(data, N=N), version


def get_sgns_info(sgns_by_chan, product='Firefox',
                  date='today', query={}, versions=None):
    today = utils.get_date(date)
    tomorrow = utils.get_date(date, -1)
    data = {chan: {} for chan in sgns_by_chan.keys()}

    def handler(json, data):
        if json['errors'] or not json['facets']['signature']:
            return

        for facets in json['facets']['signature']:
            sgn = facets['term']
            count = facets['count']
            platforms = defaultdict(lambda: 0)
            startup = {True: 0, False: 0}
            data[sgn] = {'count': count,
                         'platforms': platforms,
                         'startup': startup}
            facets = facets['facets']
            for ppv in facets['platform_pretty_version']:
                platforms[ppv['term']] += ppv['count']
            for sc in facets['startup_crash']:
                term = sc['term']
                startup[term == 'T'] += sc['count']

    base = {'product': product,
            'date': ['>=' + today,
                     '<' + tomorrow],
            'release_channel': '',
            'version': '',
            'signature': '',
            '_aggs.signature': ['platform_pretty_version',
                                'startup_crash'],
            '_results_number': 0,
            '_facets_size': 100}

    base.update(query)

    searches = []
    for chan, signatures in sgns_by_chan.items():
        params = copy.copy(base)
        params['release_channel'] = chan
        if versions:
            params['version'] = versions[chan]
        for sgns in Connection.chunks(signatures, 10):
            p = copy.copy(params)
            p['signature'] = ['=' + s for s in sgns]
            searches.append(socorro.SuperSearch(params=p,
                                                handler=handler,
                                                handlerdata=data[chan]))

    for s in searches:
        s.wait()

    return data


def get_outliers(stats, diff=diftors.diff, noutliers=5):
    delta = {}
    infinite = []
    for sgn, numbers in stats.items():
        d = diff(numbers)
        if d is not None:
            if np.isinf(d):
                infinite.append(sgn)
            else:
                delta[sgn] = d

    sgns = sorted(delta.items(), key=lambda p: p[0])
    x = [float(n) for _, n in sgns]
    outliers = tools.generalized_esd(x, noutliers, alpha=0.01, method='mean')
    return [sgns[i][0] for i in outliers], infinite


def is_spiking(data, coeff, win):
    spikes = {}
    for chan, numbers in data.items():
        x = tools.get_array(numbers)
        spike, _, _ = tools.is_spiking(x, coeff=coeff, win=win)
        spikes[chan] = 'yes' if spike == 'up' else 'no'

    return spikes


def get_spiking_signatures(data, coeff, winmin, winmax):
    spikes = defaultdict(lambda: list())
    for chan, stats in data.items():
        for sgn, numbers in stats.items():
            res = tools.is_sgn_spiking(numbers, stats, coeff,
                                       winmin, winmax, sgn=sgn)
            if res:
                win, diff = res
                info = {'signature': sgn,
                        'numbers': numbers,
                        'win': win,
                        'diff': diff}
                spikes[chan].append(info)

    return spikes


def plot(data, coeff, win):
    import matplotlib.pyplot as plt

    fig = plt.figure()
    nchans = len(data)

    for i, j in enumerate(data.items()):
        chan, numbers = j
        numbers = tools.get_array(numbers)
        ax = fig.add_subplot(nchans, 1, i + 1)
        y = numbers
        N = len(y)
        x = range(N)
        ax.plot(x, y, color='blue')

        spike, mx, md = tools.is_spiking(y, coeff=coeff, win=win)
        x = range(len(mx))
        ax.plot(x, mx, color='orange')

        y = tools.ma(mx, win)
        y = np.ceil(y)
        x = range(len(y))
        ax.plot(x, y, color='black')

        d = tools.ma(md, win)
        d = np.ceil(coeff * np.ceil(d))
        d1 = y + d
        d2 = y - d
        x = range(len(d))
        ax.fill_between(x, d1, d2, facecolor='blue', alpha=0.2)

        if spike == 'up':
            ax.plot([N - 1], [numbers[-1]], 'ro', color='red')
        elif spike == 'down':
            ax.plot([N - 1], [numbers[-1]], 'ro', color='green')
        ax.set_title(chan, fontsize=10)

    fig.tight_layout()
    plt.show()


def get_bugs(signatures):
    bugs_by_signature = socorro.Bugs.get_bugs(list(signatures))
    bugs = set()
    for b in bugs_by_signature.values():
        bugs = bugs.union(set(b))
    bugs = list(sorted(bugs))

    def handler(bug, data):
        data[bug['id']] = bug['status']

    data = {}
    Bugzilla(bugids=bugs, include_fields=['id', 'status'],
             bughandler=handler, bugdata=data).wait()

    for s, bugs in bugs_by_signature.items():
        resolved = []
        unresolved = []
        for b in bugs:
            b = int(b)
            status = data.get(b, None)
            if status in ['RESOLVED', 'VERIFIED', 'CLOSED']:
                resolved.append(b)
            elif status is not None:
                unresolved.append(b)

        if resolved:
            last_resolved = max(resolved)
            last_resolved = (str(last_resolved),
                             Bugzilla.get_links(last_resolved))
        else:
            last_resolved = None

        if unresolved:
            last_unresolved = max(unresolved)
            last_unresolved = (str(last_unresolved),
                               Bugzilla.get_links(last_unresolved))
        else:
            last_unresolved = None

        unresolved = sorted(unresolved)
        bugs_by_signature[s] = {'resolved': last_resolved,
                                'unresolved': last_unresolved}

    return bugs_by_signature
