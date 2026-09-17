# Reference

Reference pages define data and configuration contracts. The implementation
files listed below remain normative when prose and code disagree.

| Topic | Documentation | Normative implementation |
| --- | --- | --- |
| Channels and profiles | [Catalog](../CATALOG.md) | profile YAML and `src/core/config.py` |
| Telemetry messages | [Wire format](../WIRE_FORMAT.md) | `proto/telemetry.proto` |
| Pit storage | [Pit database](../PIT_SCHEMA.md) | `src/pit/db/migrations/` |
| Raw logging | [Raw capture](../RAW_CAPTURE.md) | collectors and capture tools |
| Capacity | [Link budget](../LINK_BUDGET.md) | model plus dated bench evidence |

Service commands are declared in `pyproject.toml`; deployed topology and ports
are declared in the Compose files.
