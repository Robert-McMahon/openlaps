# Development

## Set up

```bash
uv sync --all-extras --extra docs
```

The project requires Python 3.13 or newer. `uv` installs the requested Python
version when necessary.

## Run checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mkdocs build --strict
```

## Preview the documentation

```bash
uv run mkdocs serve
```

Open `http://127.0.0.1:8000`. MkDocs reloads when a Markdown file or the site
configuration changes.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/agent/` | Vehicle process and pipeline |
| `src/collectors/` | CAN, serial and host collectors |
| `src/core/` | Configuration, samples, catalog and protobuf support |
| `src/pit/` | Pit services |
| `profiles/` | Vehicle profiles |
| `deploy/` | Compose, service and host deployment files |
| `docs/` | Documentation site source |
| `tests/` | Unit, integration and deployment contract tests |

Do not commit `.env`, credentials, private keys or generated site output.
