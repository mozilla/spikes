# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import re


__SKIPLIST = None
__THRESHOLDS = None


class BadRegEx(Exception):
    """Raised when there is an invalid regular expression."""


def get_skiplist():
    global __SKIPLIST
    if not __SKIPLIST:
        with open('./config/skiplist.json', 'r') as In:
            data = json.load(In)
        __SKIPLIST = {}
        for k, v in data.items():
            res = []
            for pat in v:
                try:
                    r = re.compile(pat)
                except Exception as ex:
                    raise BadRegEx('Regex error: {}'.format(pat))
                res.append(r)
            __SKIPLIST[k] = res

    return __SKIPLIST


def get_skiplist_channel(chan):
    sl = get_skiplist()
    return sl.get(chan, []) + sl['common']


def get_thresholds():
    global __THRESHOLDS
    if not __THRESHOLDS:
        with open('./config/thresholds.json', 'r') as In:
            __THRESHOLDS = json.load(In)
    return __THRESHOLDS


def get_threshold(prod, chan, kind='normal'):
    return get_thresholds()[kind][prod][chan]


def get_limit():
    return get_thresholds()['socorro_limit']
