# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import Flask, redirect, request, send_from_directory, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from libmozdata import config as lmdconfig
from libmozdata.config import ConfigEnv
import os


IMMUTABLE_CACHE_CONTROL = 'public, max-age=31536000, immutable'
IMMUTABLE_ASSET_SUFFIXES = ('.ico', '.png', '.svg', '.woff2')


# libmozdata settings come from LIBMOZDATA_CFG_<SECTION>_<OPTION> environment
# variables (e.g. LIBMOZDATA_CFG_BUGZILLA_TOKEN). This must run before any
# other libmozdata module is imported, since they read the config on import.
lmdconfig.set_config(ConfigEnv())
lmdconfig.set_default_value('User-Agent', 'name', 'crash-clouseau')

app = Flask(__name__)

# Fall back to an in-memory SQLite database so the package can be imported
# (e.g. by the tests) without a DATABASE_URL.
uri = os.getenv('DATABASE_URL', 'sqlite://')
# Heroku exposes the URL with a postgres:// scheme, which SQLAlchemy >= 1.4
# no longer accepts.
if uri.startswith('postgres://'):
    uri = uri.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'

# Seasonality-aware dashboard (spikes/dashboard): /dashboard.html and
# /dashboard/api/*.  Imported after db exists since its models need it.
from spikes.dashboard.api import blueprint as dashboard_blueprint  # noqa: E402
app.register_blueprint(dashboard_blueprint)


@app.route('/')
def index():
    return redirect(url_for('dashboard.html'))


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('../static', 'favicon.ico')


@app.route('/robots.txt')
def robots():
    return send_from_directory('../static', 'robots.txt')


@app.after_request
def cache_immutable_assets(response):
    """Let browsers keep stable image and font assets without revalidating."""
    if response.status_code in (200, 206, 304) and \
            request.endpoint in ('dashboard.static', 'favicon') and \
            request.path.lower().endswith(IMMUTABLE_ASSET_SUFFIXES):
        response.headers['Cache-Control'] = IMMUTABLE_CACHE_CONTROL
    return response
