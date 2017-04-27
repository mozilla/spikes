# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from . import tools


def __diff(x, y):
    x = float(x)
    y = float(y)
    d = y - x
    return d if d > 0 else None


def __diff_p(x, y):
    x = float(x)
    y = float(y)
    if x != 0:
        d = y / x - 1
    elif y == 0:
        d = 0
    else:
        d = float('inf')
    return d if d > 0 else None


def diff(x):
    return __diff(x[-2], x[-1])


def diff_p(x):
    return __diff_p(x[-2], x[-1])


def diff_same_day(x):
    return __diff(x[-8], x[-1])


def diff_same_day_p(x):
    return __diff_p(x[-8], x[-1])


def diff_mean(ndays, x):
    m, _ = tools.mean(x[-(ndays + 1):-1])
    return __diff(m, x[-1])


def diff_mean_p(ndays, x):
    m, _ = tools.mean(x[-(ndays + 1):-1])
    return __diff_p(m, x[-1])
