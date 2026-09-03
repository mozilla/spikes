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


def alert_rate():
    """False-alarm rate per series-day allowed to each severity level; the
    z thresholds are learned from it per channel (see calibration.py)."""
    return get('alert_rate', {})


def volume_share():
    """Share of the channel's expected day a signature needs in crashes to
    be flagged (installs: half of it)."""
    return float(get('volume_share', 0.001))


def storm_quantile():
    """Quantile of the channel's crashes-per-install ratios above which a
    signature is a storm."""
    return float(get('storm_quantile', 0.995))
