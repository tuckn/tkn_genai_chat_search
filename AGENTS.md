# AGENTS.md

## Rules of this repository

- This CLI only reads chat logs. It never writes to, moves, locks or indexes anything in an application's
  folder, starts no application, and sends nothing over the network. Only `config init` writes, and only the
  user's own configuration file.
- SQLite title stores are opened with `mode=ro`, and a missing table or column is reported, not assumed.
- Every file found is counted. A file or record that cannot be read is counted in coverage, never dropped
  silently. Raw record text is never written to logs because it may contain private conversation.
- Readers only turn files into the common session and message shape (`models.py`). Filtering, matching,
  ordering and output belong to the shared code (`search.py`, `render.py`).
- The `--json` output is a contract ([docs/cli-output.md](docs/cli-output.md)). Adding a key keeps
  `schema_version`; changing the meaning or type of a key raises it and is recorded in `CHANGELOG.md`.
- README, docs, tests and sample configuration must not contain real personal paths, user names or log
  content. Use placeholders such as `C:\path\to\...` and `C:\Users\ExampleUser\...`.
- Tests use temporary folders and synthetic logs only, never real application logs.
- When a reader changes, update [docs/log-formats.md](docs/log-formats.md) and add a synthetic test for the
  record shape.

## Checks

```shell
uv run python -m ruff format src tests
uv run python -m ruff check .
uv run python -m mypy
uv run python -m pytest
uv build
```

## Test temporary directories

- Prefer pytest's `tmp_path`. If Windows ACLs or the sandbox prevent its use, use a fresh dedicated directory
  under `.tmp/<tool>/<run-id>/` and remove it after the run.
- Keep test paths short: Windows rejects paths longer than 260 characters unless long paths are enabled.
