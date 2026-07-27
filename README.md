# openlaps

A generic, open-source racing telemetry platform: multi-CAN + serial
collectors feed a channel catalog that assigns domain meaning to raw
signals, ship it over NATS JetStream from vehicle to pit, and land it in
TimescaleDB for dashboards and lap analysis. Any specific car — its buses,
DBCs, sensors, and track — is just a configuration profile on top of a
generic core; `profiles/example-club-racer/` is a real one, checked in as
documentation.

This repo is in early bootstrap — design docs are complete; implementation
is next. Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), then:

- [`docs/WIRE_FORMAT.md`](docs/WIRE_FORMAT.md) — subjects, streams, protobuf schema
- [`docs/AGENT_DESIGN.md`](docs/AGENT_DESIGN.md) — vehicle agent internals
- [`docs/CATALOG.md`](docs/CATALOG.md) — configuration profiles and channel naming
- [`docs/LINK_BUDGET.md`](docs/LINK_BUDGET.md) — measured bandwidth vs. radio capacity
- [`docs/adr/`](docs/adr/) — decision records

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
pre-commit install
```
