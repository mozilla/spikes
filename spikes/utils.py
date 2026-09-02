# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from dateutil.relativedelta import relativedelta
from libmozdata import utils


def get_products():
    return ['Firefox', 'Fenix', 'Thunderbird']


def get_channels():
    return ['nightly', 'beta', 'release']


def get_params_for_link(date, query={}):
    today = utils.get_date_ymd(date)
    tomorrow = today + relativedelta(days=1)
    tomorrow = utils.get_date_str(tomorrow)
    today = utils.get_date_str(today)
    search_date = ['>=' + today, '<' + tomorrow]
    params = {'product': '',
              'date': search_date,
              'release_channel': '',
              'signature': '',
              '_facets': ['url',
                          'user_comments',
                          'install_time',
                          'version',
                          'address',
                          'moz_crash_reason',
                          'reason',
                          'build_id',
                          'platform_pretty_version',
                          'signature',
                          'useragent_locale']}
    params.update(query)
    return params


def get_esearch_sgn(sgn):
    if sgn.startswith('\"'):
        return '@' + sgn
    return '=' + sgn


def make_numbers(date, numbers, ndays):
    today = utils.get_date_ymd(date)
    few_days_ago = today - relativedelta(days=ndays)
    res = []
    for i, n in enumerate(numbers):
        date = few_days_ago + relativedelta(days=i)
        date = date.strftime('%a %m-%d')
        res.append((date, n))

    return res
