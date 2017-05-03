# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import numpy as np
import scipy.stats as stats


def get_percent(x, y):
    if x == 0:
        if y == 0:
            return '0%'
        return '+Inf%'
    return '+{}%'.format(int(round((float(y) / float(x) - 1) * 100)))


def get_array(d):
    """Return an array from a dictionary

    Args:
        d (dict)

    Returns:
        list
    """
    if isinstance(d, dict):
        return [n for _, n in sorted(d.items(),
                                     key=lambda p: p[0])]
    return d


def __get_lambda_critical(N, i, alpha):
    """Get lambda for generalized ESD test
       (http://www.itl.nist.gov/div898/handbook/eda/section3/eda35h3.htm).

    Args:
        N (int): the number of data in sequence
        i (int): the i-th outlier
        alpha (float): the signifiance level

    Returns:
        list[int]: list of the index of outliers
    """
    p = 1. - alpha / (2. * (N - i + 1))
    t = stats.t.ppf(p, N - i - 1)
    return (N - i) * t / np.sqrt((N - i - 1 + t ** 2) * (N - i + 1))


def __get_pd_median(data, c=1.):
    """Get the median and the mad of data

    Args:
        data (numpy.ndarray): the data

    Returns:
        float, float: the median and the mad
    """
    p = np.nanmedian(data)
    d = np.nanmedian(np.abs(data - p)) / c  # d is the MAD

    return p, d


def __get_pd_mean(data, c=1.):
    """Get the mean and the standard deviation of data

    Args:
        data (numpy.ndarray): the data

    Returns:
        float, float: the mean and the standard deviation
    """
    p = np.nanmean(data)
    d = np.nanstd(data) / c

    return p, d


def generalized_esd(x, r, alpha=0.05, method='mean'):
    """Generalized ESD test for outliers
       (http://www.itl.nist.gov/div898/handbook/eda/section3/eda35h3.htm).

    Args:
        x (numpy.ndarray): the data
        r (int): max number of outliers
        alpha (float): the signifiance level
        method (str): 'median' or 'mean'

    Returns:
        list[int]: list of the index of outliers
    """
    x = np.asarray(x, dtype=np.float64)
    fn = __get_pd_median if method == 'median' else __get_pd_mean
    NaN = float('nan')
    outliers = []
    N = len(x)
    for i in range(1, r + 1):
        if np.any(~np.isnan(x)):
            m, e = fn(x)
            if e != 0.:
                y = np.abs(x - m)
                j = np.nanargmax(y)
                R = y[j]
                l = __get_lambda_critical(N, i, alpha)
                if R > l * e:
                    outliers.append(j)
                    x[j] = NaN
                else:
                    break
            else:
                break
        else:
            break
    return outliers


def ma(x, win):
    """Compute the moving average of x with a window equal to win

    Args:
        x (numpy.array): data
        win (int): window

    Returns:
        numpy.array: the smoothed data
    """
    y = np.ones(win, dtype=np.float64)
    i = win - 1
    _x = np.convolve(x, y, mode='full')[:-i]
    _x[1:i] = _x[1:i] / np.arange(2., win, dtype=np.float64)
    _x[i:] = _x[i:] / float(win)

    return _x


def mean(x):
    l = len(x)
    m = np.sum(x) / l
    e = np.sqrt(np.sum((x - m) ** 2) / l)
    return m, e


def median(x):
    q1, m, q3 = np.percentile(x, [25, 50, 100], interpolation='midpoint')
    return m, q3 - q1


def __convert(x):
    if not isinstance(x, np.ndarray):
        return np.asarray(x, dtype=np.float64)
    return x


def moving(x, f=mean, coeff=2.0):
    x = __convert(x)
    pieces = [[0, 0]]
    coeff = float(coeff)
    l = len(x)

    for i in range(1, l):
        p, d = f(x[pieces[-1][0]:(i + 1)])
        if abs(x[i] - p) <= coeff * d:
            pieces[-1][1] = i
        else:
            pieces.append([i, i])

    yp = np.empty(l)
    yd = np.empty(l)
    pos = 0
    for piece in pieces:
        p, d = f(x[piece[0]:(piece[1] + 1)])
        N = piece[1] - piece[0] + 1
        yp[pos:(pos + N)] = p
        yd[pos:(pos + N)] = d
        pos += N

    return yp, yd


def multimoving(x, f=mean, coeff=2.0):
    """Compute all the moving curves in moving the first point from left to right
       and for each point, select the position which minimize the dispersion.

    Args:
        x (list): numbers
        f (func): the fonction to compute the position
        coeff (float): a coefficient for the tolerance relative to the disp

    Returns:
        (numpy.array): the smoothed data
    """
    x = __convert(x)
    l = len(x)
    ys = np.empty((l, l))
    ds = np.empty((l, l))

    ys[0], ds[0] = moving(x, f, coeff)
    ys[-1], ds[-1] = moving(x[::-1], f, coeff)
    ys[-1] = ys[-1][::-1]
    ds[-1] = ds[-1][::-1]

    for i in range(1, l - 1):
        x1 = x[:(i + 1)]
        x2 = x[i:]
        y1, d1 = moving(x1[::-1], f, coeff)
        y2, d2 = moving(x2, f, coeff)

        if d2[0] < d1[0]:
            y1[0] = y2[0]
            d1[0] = d2[0]

        ys[i][:len(y1)] = y1[::-1]
        ys[i][len(y1):] = y2[1:]
        ds[i][:len(d1)] = d1[::-1]
        ds[i][len(d1):] = d2[1:]

    y = np.empty(l)
    d = np.empty(l)
    mins_index = np.argmin(ds, axis=0)
    for i in range(l):
        y[i] = ys[mins_index[i]][i]
        d[i] = ds[mins_index[i]][i]

    return y, d


def is_spiking(x, coeff=3., win=7):
    last = x[-1]
    y = x[:-1]
    mx, md = multimoving(y, coeff=coeff)
    m, _ = mean(mx[-win:])
    d, _ = mean(md[-win:])

    m = np.ceil(m)
    d = np.ceil(coeff * np.ceil(d))

    if last > m + d and last > y[-1]:
        return 'up', mx, md
    elif last < m - d and last < y[-1]:
        return 'down', mx, md
    else:
        return 'nothing', mx, md


def is_sgn_spiking(numbers, stats, coeff,
                   winmin, winmax, sgn='',
                   verbose=False):
    last = numbers[-1]
    x = __convert(numbers[:-1])
    for win in range(winmax, winmin - 1, -1):
        m, e = __get_pd_mean(x[-win:])
        m = np.ceil(m)
        e = np.ceil(e) if e != 0 else 1
        diff = last - m
        if diff > coeff * e:
            m_r, e_r, m_d, e_d = __get_mean_rate(stats, coeff, win)
            if verbose:
                print(('out', sgn, numbers, win, m, e, m_r, e_r, m_d, e_d))
            if m == 0:
                if verbose:
                    print(('zero', sgn, numbers, win,
                           m, e, m_r, e_r, m_d, e_d))
                return win, diff
            else:
                rate = np.round(100 * (last / m - 1))
                if rate - m_r > e_r:
                    if verbose:
                        print(('rate', sgn, numbers, win,
                               m, e, m_r, e_r, m_d, e_d))
                    return win, diff
                elif last - m - m_d > e_d:
                    if verbose:
                        print(('diff', sgn, numbers, win,
                               m, e, m_r, e_r, m_d, e_d))
                    return win, diff

    return None


def __get_mean_rate(stats, coeff, win, __cache={}):
    if win not in __cache:
        NaN = float('NaN')
        R = len(stats)
        for numbers in stats.values():
            C = len(numbers)
            break
        x = np.empty((R, C))
        for i, numbers in enumerate(stats.values()):
            x[i, :] = numbers

        last = x[:, -1]
        y = x[:, -(win + 1):-1]
        means = np.mean(y, axis=1)
        means = np.ceil(means)
        diffs = last - means
        zeros = means == 0
        means[zeros] = 1.
        ratios = 100 * (last / means - 1)
        ratios[zeros] = NaN
        outliers = generalized_esd(ratios, 10)
        ratios[outliers] = NaN
        m_r, e_r = __get_pd_mean(ratios)
        m_r = np.round(m_r)
        e_r = np.round(e_r)

        outliers = generalized_esd(diffs, 10)
        diffs[outliers] = NaN
        m_d, e_d = __get_pd_mean(diffs)
        m_d = np.ceil(m_d)
        e_d = np.ceil(e_d)

        __cache[win] = (m_r, e_r, m_d, e_d)

    return __cache[win]
