# spikes
> Library to detect spikes in data coming from Socorro.

[![CI](https://github.com/mozilla/spikes/actions/workflows/ci.yml/badge.svg)](https://github.com/mozilla/spikes/actions/workflows/ci.yml)

## Setup

The project is managed with [uv](https://docs.astral.sh/uv/). The following
command creates a virtual environment with the Python version from
`.python-version` and installs the dependencies pinned in `uv.lock`:

```sh
uv sync
```

Add `--extra plot` to also install matplotlib, which is only used by the
`datacollector.plot()` debugging helper.

## Running

Both the web app and the scheduler need a `DATABASE_URL` pointing to a
PostgreSQL database. libmozdata is configured through
`LIBMOZDATA_CFG_<SECTION>_<OPTION>` environment variables; the Bugzilla token
is optional and the User-Agent defaults to `crash-clouseau`:

```sh
export DATABASE_URL=postgresql://user:password@localhost/spikes
export LIBMOZDATA_CFG_BUGZILLA_TOKEN=...   # optional
export SECRET_KEY=... GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...  # optional: sign-in with a Mozilla Google account
uv run gunicorn spikes:app                  # serves /dashboard.html
PYTHONPATH=. uv run python bin/schedule.py  # collects the data every 5 minutes
```

The dashboard creates its tables on first use and backfills Socorro's
history over its first runs; see `spikes/dashboard/README.md`.

The spike emails are sent by separate one-shot scripts that query Socorro
directly and do not need the database:

```sh
uv run python -m spikes.signatures -e someone@example.com   # spiking signatures
uv run python -m spikes.startup -e someone@example.com      # startup crashes
```

## Running tests

```sh
uv run ruff check .
uv run coverage run -m unittest discover tests/
uv run coverage report
```

## Deployment

The app runs on Heroku (`heroku-26` stack). The Python buildpack reads the
Python version from `.python-version` and installs the dependencies from
`uv.lock` with `uv sync --locked`, so the lock file must be up to date:

```sh
uv lock --upgrade   # upgrade all dependencies
uv lock --check     # verify the lock file matches pyproject.toml
```

## Bugs

https://github.com/mozilla/spikes/issues/new

## Contact

Email: release-mgmt@mozilla.com
