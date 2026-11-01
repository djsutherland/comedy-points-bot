# Comedy Points Bot

A Discord bot for comedy points, reactions, private channel access, and
RSS / YouTube episode announcements.

## Setup and running

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
these commands from the repository root:

```sh
uv sync --locked
uv run --locked main.py
```

Put `DISCORD_TOKEN` and the bot's other configuration in `.env` before starting
the bot. See [logging](docs/logging.md) and [YouTube configuration](docs/youtube-websub.md)
for those integrations. Keep `.env` and the SQLite databases across deployments.

Dependencies are declared in `pyproject.toml` and resolved in the committed
`uv.lock`. `.python-version` selects Python 3.15; uv can download it if needed.
The project environment lives in `.venv` at the repository root. No global
environment activation or `PYTHONPATH` overrides are needed. From this directory,
`./main.py` also runs through uv using the lockfile.

For a service or external startup script, set its working directory to the
repository root and use the absolute path to `.venv/bin/python` to run `main.py`.
For example, substitute your checkout path in this systemd service configuration:

```ini
WorkingDirectory=/path/to/comedy-points-bot
ExecStart=/path/to/comedy-points-bot/.venv/bin/python /path/to/comedy-points-bot/main.py
```

Run `uv sync --locked` after checking out a new deployment, before restarting
the service. Use the repository's `.venv` instead of a shared virtualenv or a
version-specific `site-packages` path.

## Tests

```sh
uv run --locked python -m unittest discover -s tests -v
```

The tests use temporary databases and fake Discord clients; they do not start
the bot or post to Discord.

## Updating dependencies

Use `uv add` / `uv remove` to change dependencies. To update the locked versions
within the declared constraints:

```sh
uv lock --upgrade
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
```

Commit `pyproject.toml` and `uv.lock` together when dependencies change.
