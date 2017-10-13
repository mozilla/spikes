# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from apscheduler.schedulers.blocking import BlockingScheduler
from spikes import models
import logging


logging.basicConfig()
sched = BlockingScheduler()


@sched.scheduled_job('interval', minutes=20)
def timed_job():
    models.update()


sched.start()
