# openlaps

A generic, open-source racing telemetry platform: multi-CAN + serial
collectors feed a channel catalog that assigns domain meaning to raw
signals, ship it over NATS JetStream from vehicle to pit, and land it in
TimescaleDB for dashboards and lap analysis. Any specific car — its buses,
DBCs, sensors, and track — is just a configuration profile on top of a
generic core; `profiles/example-club-racer/` is a real one, checked in as
documentation.

This repo is in early bootstrap. See `docs/ARCHITECTURE.md` (coming in a
follow-up phase) for the full design.

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
pre-commit install
```
