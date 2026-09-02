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
PostgreSQL database:

```sh
export DATABASE_URL=postgresql://user:password@localhost/spikes
# First run only: create the table and backfill the last few days.
uv run python -c 'from spikes import models; models.create()'
uv run gunicorn spikes:app
PYTHONPATH=. uv run python bin/schedule.py
```

## Running tests

```sh
uv run ruff check .
uv run coverage run -m unittest discover tests/
uv run coverage report
```

## Deployment

The app runs on Heroku (`heroku-24` stack). The Python buildpack reads the
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
