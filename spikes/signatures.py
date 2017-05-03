# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
from collections import OrderedDict
from dateutil.relativedelta import relativedelta
from jinja2 import Environment, FileSystemLoader
from libmozdata import utils, socorro, gmail
from . import datacollector as dc


channels = ['nightly', 'aurora', 'beta', 'release']
products = ['Firefox', 'FennecAndroid']
query = {}
ndays = 11


def get(date='today'):
    coeff = 3.
    winmin = 7
    winmax = ndays
    signatures = set()
    bugs_by_signature = {}
    spikes = {}
    for product in products:
        data = dc.get_signatures_by_install_time(channels, product=product,
                                                 date=date, query=query,
                                                 ndays=winmax)
        s = dc.get_spiking_signatures(data, coeff, winmin, winmax)

        if not s:
            continue

        for info in s.values():
            for i in info:
                signatures.add(i['signature'])

        spikes[product] = s

    if signatures:
        bugs_by_signature = dc.get_bugs(signatures)

    return spikes, bugs_by_signature


def make_numbers(date, numbers, ndays):
    today = utils.get_date_ymd(date)
    few_days_ago = today - relativedelta(days=ndays)
    res = []
    for i, n in enumerate(numbers):
        date = few_days_ago + relativedelta(days=i)
        date = date.strftime('%a %m-%d')
        res.append((date, n))

    return res


def prepare(spikes, bugs_by_signature, date):
    if spikes:
        today = utils.get_date_ymd(date)
        tomorrow = today + relativedelta(days=1)
        tomorrow = utils.get_date_str(tomorrow)
        today = utils.get_date_str(today)

        search_date = ['>=' + today, '<' + tomorrow]
        affected_chans = []
        results = OrderedDict()

        params = {'product': '',
                  'date': search_date,
                  'release_channel': '',
                  'signature': '',
                  '_facets': ['url',
                              'user_comments',
                              'install_time']}
        params.update(query)

        for product in products:
            if product in spikes:
                params['product'] = product
                data1 = spikes[product]
                results1 = OrderedDict()
                results[product] = results1
                for chan in channels:
                    if chan in data1:
                        affected_chans.append(chan)
                        params['release_channel'] = chan
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
                            results3['numbers'] = make_numbers(today,
                                                               numbers,
                                                               ndays)
                            results3['resolved'] = bugs.get('resolved', None)
                            results3['unresolved'] = bugs.get('unresolved',
                                                              None)
                            results3['url'] = url

        return results, affected_chans, today
    return None


def send_email(emails=[], date='today'):
    spikes, bugs_by_signature = get(date=date)
    r = prepare(spikes, bugs_by_signature, date)
    if r:
        results, affected_chans, today = r
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('signatures_email')
        body = template.render(date=today,
                               results=results)

        chan_list = ', '.join(affected_chans)
        title = 'Spikes in signatures in {}'.format(chan_list)
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
