# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Seasonality-aware crash spike dashboard.

See README.md in this directory for the design.  The package is wired into
the Flask app through :func:`spikes.dashboard.api.blueprint` and into the
scheduler through :func:`spikes.dashboard.update.run`.
"""
