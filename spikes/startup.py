# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
from collections import defaultdict, OrderedDict
from dateutil.relativedelta import relativedelta
import inflect
from jinja2 import Environment, FileSystemLoader
from libmozdata import utils, socorro
from . import datacollector as dc
from . import differentiators as diftors
from . import tools, mail


channels = ['nightly', 'beta', 'release']
products = ['Firefox', 'FennecAndroid']
query = {'startup_crash': '__true__'}


def get(date='today'):
    significants = defaultdict(lambda: defaultdict(lambda: dict()))
    signatures = set()
    bugs_by_signature = {}
    totals = {}
    coeff = 4.
    win = 5
    for product in products:
        data = dc.get_by_install_time(channels, product=product,
                                      date=date, query=query)

        if not data:
            continue

        spikes = dc.is_spiking(data, coeff, win)
        # dc.plot(data, coeff, win)
        spiking = []
        for chan, res in spikes.items():
            if res == 'yes':
                spiking.append(chan)
        if spiking:
            totals[product] = dc.get_total(channels=spiking,
                                           product=product,
                                           date=date)
            data, _ = dc.get_sgns_by_install_time(channels=spiking,
                                                  product=product,
                                                  date=date,
                                                  query=query,
                                                  ndays=1)
            for chan, stats in data.items():
                outliers, _ = dc.get_outliers(stats, diff=diftors.diff)
                for o in outliers:
                    numbers = stats[o]
                    numbers.append(tools.get_percent(numbers[-2], numbers[-1]))
                    significants[product][chan][o] = numbers
                    signatures.add(o)

    if signatures:
        bugs_by_signature = dc.get_bugs(signatures)

    return significants, bugs_by_signature, totals


def prepare(significants, bugs_by_signature, totals, date):
    if significants:
        today = utils.get_date_ymd(date)
        yesterday = today - relativedelta(days=1)
        yesterday = utils.get_date_str(yesterday)
        tomorrow = today + relativedelta(days=1)
        tomorrow = utils.get_date_str(tomorrow)
        today = utils.get_date_str(today)

        search_date = ['>=' + today, '<' + tomorrow]
        affected_chans = set()
        urls = defaultdict(lambda: dict())
        spikes_number = 0
        results = OrderedDict()

        for product in products:
            if product in significants:
                data1 = significants[product]
                results1 = OrderedDict()
                results[product] = results1
                for chan in channels:
                    if chan in data1:
                        affected_chans.add(chan)
                        params = {'product': product,
                                  'date': search_date,
                                  'release_channel': chan}
                        params.update(query)
                        url = socorro.SuperSearch.get_link(params)
                        urls[product][chan] = url
                        spikes_number += 1
                        results2 = OrderedDict()
                        results1[chan] = results2
                        sgns = data1[chan]
                        # we order on the 2nd number (today) and the signature
                        for sgn, num in sorted(sgns.items(),
                                               key=lambda p: (p[1][1], p[0]),
                                               reverse=True):
                            bugs = bugs_by_signature.get(sgn, {})
                            results3 = OrderedDict()
                            results2[sgn] = results3
                            results3['numbers'] = num
                            results3['resolved'] = bugs.get('resolved', None)
                            results3['unresolved'] = bugs.get('unresolved',
                                                              None)
        affected_chans = list(sorted(affected_chans))

        return results, spikes_number, urls, affected_chans, yesterday, today
    return None


def send_email(emails=[], date='today'):
    significants, bugs_by_signature, totals = get(date=date)
    r = prepare(significants, bugs_by_signature, totals, date)
    if r:
        results, spikes_number, urls, affected_chans, yesterday, today = r
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('startup_crashes_email')
        spikes_number_word = inflect.engine().number_to_words(spikes_number)
        body = template.render(spikes_number=spikes_number,
                               spikes_number_word=spikes_number_word,
                               totals=totals,
                               start_date=yesterday,
                               end_date=today,
                               results=results,
                               urls=urls)

        chan_list = ', '.join(affected_chans)
        title = 'Spikes in startup crashes in {} the {}'
        title = title.format(chan_list, today)
        if emails:
            mail.send(emails, title, body, html=True)
        else:
            with open('/tmp/foo.html', 'w') as Out:
                Out.write(body)
            print('Title: %s' % title)
            print('Body:')
            print(body)


if __name__ == '__main__':
    description = 'Monitor spikes in startup crashes'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-e', '--email', dest='emails',
                        action='store', nargs='+',
                        default=[], help='emails')
    parser.add_argument('-d', '--date', dest='date',
                        action='store', default='today', help='date')
    args = parser.parse_args()

    send_email(emails=args.emails, date=args.date)
