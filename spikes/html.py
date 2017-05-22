# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from spikes import utils, models, signatures
from flask import request, render_template


def sgns():
    product = request.args.get('product', '')
    product = utils.get_correct_product(product)
    date = request.args.get('date', 'today')
    date = utils.get_correct_date(date)
    channel = request.args.get('channel', '')
    channel = utils.get_correct_channel(channel)
    data = models.Signatures.get(product, channel, date)
    signatures.prepare_for_html(data, product, channel)

    return render_template('signatures.html',
                           product=product,
                           products=utils.get_products(),
                           channel=channel,
                           channels=utils.get_channels(),
                           date=data['date'],
                           dates=models.Signatures.listdates(),
                           data=data)
