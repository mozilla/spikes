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
| `api.py` | Flask blueprint (`/dashboard.html`, `/dashboard/api/*`) |
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

| label | condition |
|-------|-----------|
| `major` | z ≥ 8 and ratio ≥ 2 |
| `spike` | z ≥ 5 and ratio ≥ 1.5 |
| `watch` | z ≥ 3 and ratio ≥ 1.25 |
| `drop` | z ≤ −4 and ratio ≤ 0.6 and expected ≥ 20 (cumulative only) |

taken as the worst of the cumulative and the recent score, then gated:

* a signature needs `min_crashes` (per channel, optionally per
  `Product/channel`) over the **last 24 hours**: today so far plus the part
  of yesterday after this hour (from its hourly split), so the per-day
  floor means the same at 06:00 UTC as at 22:00 and does not hide a spike
  that already scores high in the European morning;
* **installs are first class**: one machine crashing a thousand times is
  one machine.  An upward severity also needs at least `min_installs`
  distinct installs over the last 24 hours (today's plus yesterday's scaled
  by the share of its day after this hour, an estimate since installs are
  not additive) *and* an install-based score (`installs` vs
  `expected * install_share`, where `install_share` is the signature's
  usual installs/crashes ratio over the last 28 days) that reaches the
  same level: the final severity is the lower of the two.  A **storm**
  (≤ 5 installs with ≥ 5 crashes each, or ≥ 20 crashes per install) is a
  badge and a count, never an alert.  The channel total is gated the same
  way with the channel's distinct installs, and when ≥ 50 % of a total's
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

## Web

`GET /dashboard.html` and the JSON endpoints described in `API.md`
(`/dashboard/api/summary`, `/channel`, `/signature`).  The page is vanilla
JS with hand-rolled SVG charts: an overview of the six channels with a
cross-channel "flagged in the last 48 h" table, then for the selected channel the KPI
tiles (today so far, projected, yesterday), the drivers, the intraday chart
(hourly bars vs expected, in-progress hour hollow), the daily chart (day or
week granularity, 30-365 days, expected line and bands, severity markers,
release markers), a collapsed explanation of how the expectation is built,
and the sortable signature table (flagged rows by default, sparklines,
expandable per-signature charts).  Data health (stale run, processing lag,
backfill in progress) is shown in a banner.

## Running

```sh
export DATABASE_URL=postgresql://...      # or sqlite:////tmp/dashboard.db
export LIBMOZDATA_CFG_SOCORRO_TOKEN=...   # optional, higher rate limit
uv run python -m spikes.dashboard.update --loop   # backfill, resumable
uv run gunicorn spikes:app                        # then open /dashboard.html
```

`bin/schedule.py` runs `spikes.dashboard.update.run()` every 5 minutes at
:02, :07, ...  The page polls
every 5 minutes; unchanged data costs a `304` and no re-render.  Tests: `uv run python -m unittest
discover tests/` (`test_dashboard_*` use fixtures and an in-memory SQLite
database, no network).
