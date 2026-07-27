"""Build hot-path source mappings and protobuf channel registries."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from core.config import ChannelConfig, ConfigError, ProfileConfig, RbeConfig, load_profile
from core.pb import telemetry_pb2 as pb

DERIVED_ID_START = 1 << 31
_STATE_FILE_NAME = ".registry-state.json"

_VALUE_TYPES = {
    "double": pb.DOUBLE,
    "int": pb.INT64,
    "bool": pb.BOOL,
    "string": pb.STRING,
    "float": pb.FLOAT,
    "uint": pb.UINT,
}


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    """Pipeline policy attached to a mapped channel."""

    name: str
    value_type: int
    rbe: RbeConfig | None
    live_hz: float | None
    scale: float = 0.0
    offset: float = 0.0


@dataclass(frozen=True, slots=True)
class DerivedChannel:
    """A channel emitted inside the agent rather than by a collector."""

    name: str
    value_type: int
    units: str = ""


DEFAULT_DERIVED_CHANNELS = (
    DerivedChannel("lap.event", pb.STRING),
    DerivedChannel("lap.number", pb.INT64),
    DerivedChannel("lap.sector", pb.INT64),
    DerivedChannel("lap.last_time", pb.DOUBLE, "s"),
    DerivedChannel("lap.best_time", pb.DOUBLE, "s"),
    DerivedChannel("timing.delta_best", pb.DOUBLE, "s"),
    DerivedChannel("timing.predicted_lap", pb.DOUBLE, "s"),
    DerivedChannel("timing.distance", pb.DOUBLE, "m"),
)


@dataclass(frozen=True, slots=True)
class RuntimeCatalog:
    """Immutable startup product used by the mapper and batcher."""

    source_map: dict[str, tuple[int, ChannelPolicy]]
    channel_ids: dict[str, int]
    policies_by_id: dict[int, ChannelPolicy]
    registry: pb.ChannelRegistry
    catalog_hash: str


def build_runtime_catalog(
    profile: ProfileConfig | str | Path,
    *,
    state_path: str | Path | None = None,
    created_unix_ms: int | None = None,
    derived_channels: tuple[DerivedChannel, ...] = DEFAULT_DERIVED_CHANNELS,
) -> RuntimeCatalog:
    """Build the runtime lookup and registry, persisting registry generation.

    Catalog channels receive compact IDs starting at one in YAML order.
    Agent-derived channels use a separate high range so adding catalog entries
    cannot renumber them.
    """
    loaded = load_profile(profile) if isinstance(profile, (str, Path)) else profile
    catalog_hash = loaded.catalog_hash
    persisted_path = Path(state_path) if state_path is not None else loaded.path / _STATE_FILE_NAME
    # The registry snapshot also carries the agent's derived channels, so the
    # persisted generation must bump when *either* the catalog file or the
    # derived set changes — a consumer decodes strictly by registry_seq.
    registry_hash = hashlib.sha256(
        catalog_hash.encode("ascii")
        + b"\x00"
        + repr([(d.name, d.value_type, d.units) for d in derived_channels]).encode("utf-8")
    ).hexdigest()
    registry_seq = _update_registry_state(persisted_path, registry_hash)
    created_ms = round(time.time() * 1000) if created_unix_ms is None else created_unix_ms

    registry = pb.ChannelRegistry(
        registry_seq=registry_seq,
        vehicle_id=loaded.vehicle.vehicle.id,
        created_unix_ms=created_ms,
    )
    source_map: dict[str, tuple[int, ChannelPolicy]] = {}
    channel_ids: dict[str, int] = {}
    policies_by_id: dict[int, ChannelPolicy] = {}

    for channel_id, (name, config) in enumerate(loaded.catalog.channels.items(), start=1):
        policy = _channel_policy(name, config)
        _add_mapping(source_map, config.source_ref, channel_id, policy, loaded.path)
        channel_ids[name] = channel_id
        policies_by_id[channel_id] = policy
        registry.channels.add(
            id=channel_id,
            name=name,
            source_ref=config.source_ref,
            units=config.units,
            type=policy.value_type,
            scale=policy.scale,
            offset=policy.offset,
        )

    for index, derived in enumerate(derived_channels):
        channel_id = DERIVED_ID_START + index
        if derived.name in channel_ids:
            raise ConfigError(
                f"{loaded.path / 'catalog.yaml'}: duplicate derived channel {derived.name!r}"
            )
        source_ref = f"derived:{derived.name}"
        policy = ChannelPolicy(
            name=derived.name, value_type=derived.value_type, rbe=None, live_hz=None
        )
        _add_mapping(source_map, source_ref, channel_id, policy, loaded.path)
        channel_ids[derived.name] = channel_id
        policies_by_id[channel_id] = policy
        registry.channels.add(
            id=channel_id,
            name=derived.name,
            source_ref=source_ref,
            units=derived.units,
            type=derived.value_type,
        )

    return RuntimeCatalog(
        source_map=source_map,
        channel_ids=channel_ids,
        policies_by_id=policies_by_id,
        registry=registry,
        catalog_hash=catalog_hash,
    )


def _channel_policy(name: str, channel: ChannelConfig) -> ChannelPolicy:
    encoding = channel.encode
    encoding_type = channel.type if encoding is None else encoding.type
    scale = 0.0 if encoding is None else encoding.scale
    offset = 0.0 if encoding is None else encoding.offset
    return ChannelPolicy(
        name=name,
        value_type=_VALUE_TYPES[encoding_type],
        rbe=channel.rbe,
        live_hz=channel.live_hz,
        scale=scale,
        offset=offset,
    )


def _add_mapping(
    source_map: dict[str, tuple[int, ChannelPolicy]],
    source_ref: str,
    channel_id: int,
    policy: ChannelPolicy,
    profile_path: Path,
) -> None:
    if source_ref in source_map:
        other = source_map[source_ref][1].name
        raise ConfigError(
            f"{profile_path / 'catalog.yaml'}: channels.{policy.name}.from: "
            f"source reference {source_ref!r} is already mapped by {other!r}"
        )
    source_map[source_ref] = (channel_id, policy)


def _update_registry_state(path: Path, catalog_hash: str) -> int:
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            return _update_registry_state_locked(path, catalog_hash)
    except OSError as exc:
        raise ConfigError(
            f"{path}: unable to update registry state: {exc.strerror or exc}"
        ) from exc


def _update_registry_state_locked(path: Path, catalog_hash: str) -> int:
    previous_hash: str | None = None
    previous_seq = 0
    try:
        state_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        state_text = None
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path}: invalid registry state: unable to decode as UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{path}: unable to read registry state: {exc.strerror or exc}") from exc
    if state_text is not None:
        try:
            state = json.loads(state_text)
            previous_hash = state["catalog_hash"]
            previous_seq = state["registry_seq"]
            if not isinstance(previous_hash, str):
                raise TypeError("catalog_hash must be a string")
            if type(previous_seq) is not int:
                raise TypeError("registry_seq must be an integer")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ConfigError(f"{path}: invalid registry state: {exc}") from exc

    registry_seq = previous_seq if previous_hash == catalog_hash else previous_seq + 1
    if not 1 <= registry_seq <= (1 << 32) - 1:
        raise ConfigError(f"{path}: registry_seq exhausted uint32 range")
    if previous_hash != catalog_hash:
        _write_registry_state(path, catalog_hash, registry_seq)
    return registry_seq


def _write_registry_state(path: Path, catalog_hash: str, registry_seq: int) -> None:
    payload = json.dumps(
        {"catalog_hash": catalog_hash, "registry_seq": registry_seq},
        indent=2,
        sort_keys=True,
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(payload + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise ConfigError(
            f"{path}: unable to persist registry state: {exc.strerror or exc}"
        ) from exc
