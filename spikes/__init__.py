# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import Flask, send_from_directory
from flask_cors import CORS, cross_origin
from flask_sqlalchemy import SQLAlchemy
import logging
import os


app = Flask(__name__, template_folder='../templates')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'
log = logging.getLogger(__name__)


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
