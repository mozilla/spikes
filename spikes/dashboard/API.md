# Dashboard JSON API contract

All endpoints are `GET`, return `application/json`, and are mounted under
`/dashboard/api/`.  Timestamps are ISO-8601 UTC strings ending with `Z`;
days are `YYYY-MM-DD` (UTC).  Numbers may be `null` when not computable.

Every response carries a `data_version` string (also its `ETag`) that only
changes when a scheduler run has produced new scores; a request with a
matching `If-None-Match` gets `304 Not Modified`, and the page skips its
re-render when the version it holds is unchanged.  Responses are gzipped
when the client accepts it.

Severities (ordered worst first): `major`, `spike`, `watch`, `drop`, `ok`.
`new`, `storm` and `noise` are orthogonal boolean badges on a row.

## Shared objects

### Score

```jsonc
{
  "day": "2026-09-02",
  "as_of": "2026-09-02T13:42:10Z",   // when the counts were fetched
  "partial": true,                    // day still in progress
  "elapsed_fraction": 0.57,           // F(as_of): share of the day's crashes usually in by now
  "observed": 8410,                   // crashes so far (partial) or for the day
  "expected": 8200.5,                 // expected so far (== expected_day when not partial)
  "expected_day": 21000.0,            // expected full day
  "excess": 210,                      // observed - expected (rounded)
  "ratio": 1.03,                      // observed / expected (null when expected == 0)
  "z": 1.2,                           // score, null if not scorable
  "confidence": 0,                    // 0..3 = number of thresholds (3/5/8 in |z|) passed
  "projected": 22100.0,               // observed / F(as_of); null when elapsed_fraction < 0.25
  "projected_lo": 21500.0, "projected_hi": 22800.0,
  "recent": {                         // last few hours; null only without hourly data
    "hours": 3, "observed": 1200, "expected": 1100.0, "z": 0.9,   // z null when the
    "excess": 100, "ratio": 1.09                                  // window is too small to score
  },
  "recent_reason": null,              // why z is null, e.g. "quiet: 3 observed, 2.4 expected (12h)"
  "installs": 5200,                   // distinct installs so far, null if unknown
  "expected_installs": 5100.0,        // expected installs so far (null without history);
                                      // assumes the day's usual installs/crashes ratio, which
                                      // understates distinct installs early in the day
  "z_installs": 0.4,                  // score of the install count; an upward severity
                                      // requires it to deviate too (one machine is one machine)
  "installs_ratio": 1.6,              // observed / installs
  "storm": false,                     // very few installs or >= 20 crashes per install:
                                      // a badge, never an alert (severity stays ok)
  "severity": "ok",                   // today's live state; the per-day floors (min crashes /
                                      // installs) are checked on the last 24 hours
  "is_new": false,                    // not seen above the cut in the previous 14 days
  "noise": false,                     // matches config/skiplist.json (never alerts)
  "since": null,                      // first time today the severity was >= watch
  "peak": null,                       // {"severity": "spike", "z": 7.1, "excess": 770, "at": "..."} while today
  "level": 20400.0,                   // de-seasonalised daily level
  "dispersion": 1.7,                  // robust scale of the residuals at expected_day
  "level_change_28": 1.05,            // level now / level 4 weeks ago, null if unknown
  "drivers": [                        // channel totals only: what explains the excess
    {"signature": "...", "excess": 700, "share": 0.89, "severity": "major",
     "noise": false, "installs": 4, "storm": true}
  ],
  "storm_share": 0.9,                 // totals only: share of the excess from storms
  "storm_driven": true                // totals only: >= 50 % from storms -> severity ok
}
```

### Row (a signature in a channel)

A `Score` plus:

```jsonc
{
  "signature": "libc.so.6 | cuEGLApiInit",
  "product": "Firefox", "channel": "nightly",   // present in cross-channel lists
  "series_id": 12,
  "socorro_url": "https://crash-stats.mozilla.org/search/?...",
  "bugs": {"open": 1234567, "closed": null},     // most recent bug ids, may be null
  "first_seen": "2026-09-01",
  "flagged_days": 2,                              // consecutive previous days with peak >= watch
  "yesterday": {"observed": 120, "expected": 98.0, "z": 1.1, "severity": "ok",
                "final": true},                   // or null
  "spark": {"dates": ["2026-08-06", "..."], "observed": [1, 2, 3], "expected": [1.1, 2.0, 2.9]},  // 28 days
  "flag": {                                       // what the row is shown as, or null:
    "severity": "major", "is_new": false,         // today's live state, or the worst state a
    "day": "2026-09-01",                          // previous day reached, kept for
    "since": "2026-09-01T09:12:00Z",              // `flag_window_hours` after the last run
    "at": "2026-09-01T23:57:00Z",                 // that flagged it (so nothing vanishes at
    "observed": 430, "expected": 98.0,            // 00:00 UTC); the numbers are that day's
    "z": 12.0, "excess": 332,
    "peak": null                                  // {"severity", "z", "excess", "at"} when the
  }                                               // day stepped down from a higher severity
}
```

Lists, counts and sort orders use `flag` (a row whose `flag.day` is not
`day` carries yesterday's spike into today); `severity` and `is_new` stay
today's own state.

### Series block (charts)

```jsonc
"daily": {
  "granularity": "day",               // or "week" (buckets start on Monday)
  "start": ["2026-06-04", "..."],     // bucket start day
  "observed": [1, 2],                 // null for unknown days
  "expected": [1.1, 2.0],
  "lo3": [], "hi3": [],               // band at +-3 dispersions (watch threshold)
  "lo5": [], "hi5": [],               // band at +-5 (spike threshold)
  "z": [],
  "partial": [false, true],           // bucket still in progress
  "projected": [null, 22100.0],       // for partial buckets only
  "severity": ["ok", "spike"]         // per bucket, from z (no ratio gate for history)
},
"hourly": {
  "hours": [0, 1, "...", 23],
  "today": [638, 670],                // per UTC hour, null for future hours
  "yesterday": [601, 640],
  "expected_today": [640.0, 655.0],   // expected_day * hourly share
  "expected_yesterday": [],
  "in_progress_hour": 13,             // null when the day is complete
  "profile_source": "channel"         // or "own"
},
"model": {
  "level": 20400.0, "dispersion": 1.7, "c2": 0.0185, "history_days": 180,
  "components": {"weekly": {"active": true, "cycles": 25.7, "min_cycles": 3},
                 "cycle": {"active": true, "cycles": 6.4, "min_cycles": 3},
                 "yearly": {"active": false, "cycles": 0.49, "min_cycles": 2}},
  "factors": {"weekly": [1.13, 1.09, 1.10, 1.08, 1.03, 0.79, 0.77],
              "cycle": [/* 28 values */]},
  "today_factors": {"weekly": 1.13, "cycle": 0.97},
  "cycle_day": 12,                    // day 1..28 of the 28-day cycle
  "borrowed": ["weekly", "cycle"]     // components taken from the channel (signatures)
}
```

## Endpoints

### `GET /dashboard/api/summary`

```jsonc
{
  "now": "2026-09-02T13:49:00Z",
  "as_of": "2026-09-02T13:42:10Z",    // most recent data across channels
  "last_run": {"started": "...", "finished": "...", "status": "ok",   // ok|partial|failed|aborted|running
               "queries": 12, "failures": 0, "message": null, "lag_suspected": false},
  "data_health": {"status": "ok",     // ok|stale_local|stale_upstream|backfilling
                  "since": null, "detail": "..."},
  "thresholds": {"watch": {"z": 3, "ratio": 1.25}, "spike": {"z": 5, "ratio": 1.5},
                 "major": {"z": 8, "ratio": 2}, "drop": {"z": -4, "ratio": 0.6}},
  "flag_window_hours": 48,            // how long a previous day's flag stays listed
  "channels": [
    {"product": "Firefox", "channel": "release", "day": "2026-09-02",
     "as_of": "...", "history_days": 180,
     "total": Score, "yesterday": Score,           // yesterday may be null
     "counts": {"major": 1, "spike": 2, "watch": 5, "drop": 0, "new": 3,
                "storm": 1, "scored": 312, "noise": 4}}
  ],
  "alerts": [Row],                    // union of flagged rows across channels, <= 50,
                                      // sorted by severity rank then excess
  "releases": [{"date": "2026-08-19", "version": "146.0"}]   // Firefox major releases
}
```

### `GET /dashboard/api/channel?product=Firefox&channel=release&days=90&granularity=day`

```jsonc
{
  "product": "Firefox", "channel": "release", "day": "2026-09-02", "as_of": "...",
  "total": Score, "yesterday": Score,
  "daily": SeriesBlock.daily, "hourly": SeriesBlock.hourly, "model": SeriesBlock.model,
  "signatures": [Row],                // every scored row (flagged and not)
  "counts": {...}, "thresholds": {...},
  "releases": [...],                  // major releases; ESR point releases ("140.15.0esr") for channel esr
  "data_health": {...}
}
```

`days` in 7..730 (default 90); `granularity` `day` (default) or `week`.

### `GET /dashboard/api/events?days=730`

Platform events of the last `days` days (1..800), grouped per day and
source, oldest first.  Read from the database (the scheduler fetches the
feeds); the `ETag` only changes when a refresh wrote something.

```jsonc
{
  "since": "2024-09-03",
  "events": [
    {"day": "2026-08-26", "source": "nvidia",          // windows | nvidia | apple | linux | android
     "platform": "windows",                            // windows | mac | linux | android (which products show it)
     "label": "NVIDIA driver", "at": null,             // at: earliest known time of the day's items
     "items": [{"kind": "nvidia-driver", "title": "GeForce Game Ready Driver 616.56",
                "detail": "WHQL, adapter_driver_version *.6.1656 in crash reports",
                "url": "https://www.nvidia.com/en-us/drivers/details/278153/",
                "search": "https://crash-stats.mozilla.org/search/?...",   // may be null
                "at": null}]}
  ],
  "feeds": {"windows-updates": {"fetched_at": "...", "ok": true, "items": 212, "message": null}},
  "data_version": "events-612-2026-09-03T06:52:10Z-730"
}
```

### `GET /dashboard/api/signature?product=&channel=&signature=&days=90&granularity=day`

```jsonc
{"row": Row, "daily": {...}, "hourly": {...}, "model": {...}, "releases": [...]}
```

404 when the signature is unknown for that channel.

### Errors

`400 {"error": "..."}` for invalid parameters.
