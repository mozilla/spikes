# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import Flask, send_from_directory
from flask_cors import CORS, cross_origin
from flask_sqlalchemy import SQLAlchemy
from libmozdata import config as lmdconfig
from libmozdata.config import ConfigEnv
import os


# libmozdata settings come from LIBMOZDATA_CFG_<SECTION>_<OPTION> environment
# variables (e.g. LIBMOZDATA_CFG_BUGZILLA_TOKEN). This must run before any
# other libmozdata module is imported, since they read the config on import.
lmdconfig.set_config(ConfigEnv())
lmdconfig.set_default_value('User-Agent', 'name', 'crash-clouseau')

app = Flask(__name__, template_folder='../templates')

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


@app.route('/signatures', methods=['GET'])
@cross_origin()
def signatures_rest():
    from spikes import api
    return api.signatures()


@app.route('/')
@app.route('/signatures.html')
def signatures_html():
    from spikes import html
    return html.sgns()


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('../static', 'favicon.ico')


@app.route('/spikes.js')
def spikes_js():
    return send_from_directory('../static', 'spikes.js')


@app.route('/spikes.css')
def spikes_css():
    return send_from_directory('../static', 'spikes.css')
