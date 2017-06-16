# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
from collections import OrderedDict
from jinja2 import Environment, FileSystemLoader
from libmozdata import utils, socorro, gmail
from . import datacollector as dc
from . import utils as sputils


channels = ['nightly', 'beta', 'release']
products = ['Firefox', 'FennecAndroid']


def get(date='today', ndays=11, query={}):
    coeff = 3.
    winmin = 7
    winmax = ndays
    signatures = set()
    bugs_by_signature = {}
    spikes = {}
    versions = {}
    products = sputils.get_products()
    channels = sputils.get_channels()
    for product in products:
        data, version = dc.get_sgns_by_install_time(channels, product=product,
                                                    date=date, query=query,
                                                    ndays=winmax, version=True)
        versions[product] = version
        s = dc.get_spiking_signatures(data, coeff, winmin, winmax)

        if not s:
            continue

        for info in s.values():
            for i in info:
                signatures.add(i['signature'])

        spikes[product] = s

    if signatures:
        bugs_by_signature = dc.get_bugs(signatures)

    return spikes, bugs_by_signature, versions


def prepare_for_html(data, product, channel, query={}):
    params = sputils.get_params_for_link(data['date'], query=query)
    params['release_channel'] = channel
    params['product'] = product
    params['version'] = data['versions']
    for sgn, info in data['signatures'].items():
        params['signature'] = '=' + sgn
        url = socorro.SuperSearch.get_link(params)
        url += '#crash-reports'
        info['socorro_url'] = url

    def sort_fun(p):
        last = float(p[1]['numbers'][-1])
        m = p[1]['mean']
        e = p[1]['stddev']
        a = 1 if last >= sputils.get_thresholds()[channel] else 0
        c = (float(last) - m) / e if e != 0 else float('Inf')
        return (a, c, last, p[0])

    data['signatures'] = sorted(data['signatures'].items(),
                                key=lambda p: sort_fun(p),
                                reverse=True)


def prepare(spikes, bugs_by_signature, date, versions, query, ndays):
    if spikes:
        affected_chans = set()
        results = OrderedDict()
        today = utils.get_date(date)
        params = sputils.get_params_for_link(date, query=query)

        for product in sputils.get_products():
            if product in spikes:
                params['product'] = product
                data1 = spikes[product]
                results1 = OrderedDict()
                results[product] = results1
                version_prod = versions[product]
                for chan in sputils.get_channels():
                    if chan in data1:
                        affected_chans.add(chan)
                        params['release_channel'] = chan
                        params['version'] = version_prod[chan]
                        results2 = OrderedDict()
                        results1[chan] = results2

                        for stats in sorted(data1[chan],
                                            reverse=True,
                                            key=lambda d: (d['diff'],
                                                           d['numbers'][-1],
                                                           d['signature'])):
                            sgn = stats['signature']
                            params['signature'] = '=' + sgn
                            url = socorro.SuperSearch.get_link(params)
                            url += '#crash-reports'
                            bugs = bugs_by_signature.get(sgn, {})
                            results3 = OrderedDict()
                            results2[sgn] = results3
                            numbers = stats['numbers']
                            results3['numbers'] = sputils.make_numbers(today,
                                                                       numbers,
                                                                       ndays)
                            results3['resolved'] = bugs.get('resolved', None)
                            results3['unresolved'] = bugs.get('unresolved',
                                                              None)
                            results3['url'] = url
        affected_chans = list(sorted(affected_chans))

        return results, affected_chans, today
    return None


def send_email(emails=[], date='today'):
    query = {}
    ndays = 11
    spikes, bugs_by_signature, versions = get(date=date,
                                              query=query,
                                              ndays=ndays)
    r = prepare(spikes, bugs_by_signature, date, versions, query, ndays)
    if r:
        results, affected_chans, today = r
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('signatures_email')
        body = template.render(date=today,
                               results=results,
                               versions=versions)

        chan_list = ', '.join(affected_chans)
        title = 'Spikes in signatures in {} the {}'.format(chan_list, today)
        if emails:
            gmail.send(emails, title, body, html=True)
        else:
            with open('/tmp/foo.html', 'w') as Out:
                Out.write(body)
            print('Title: %s' % title)
            print('Body:')
            print(body)


if __name__ == '__main__':
    description = 'Monitor spikes in crashes'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-e', '--email', dest='emails',
                        action='store', nargs='+',
                        default=[], help='emails')
    parser.add_argument('-d', '--date', dest='date',
                        action='store', default='today', help='date')
    args = parser.parse_args()

    send_email(emails=args.emails, date=args.date)
