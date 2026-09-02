# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from apscheduler.schedulers.blocking import BlockingScheduler
from spikes import app
from spikes.dashboard import update as dashboard_update


sched = BlockingScheduler()


# Pinned to the clock (an interval trigger would start at an arbitrary
# minute after each dyno restart) and never running twice at once.
@sched.scheduled_job('cron', minute='2-59/5', max_instances=1,
                     coalesce=True, misfire_grace_time=120)
def dashboard_job():
    with app.app_context():
        dashboard_update.run()


sched.start()
