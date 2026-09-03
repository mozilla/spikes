# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Configuration of the dashboard, read from ``config/dashboard.json``.

The file is resolved relative to the repository root (not the current
working directory) so the module works from tests, gunicorn and the
scheduler alike.  Values can be overridden for tests with :func:`override`.
"""

import copy
import json
import os


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PATH = os.path.join(ROOT, 'config', 'dashboard.json')

_CONFIG = None


def load():
    """Return the configuration dictionary (cached)."""
    global _CONFIG
    if _CONFIG is None:
        with open(PATH, 'r') as In:
            _CONFIG = json.load(In)
    return _CONFIG


def get(key, default=None):
    return load().get(key, default)


def override(**values):
    """Override some values (for tests).  Returns the previous config."""
    global _CONFIG
    previous = copy.deepcopy(load())
    _CONFIG = copy.deepcopy(previous)
    _CONFIG.update(values)
    return previous


def restore(previous):
    global _CONFIG
    _CONFIG = previous


def products():
    return list(get('products', ['Firefox', 'Fenix']))


def channels(product=None):
    """Channels of *product* (or of every product, in order, without
    duplicates).  ``channels`` is either a list shared by all products or
    a ``{product: [channels]}`` mapping (Fenix has no ESR)."""
    conf = get('channels', ['nightly', 'beta', 'release'])
    if isinstance(conf, dict):
        if product is not None:
            return list(conf.get(product, []))
        seen = []
        for p in products():
            for c in conf.get(p, []):
                if c not in seen:
                    seen.append(c)
        return seen
    return list(conf)


def pairs():
    """All (product, channel) pairs, in display order."""
    return [(p, c) for p in products() for c in channels(p)]


def fit_history_days():
    """Days of stored history the channel totals are fitted on.

    ``history_days`` (180) is how far back Socorro is backfilled (its
    retention is ~6 months) and the window signatures are fitted on; the
    totals keep accumulating in the database and are fitted on up to
    ``fit_history_days`` (3 years) so the yearly component can activate
    once two years exist.  Signatures borrow the yearly factors from their
    channel.
    """
    return max(int(get('history_days', 180)),
               int(get('fit_history_days', 1100)))


def min_crashes(channel, product=None):
    """Minimum crashes today for a signature to be scored.

    Looked up as ``"Product/channel"``, then ``channel``, then ``default``:
    every product and channel has its own volume and seasonality.
    """
    mins = get('min_crashes', {})
    if product is not None and '{}/{}'.format(product, channel) in mins:
        return int(mins['{}/{}'.format(product, channel)])
    return int(mins.get(channel, mins.get('default', 10)))


def severity_rules():
    return get('severity', {})


def min_installs(channel, product=None):
    """Minimum distinct installs today for a signature to be flagged.

    One machine crashing a thousand times is one machine.  Looked up like
    :func:`min_crashes`.
    """
    mins = get('min_installs', {})
    if product is not None and '{}/{}'.format(product, channel) in mins:
        return int(mins['{}/{}'.format(product, channel)])
    return int(mins.get(channel, mins.get('default', 5)))
