# openlaps

A generic, open-source racing telemetry platform: multi-CAN + serial
collectors feed a channel catalog that assigns domain meaning to raw
signals, ship it over NATS JetStream from vehicle to pit, and land it in
TimescaleDB for dashboards and lap analysis. Any specific car — its buses,
DBCs, sensors, and track — is just a configuration profile on top of a
generic core; `profiles/example-club-racer/` is a real one, checked in as
documentation. The SBC that profile runs on is configuration too, and a
separate one: `deploy/targets/` describes each supported board — its CAN
interface, its GNSS tty, the encoder its silicon actually has — so moving a
car to different hardware edits no DBC and no channel.

The vehicle side is built — collectors, channel catalog, timing engine and
the JetStream publisher all run — and the pit side is in progress: the
TimescaleDB schema and the ingest-writer have landed, the remaining pit
services and the deploy stacks have not. Start with
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), then:

- [`docs/WIRE_FORMAT.md`](docs/WIRE_FORMAT.md) — subjects, streams, protobuf schema
- [`docs/AGENT_DESIGN.md`](docs/AGENT_DESIGN.md) — vehicle agent internals
- [`docs/CATALOG.md`](docs/CATALOG.md) — configuration profiles and channel naming
- [`deploy/targets/`](deploy/targets/README.md) — the supported vehicle SBCs, and how to add one
- [`docs/PIT_SCHEMA.md`](docs/PIT_SCHEMA.md) — the pit database and its stable read surface
- [`docs/LINK_BUDGET.md`](docs/LINK_BUDGET.md) — measured bandwidth vs. radio capacity
- [`docs/adr/`](docs/adr/) — decision records

Entry points, all configured from the environment
([`example.env`](example.env)):

| Command | Runs |
| --- | --- |
| `openlaps-agent` | The vehicle agent (collectors → catalog → timing → JetStream) |
| `openlaps-migrate` | Applies the pit database schema; a bring-up step, not a service |
| `openlaps-ingest-writer` | The pit's durable consumer: JetStream → TimescaleDB |
| `openlaps-notifier` | Grafana's alert contact point: ledger, annunciator, acknowledgements, phone fan-out |

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
pre-commit install
```
