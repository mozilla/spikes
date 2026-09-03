# Crash-spikes dashboard

`spikes/dashboard/` is a self-contained package that collects crash counts
from Socorro, models their seasonality, scores what is happening *right now*
against what was expected, and serves `dashboard.html`.  It is independent
from the spike emails (`spikes/signatures.py`, `spikes/startup.py`); the only
integration points are the Flask blueprint registered in `spikes/__init__.py`
and the job in `bin/schedule.py`.

## What it answers

For every (product, channel) pair configured in `config/dashboard.json`
(Firefox nightly, beta, release and ESR; Fenix nightly, beta and release;
Thunderbird nightly, beta, release and ESR) and every signature with
meaningful volume:

* Is the volume seen **today so far**, or in the **last few hours**, higher
  or lower than the seasonal pattern predicts for this weekday and time of
  day?  How big is the deviation (excess, ratio, z-score) and what does it
  project to at the end of the day?
* Which signatures **drive** a channel-level deviation?
* Is a signature **new**?  Is it a **storm** from a handful of installs?  Is
  it known **noise** (`config/skiplist.json`)?

Measured on real data (28 days ending 2026-09-01): Firefox release has a
weekday pattern (Mon x1.13 ... Sat x0.79, Sun x0.77), Fenix release almost
none, and every channel has a strong hour-of-day pattern (release: 2.7 % of
the day's crashes at 23:00 UTC vs 5.6 % at 14:00; beta peaks at 07:00 with a
4.5x peak/trough ratio).  The cumulative arrival curve is very stable on
release/beta (relative sd 6 % at 03:00 UTC, 2 % at 09:00, < 1 % at 18:00)
and bursty on nightly (11 of 28 days had an hour with 3x its usual share).
Both patterns are removed before anything is compared, so the Monday bump
and the afternoon peak are not spikes.

## Layout

| file | role |
|------|------|
| `config.py`, `config/dashboard.json` | all tunables |
| `socorro.py` | query shapes, response parsers, paced execution |
| `collect.py` | fetch planner (what is missing / not final) and writers |
| `models.py` | tables `dashboard_*`, portable upserts |
| `seasonal.py` | daily seasonal model and scoring math |
| `intraday.py` | hour-of-day arrival profiles |
| `scoring.py` | per-channel scoring (today, yesterday, drivers) |
| `update.py` | one scheduler run; `python -m spikes.dashboard.update` |
| `events.py` | platform events (Windows updates, drivers, OS releases) fetched by the scheduler, badges on the charts |
| `api.py` | Flask blueprint (`/dashboard.html`, `/dashboard/api/*`) |
| `auth.py` | sign-in with a Mozilla Google account (`/dashboard/login`, `/dashboard/logout`, `/dashboard/api/me`) and the `login_required` guard for routes that change something |
| `API.md` | JSON contract used by the page |
| `templates/`, `static/` | the page (vanilla JS, SVG charts, no build) |

## Data collection (Socorro SuperSearch)

The SuperSearch `date` field is the collector receipt time of a report
(`date_processed`); the processor may index a report later, so *past*
buckets can still grow for a while.  Crashes submitted from the infobar or
`about:crashes` are excluded (`exclude_submitted_from`) because they are
bulk uploads of old crashes.  The same filters are used for the crash-stats
links so the numbers match what the user sees there.

One query gives everything for a (product, channel, day):

```
_histogram.date=signature&_histogram_interval.date=1h
&_aggs.signature=_cardinality.install_time&_facets_size=1000&_results_number=0
```

plus `_aggs.product=_cardinality.install_time` and a second
`_histogram.date=_cardinality.install_time`, i.e. exact hourly channel
totals and distinct installs per hour, the hourly split of every signature
(at most ~400 distinct signatures per hour on Firefox release, far below
the 1000 cap, so the split is complete: hourly sums equal day counts), the
exact day count + distinct installs of the top 1000 signatures, and the
channel's distinct installs for the day (~0.8 MB, < 1 s).  History is backfilled with `_histogram_interval.date=1d` over
14-day chunks (per-day counts of the top 1000) plus an hourly histogram of
the channel total.

Query budget:

* **steady state, every 5 minutes**: one small `recent` query per channel
  (the hourly histogram from one hour before the previous fetch to now,
  ~150 KB on Firefox release instead of ~800 KB for the whole day) whose
  buckets replace the stored hours; today's counts are the sums of the
  hourly arrays.  Every `installs_refresh_minutes` (30) the full `day`
  query runs instead: distinct installs are not additive, and a full pass
  also picks up reports the processor indexed late into an earlier hour.
  Signatures first seen by a `recent` window get the window's distinct
  installs until then.  Yesterday is refetched with the full `day` query
  until it is *final*.  That is 7 queries per run for 7 channels, ~1 MB
  per run on average.
* **backfill / catch-up**: `collect.plan` lists the days that are missing or
  not final and fetches them within `max_queries_per_run` (60) and
  `max_run_seconds` (420), interleaving channels so all of them progress.
  The initial 6 months (Socorro's retention) are ~200 queries, i.e. 3-4
  runs.  Gaps after downtime are refilled the same way.
* Requests go through a `SuperSearch` subclass with a bounded worker pool
  and 6 retries (libmozdata ignores those as keyword arguments and would
  otherwise retry 256 times), paced to at most `60 / min_seconds_per_query`
  requests per minute.  Set `LIBMOZDATA_CFG_SOCORRO_TOKEN` for the higher
  authenticated rate limit.  Response handlers only parse JSON; all database
  work happens afterwards, one transaction per unit with the bookkeeping
  row written last, so a killed dyno never leaves a day that looks complete.

A day is **final** when it was fetched at least `final_grace_hours` (6)
after its end and its total did not change since the previous fetch (days
older than the recent window are final on first fetch; a day left non-final
by an outage is fetched once more).  Units skipped for budget or deadline
stay pending and the run reports them (`pending_units`), which the page
shows as "backfilling"; fits made while history is still incomplete are
marked stale so they are redone on the next run.  Signatures below
the day's top 1000 are not stored; the count of the 1000th is kept as the
day's `cutoff`, and a missing (series, day) value is treated as censored
(`cutoff / 2`, 1-2 crashes in practice) rather than as zero.

## Storage

| table | row | notes |
|-------|-----|-------|
| `dashboard_series` | (product, channel, signature) | + first/last seen, `noise`, cached bug ids; the channel total is the series with signature `''` |
| `dashboard_daily` | (series, day) → crashes, installs | |
| `dashboard_hourly` | (series, day) → 24 counts | separate table so retention is a range delete |
| `dashboard_days` | (product, channel, day) | fetch bookkeeping: total, previous total, cutoff, `as_of`, `final`, `complete` |
| `dashboard_models` | series → cached fit | level, trend, dispersion, `c2`, factors, borrowed components |
| `dashboard_scores` | (series, day) → live score | updated in place; keeps `first_flagged_at` and the day's peak |
| `dashboard_runs` | one per run | status (`ok`, `partial`, `failed`, `aborted`), queries, message (pending work, errors) |

`models.create_all()` (called by every run and by the web process before
its first request) creates missing tables and adds columns that were added
to the models since, so additive schema changes deploy without a manual
`ALTER TABLE`.

Retention (`update.maybe_prune`, once a day after 03:00 UTC): daily rows
with < 3 crashes after 120 days and < 10 crashes after 365 days are deleted,
signature hourly splits after 60 days, scores after 30 days.  The channel
totals (daily and hourly), the `dashboard_days` bookkeeping (needed to
censor missing signature days) and signature days with ≥ 10 crashes are
kept indefinitely.  Growth is then a few tens of MB per year on the
`essential-0` plan while the long-term history the yearly component needs
is kept (the totals are what it is estimated on).

## Detection

### Daily model (`seasonal.py`)

```
y[t] ~ level[t] * weekly[weekday(t)] * cycle[t mod 28] * yearly[week(t)]
```

* Factors are **medians** of ratios to a centred rolling median (window =
  one period), estimated by back-fitting; medians keep the spikes we are
  detecting out of the baseline.  Each factor is shrunk toward 1 with an
  Efron-Morris weight `max(0, 1 - noise / spread)`: a component whose
  phases differ no more than the noise is flattened (Fenix release has no
  weekday pattern and gets none), a strong pattern is kept.
* Components activate with enough history: weekly ≥ 3 weeks, 28-day cycle
  (the release train) ≥ 3 cycles, yearly ≥ 2 years.  Socorro only keeps 6
  months (`history_days` = 180 is the backfill horizon), but the database
  keeps accumulating: the channel totals are fitted on up to
  `fit_history_days` (1100, three years) of stored history, so the yearly
  component reads "not enough history" until two years have accumulated
  (~2028-09) and then activates by itself; the API reports this.
  Signatures are fitted on 180 days and borrow the yearly factors from
  their channel.
* `28 = 4 * 7` makes the cycle and weekday components non-identifiable, so
  the cycle factors are constrained to carry no weekday effect.
* **Signatures borrow the channel's factors**: a signature only estimates
  its own factors once it has `own_factors_min_crashes` on enough days, and
  then in log space between the channel's and its own with weight
  `n / (n + 2)` cycles.  A two-week-old regression therefore gets the
  channel's Monday/Sunday correction from day one.
* The **level** is a robust local-linear forecast (Theil-Sen slope over
  the last 14 de-seasonalised days, clipped to ±10 %/day, ignored under
  `trend_min_level`), so a rollout ramp is followed without lag while up to
  six anomalous days are ignored.  `level_change_28` (level now vs four
  weeks ago) is reported so a persistent regression stays visible after the
  level has absorbed it.
* Residuals use the Anscombe transform `2 (sqrt(y + 3/8) - sqrt(e + 3/8))`
  (~N(0, 1) for Poisson).  Real counts are over-dispersed with relative
  variance `c2` (`Var = e + c2 e^2`, measured ≈ 0.02 on release
  signatures), estimated from the one-step-ahead residuals and shrunk
  toward the channel's, so the score of an observation is
  `anscombe / sqrt(1 + c2 e)`: the scale grows with the expectation, which
  matters intraday when `e` ranges from 20 to the full day.  Under `e < 10`
  the exact mid-p negative-binomial tail is used instead.
* The same machinery gives the expected value and `±3` / `±5` bands for
  every historical day (and for weekly sums, with the variance of the sum,
  over the days that have data), which is what the charts draw.

### Intraday (`intraday.py`, `scoring.py`)

* From the channel's exact hourly totals of the last 28 days a cumulative
  arrival profile `F(h)` is estimated (element-wise median, burst days with
  an hour > 3x its usual share excluded), per weekday (last 8 same weekdays,
  shrunk toward the all-days curve), together with its day-to-day relative
  variance `vF(h)`.  Signatures use the channel profile: the top
  signatures' hourly counts match it within Poisson noise.
* `expected_sofar = E_day * F(as_of)` where `as_of` is the fetch time, not
  the scoring time.  For signatures the calendar fraction is blended
  (`pace_blend` = 0.5) with the channel's *realised* pace (median of
  `observed / E_day` over the top 50 signatures, used only when at least
  `pace_min_signatures` exist and clamped to 0.5-1.5x the calendar
  share), which makes them robust to a Socorro processing lag or
  catch-up; the channel total itself is scored against the pure seasonal
  forecast.
* `z = score(observed, expected_sofar, c2 + vF)`; `projected =
  observed / F` (with a ±2 scale range) once 25 % of the day is in.
* **Recent window**: the shortest of 3 / 6 / 12 hours with an expected count
  ≥ 10, taken from the hourly splits of today *and* yesterday (so it works
  across midnight; the bucket containing `as_of` is taken in full, the one
  containing the window start pro rata).  When no window qualifies the API
  says why (`recent_reason`).
* **Drivers**: the five signatures with the largest excess in the direction
  of the total's deviation, with their share of it (noise signatures are
  listed but flagged).

### Severity

No hand-set threshold: every one is learned from the channel's own data
(`calibration.py`; the page's **?** button shows the current values).

| label | condition |
|-------|-----------|
| `major`, `spike`, `watch` | z ≥ the channel's threshold for the level |
| `drop` | z ≤ the channel's drop threshold and expected ≥ the volume floor (cumulative only) |

The thresholds are quantiles of the channel's one-step-ahead z-scores,
pooled over its scored signatures (each fit caches a histogram of its z
with the model).  The config only states the false-alarm rate per
signature and day each level may have (`alert_rate`: watch 1.5 %, spike
0.15 %, major 0.015 %, drop 0.15 % in the lower tail): with 200
signatures, watch is three false flags a day.  Measured on real data the
tails are far heavier than Gaussian and differ by channel (z ≥ 3 on 2.9 %
of the series-days on Firefox release, 5.5 % on Fenix release), so a fixed
z would mean different things per channel; the learned bar is higher where
the residuals are noisier.  The Gaussian value for the same rate is a floor
(real tails are never lighter) and is used outright under 300 pooled
series-days; a level whose tail holds fewer than 5 points is extrapolated
with an exponential tail fitted on the top of the sample.  The ratio gates
of the first version are gone: the over-dispersion term of the score
already grows with the count.  The severity is the worst of the cumulative
and the recent score, then gated:

* **volume floor**: a signature needs `volume_share` (0.1 %) of the
  channel's expected day in crashes (at least 2) over the **last 24
  hours**: today so far plus the part of yesterday after this hour (from
  its hourly split), so the floor means the same at 06:00 UTC as at 22:00
  and does not hide a spike that already scores high in the European
  morning;
* **installs are first class**: one machine crashing a thousand times is
  one machine.  An upward severity also needs half the crash floor in
  distinct installs over the last 24 hours (today's plus yesterday's scaled
  by the share of its day after this hour, an estimate since installs are
  not additive) *and* an install-based score (`installs` vs
  `expected * install_share`, where `install_share` is the signature's
  usual installs/crashes ratio over the last 28 days) that reaches the
  same level: the final severity is the lower of the two.  A **storm** is a
  signature whose crashes per install exceed the `storm_quantile` (99.5 %)
  quantile of the channel's own signatures over the last 4 weeks: a badge
  and a count, never an alert.  The channel total is gated the same way
  with the channel's distinct installs, and when ≥ 50 % of a total's
  excess comes from storm signatures it is marked `storm_driven` and not
  reported as a spike;
* **hysteresis**: a severity only steps down once z is one unit under its
  threshold; `first_flagged_at` and the day's peak are kept;
* **flag window** (`api.flag_of`, `flag_window_hours` = 48): scores are
  per UTC day, so at 00:00 UTC every live severity restarts from scratch
  and the page would be empty for the European morning.  A row is
  therefore *shown* with the worst state it reached today or on a previous
  day, for 48 hours after the last run that flagged it (`last_flagged_at`;
  drops and `new` included), marked "yesterday" with that day's numbers.
  Yesterday's spikes stay listed until the live scoring takes over or the
  window closes, and a reader in any timezone sees the same list; a spike
  that lasts longer is re-flagged by the live scores until the model has
  absorbed it as the new level (it is then a trend, not a spike, and
  `level_change_28` keeps it visible);
* **lag guard** (`update.lag_guard`): when at least 4 of the 6 channel
  totals are < 70 % of expectation or have `z_recent ≤ −3` at once,
  Socorro is late, not Firefox: drops are suppressed for the run and the
  page shows a banner;
* `new` (no day above the cut in the previous 14 days), `storm` and `noise`
  are badges orthogonal to the severity.

Fits are cached in `dashboard_models` and refreshed oldest-first when older
than `refit_hours` (6), at most `max_fits_per_run` per run, so the
10-minute run only recomputes the cheap score formula.  Yesterday is scored
as a complete day; bugs (Socorro `Bugs` + Bugzilla status) are fetched for
flagged signatures and cached on the series for 12 hours.

## Platform events

Crash volumes move when the platform under Firefox moves.  `events.py`
fetches public feeds *in the scheduler* (never in a page request), stores
them in `dashboard_events` and the page shows them as icon badges above the
"Today by hour" and "Daily crashes" charts, with a tooltip saying what
happened that day (hover or focus; a click opens the crash-stats search or
the release notes).  Firefox and Thunderbird get the Windows, macOS and Linux
events, Fenix the Android ones.

| badge | source | what |
|-------|--------|------|
| Windows | DataForNerds' machine-readable copy of Microsoft's update-history pages | every KB with its OS build and type (Patch Tuesday, preview, out-of-band, hotpatch) |
| NVIDIA | the GeForce download page's lookup endpoint (unofficial, unchanged for years) | Game Ready drivers with a crash-stats link on the `adapter_driver_version` string they report as (`616.56` is `*.6.1656`) |
| NVIDIA, AMD, Intel | Socorro itself: one SuperSearch query per vendor, the daily counts of every `adapter_driver_version` on Firefox release, Windows, over 45 days | a driver version *appears in crash reports*: dated the day it first shows up (0.2 % of its vendor's crashes after at least five days without it), once it has reached 1 % (≥ 20 crashes) within two weeks and held half of that the next day. An established version never qualifies, nor does a one-day blip from a crash-looping machine. Covers drivers shipped by Windows Update and OEMs, which no vendor feed does; never moved once stored. NVIDIA strings get their GeForce name (`32.0.16.1656` is `616.56`) |
| Apple | MacAdmins SOFA feed | macOS security releases, with the CVE count |
| Linux | endoflife.date (kernel series, Ubuntu, Fedora), freedesktop GitLab tags (Mesa) | releases, no release candidates |
| Android | endoflife.date (major versions); the monthly security bulletin is computed, it has no feed | published on the first Monday of the month |
| Antivirus (one shield badge for all vendors) | Norton Community announcements RSS (the monthly "Norton Security N for Windows" posts); Chocolatey packages for Avast and Malwarebytes (published a day or two after the release); winget-pkgs commits for ESET; the Defender updates page for the Microsoft Defender platform version (dated when first seen, never moved) | Kaspersky, McAfee and Bitdefender publish nothing machine-readable and are absent |

Feeds are fetched in parallel with a 15 s timeout (60 s for the GeForce
lookup, which takes tens of seconds), every `events_refresh_hours` (6; a
failed feed is retried after `events_retry_hours`), and the run reports
them (`events` in the run message).  A dead feed keeps its previous rows.  `GET /dashboard/api/events`
serves everything grouped per day and source, a few KB gzipped, with an
ETag that only changes when a refresh wrote something: the page fetches it
once and its polls cost a 304.  Rows older than `events_retention_days`
(800) are pruned.

## Web

`GET /dashboard.html` and the JSON endpoints described in `API.md`
(`/dashboard/api/summary`, `/channel`, `/signature`).  The page is vanilla
JS with hand-rolled SVG charts: an overview of the six channels with a
cross-channel "flagged in the last 48 h" table, then for the selected channel the KPI
tiles (today so far, projected, yesterday), the drivers, the intraday chart
(hourly bars vs expected, in-progress hour hollow), the daily chart (day or
week granularity, 30-365 days, expected line and bands, severity markers,
release markers, and the forecast up to the next release: the expected
path and its bands continue past today over a shaded zone, with a damped
trend so a rollout ramp is not extrapolated forever, redone with every
fit; the next release comes from whattrainisitnow, the merge day for
nightly, two weeks when unknown), a collapsed explanation of how the expectation is built,
and the sortable signature table (flagged rows by default, sparklines,
expandable per-signature charts).  Data health (stale run, processing lag,
backfill in progress) is shown in a banner.

## Sign-in

Reading the dashboard needs no account.  Routes that change it are wrapped
in `auth.login_required` and only run for a signed-in user whose Google
account has a verified address in one of `login_domains`
(`config/dashboard.json`, `mozilla.com` by default), the same way
hackbot.moz.tools is restricted to `@mozilla.com` accounts.  The header
shows a **Sign in** link, or the signed-in user and a **Sign out** button.

The flow is OpenID Connect against Google through Authlib (state, nonce and
ID-token signature checks); the `hd` parameter pre-selects the Mozilla
account in Google's chooser but the check is the `email_verified`, `email`
and `hd` claims of the ID token, at the callback *and* on every request
(a change of `login_domains` cuts existing sessions).  The session is the
signed Flask cookie (`HttpOnly`, `SameSite=Lax`, `Secure` on Heroku, 7
days): nothing is stored server-side.  Writes also refuse a request whose
`Origin` or `Sec-Fetch-Site` header names another site.  Signed-out calls
to a guarded route get `401 {"error": "sign-in required", "login": ...}`.

Setup, once, in the Google Cloud console (APIs & Services → Credentials →
OAuth client ID, type *Web application*; a consent screen of type
*Internal* additionally keeps non-Mozilla accounts from even seeing it):
authorized redirect URI `https://crash-spikes.herokuapp.com/dashboard/login/callback`
(and `http://localhost:5000/dashboard/login/callback` for a local run).
Then:

```sh
heroku config:set -a crash-spikes \
  SECRET_KEY=$(openssl rand -hex 32) \
  GOOGLE_CLIENT_ID=....apps.googleusercontent.com \
  GOOGLE_CLIENT_SECRET=...
```

Without `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` the sign-in routes
answer 503 and the page hides the link; without `SECRET_KEY` a random one
is used and sessions end with the process.  On Heroku the app trusts one
`X-Forwarded-Proto` hop (`ProxyFix`, only when `DYNO` is set) so the
redirect URI and the cookie flags say https.

## Running

```sh
export DATABASE_URL=postgresql://...      # or sqlite:////tmp/dashboard.db
export LIBMOZDATA_CFG_SOCORRO_TOKEN=...   # optional, higher rate limit
export SECRET_KEY=... GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...  # optional, sign-in
uv run python -m spikes.dashboard.update --loop   # backfill, resumable
uv run gunicorn spikes:app                        # then open /dashboard.html
```

`bin/schedule.py` runs `spikes.dashboard.update.run()` every 5 minutes at
:02, :07, ...  The page polls
every 5 minutes; unchanged data costs a `304` and no re-render.  Tests: `uv run python -m unittest
discover tests/` (`test_dashboard_*` use fixtures and an in-memory SQLite
database, no network).
