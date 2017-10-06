# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
from dateutil.relativedelta import relativedelta
import six
from libmozdata import utils


try:
    UNICODE_EXISTS = bool(type(unicode))
except NameError:
    UNICODE_EXISTS = False


def get_str(s):
    if UNICODE_EXISTS and type(s) == unicode:
        return s.encode('raw_unicode_escape')
    return s


def get_products():
    return ['Firefox', 'FennecAndroid']


def get_channels():
    return ['nightly', 'beta', 'release']


def get_thresholds():
    return {'nightly': 5,
            'beta': 10,
            'release': 50}


def get_params_for_link(date, query={}):
    today = utils.get_date_ymd(date)
    tomorrow = today + relativedelta(days=1)
    tomorrow = utils.get_date_str(tomorrow)
    today = utils.get_date_str(today)
    search_date = ['>=' + today, '<' + tomorrow]
    params = {'product': '',
              'date': search_date,
              'release_channel': '',
              'version': '',
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


def get_date(date):
    if date:
        try:
            if isinstance(date, six.string_types):
                if date.startswith('today'):
                    s = date.split('-')
                    if len(s) == 2 and s[1].isdigit():
                        date = utils.get_date_ymd('today')
                        date -= relativedelta(days=int(s[1]))
                        return datetime.date(date.year, date.month, date.day)

                date = utils.get_date_ymd(date)
                return datetime.date(date.year, date.month, date.day)
            elif isinstance(date, datetime.date):
                return date
            elif isinstance(date, datetime.datetime):
                return datetime.date(date.year, date.month, date.day)
        except:
            pass
    return None


def get_versions_str(v):
    return '|'.join(v)


def get_versions_list(v):
    return v.split('|')


def get_correct_date(date):
    date = get_date(date)
    if date:
        return utils.get_date_str(date)
    return utils.get_date('today')


def get_correct_product(p):
    if isinstance(p, list) and len(p) >= 1:
        p = p[0]
    if isinstance(p, six.string_types):
        p = p.lower()
        prods = {'firefox': 'Firefox',
                 'fennecandroid': 'FennecAndroid'}
        return prods.get(p, 'Firefox')
    return 'Firefox'


def get_correct_channel(c):
    if isinstance(c, list) and len(c) >= 1:
        c = c[0]
    if isinstance(c, six.string_types):
        c = c.lower()
        return c if c in get_channels() else 'nightly'
    return 'nightly'


def get_correct_sgn(sgn):
    if isinstance(sgn, six.string_types):
        return sgn
    elif isinstance(sgn, list) and len(sgn) >= 1:
        return sgn[0]
    return ''


def get_supersearch_sgn(sgn):
    if sgn.startswith('\"'):
        return '@' + sgn
    return '=' + sgn


def get_bug_number(bug):
    if bug is None:
        return 0
    if isinstance(bug, tuple):
        return int(bug[0])
    return int(bug)


def make_numbers(date, numbers, ndays):
    today = utils.get_date_ymd(date)
    few_days_ago = today - relativedelta(days=ndays)
    res = []
    for i, n in enumerate(numbers):
        date = few_days_ago + relativedelta(days=i)
        date = date.strftime('%a %m-%d')
        res.append((date, n))

    return res


def make_dates(date, ndays):
    today = utils.get_date_ymd(date)
    few_days_ago = today - relativedelta(days=ndays)
    res = []
    for i in range(ndays + 1):
        date = few_days_ago + relativedelta(days=i)
        date = date.strftime('%a %m-%d')
        res.append(date)

    return res
