# openlaps

Open-source racing telemetry from vehicle to pit: multi-CAN and serial
collection, vehicle-side lap timing, loss-tolerant NATS JetStream transport,
TimescaleDB storage, Grafana dashboards, strategy and alerts.

The car is configuration. Profiles define buses, DBCs, sensors and canonical
channels; hardware targets define the vehicle computer and its device paths.

Read the documentation site:

- [Documentation home](docs/index.md)
- [Getting started](docs/getting-started/index.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Deployment](docs/operations/index.md)
- [Project status](docs/status.md)

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Development

```bash
uv sync --all-extras --extra docs
uv run pytest
uv run ruff check .
uv run mkdocs build --strict
pre-commit install
```
