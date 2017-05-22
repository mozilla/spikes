# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, jsonify
from spikes import models, log, utils


def signatures():
    product = request.args.get('product', 'Firefox')
    product = utils.get_correct_product(product)
    channel = request.args.get('channel', 'nightly')
    channel = utils.get_correct_channel(channel)
    date = request.args.get('date', 'today')
    date = utils.get_correct_date(date)
    signature = request.args.get('signature', '')
    signature = utils.get_correct_sgn(signature)
    log.info('Get signatures for {}::{}, the {}'.format(product,
                                                        channel,
                                                        date))
    return jsonify(models.Signatures.get(product, channel,
                                         date, sgn=signature))
