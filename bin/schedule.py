# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from apscheduler.schedulers.blocking import BlockingScheduler
from spikes import app, models
from spikes.dashboard import update as dashboard_update


sched = BlockingScheduler()


# Both jobs are pinned to the clock (an interval trigger would start at an
# arbitrary minute after each dyno restart): the legacy job at :00, :10, ...
# and the dashboard every 5 minutes at :02, :07, ... so their Socorro
# queries never overlap, and neither runs twice at once.
@sched.scheduled_job('cron', minute='0,10,20,30,40,50', max_instances=1,
                     coalesce=True, misfire_grace_time=120)
def timed_job():
    with app.app_context():
        models.update()


@sched.scheduled_job('cron', minute='2-59/5', max_instances=1,
                     coalesce=True, misfire_grace_time=120)
def dashboard_job():
    with app.app_context():
        dashboard_update.run()


sched.start()
