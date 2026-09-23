#!/usr/bin/env python3
"""Fill the pit registry with every image the stack runs from.

The pit registry (`deploy/registry-compose.yaml`) is what lets a pit with no
uplink bring the stack up, and a car with no uplink pull the openlaps image
the pit built. It is only as complete as its last fill, and "complete" is
defined by the compose files, not by a list kept here: this tool asks
`docker compose config --images` for every stack -- the pit, and the vehicle
stack once per target under `deploy/targets/` -- so a new service or a bumped
tag is mirrored without anyone remembering to edit a second list.

    uv run tools/registry_sync.py            # while the pit has internet
    uv run tools/registry_sync.py --dry-run  # what it would do

Third-party images are copied registry-to-registry with `docker buildx
imagetools create`, which carries the *whole* manifest list: the Luckfox is
arm64 and the pit and Radxa are amd64, and a `docker pull && docker push` from
the pit would hold only the pit's own architecture. The copy is keyed by
digest -- the digest the pit's daemon already runs where it has the image,
so the mirror serves exactly what the pit was verified with, or Docker Hub's
current one for a tag the pit has never pulled -- and skipped when the
registry already holds it, so re-running is cheap.

The openlaps image is different: it is built here, from this checkout, and
pushed as `openlaps:<git describe>` plus `openlaps:latest`. Vehicles pull it
by that name (`OPENLAPS_IMAGE` in their .env). It is amd64 only, which is
every board that runs the containerised agent today; the Luckfox runs the
agent from a systemd unit.

Pushes go to `localhost:<port>` deliberately. The daemon treats loopback as
an insecure registry without being told, so the pit itself needs no
daemon.json change to fill the registry; the names the *vehicles* pull by
are the pit's LAN address, which their daemons list under
`insecure-registries` (docs/operations/registry.md).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
DEFAULT_PORT = 5000

# The name pit-compose.yaml and vehicle-compose.yaml build when OPENLAPS_IMAGE
# is unset. Whatever OPENLAPS_IMAGE says on the pit, this is the image the
# sync builds and pushes, because it is the one the pit's `build:` produces.
LOCAL_IMAGE = "openlaps:local"
PUSHED_REPO = "openlaps"

_REQUIRED_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")
_HUB_HOSTS = ("docker.io", "index.docker.io", "registry-1.docker.io")


class SyncError(Exception):
    """A condition the operator has to resolve; the message is the report."""


@dataclass(frozen=True)
class Stack:
    """One `docker compose config --images` invocation."""

    label: str
    files: tuple[Path, ...]
    env_files: tuple[Path, ...] = ()
    all_profiles: bool = False


@dataclass
class Plan:
    """The third-party images to mirror, each with the digest wanted (None: ask Hub)."""

    mirror: dict[str, str | None] = field(default_factory=dict)


# --- names ------------------------------------------------------------------


def split_reference(image: str) -> tuple[str | None, str, str]:
    """`(registry, repository, tag-or-digest)` for a docker image reference.

    The registry is `None` for Docker Hub, whether the reference spells it
    out or not. Docker's rule for telling a registry host from a repository
    namespace is the one used here: the first path component is a host when
    it contains a dot or a colon, or is `localhost`.
    """
    if "@" in image:
        name, _, ref = image.partition("@")
    else:
        name, _, ref = image.rpartition(":")
        if not name or "/" in ref:
            # `host:5000/repo` with no tag: the colon belonged to the host.
            name, ref = image, "latest"
    first, sep, rest = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        registry: str | None = first
        repository = rest
        if registry in _HUB_HOSTS:
            registry = None
    else:
        registry, repository = None, name
    if registry is None and "/" not in repository:
        repository = f"library/{repository}"
    return registry, repository, ref


def mirror_name(image: str, registry: str) -> str:
    """Where a Docker Hub image lives in the mirror: `<registry>/library/nats:tag`.

    A daemon asked for `nats:2.12-alpine` requests `library/nats` from every
    mirror it has, so that is the path the copy has to be pushed under.
    """
    upstream, repository, ref = split_reference(image)
    if upstream is not None:
        raise SyncError(
            f"{image} is not on Docker Hub: a daemon's registry-mirrors setting "
            "only covers Docker Hub names, so the pit cannot serve it"
        )
    sep = "@" if ref.startswith("sha256:") else ":"
    return f"{registry}/{repository}{sep}{ref}"


def is_local_build(image: str, registry: str) -> bool:
    """Whether `image` is the pit-built openlaps image under any of its names."""
    upstream, repository, _ = split_reference(image)
    if repository == f"library/{PUSHED_REPO}" and upstream in (None, registry):
        return True
    if upstream is not None and repository == PUSHED_REPO:
        return True
    return False


# --- compose --------------------------------------------------------------


def required_placeholders(files: list[Path]) -> dict[str, str]:
    """A dummy value for every `${VAR:?...}` in `files`, so `config` renders.

    The pit .env holds the pit's secrets but not the vehicle's, and a fresh
    checkout holds neither. Image names never depend on those variables --
    they are `${X:-default}` interpolations -- so a placeholder is enough
    to get compose to print them. They go in the process environment, which
    compose prefers over .env, so they shadow the pit's real values for the
    duration of one `config` call -- and nothing about an image name depends
    on any of them.
    """
    names: set[str] = set()
    for path in files:
        names.update(_REQUIRED_VAR.findall(path.read_text()))
    return {name: "/placeholder" for name in sorted(names) if name not in os.environ}


def stacks(deploy: Path = DEPLOY) -> list[Stack]:
    """The pit stack and one vehicle stack per target directory."""
    env = deploy / ".env"
    base_env = (env,) if env.exists() else ()
    out = [Stack("pit", (deploy / "pit-compose.yaml",), base_env)]
    for target_env in sorted(deploy.glob("targets/*/target.env")):
        target = target_env.parent
        files: list[Path] = [deploy / "vehicle-compose.yaml"]
        if (target / "compose.yaml").exists():
            files.append(target / "compose.yaml")
        out.append(
            Stack(
                f"vehicle/{target.name}",
                tuple(files),
                base_env + (target_env,),
                all_profiles=True,
            )
        )
    return out


def compose_images(stack: Stack) -> list[str]:
    """Every `image:` the stack resolves to, with .env and defaults applied."""
    cmd = ["docker", "compose"]
    for env_file in stack.env_files:
        cmd += ["--env-file", str(env_file)]
    for path in stack.files:
        cmd += ["-f", str(path)]
    if stack.all_profiles:
        cmd += ["--profile", "*"]
    cmd += ["config", "--images"]
    env = os.environ | required_placeholders(list(stack.files))
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise SyncError(f"{stack.label}: {result.stderr.strip()}")
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


# --- docker -----------------------------------------------------------------


def _run(cmd: list[str], *, quiet: bool = False) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if quiet:
            return ""
        raise SyncError(f"{' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def local_digest(image: str) -> str | None:
    """The manifest digest the pit's daemon pulled `image` by, if it has it."""
    _, repository, _ = split_reference(image)
    short = repository.removeprefix("library/")
    out = _run(
        ["docker", "image", "inspect", "--format", '{{join .RepoDigests "\\n"}}', image], quiet=True
    )
    for entry in out.splitlines():
        name, _, digest = entry.partition("@")
        if name in (repository, short) and digest:
            return digest
    return None


def remote_digest(reference: str) -> str | None:
    """The manifest digest a registry serves for `reference`, or None if absent."""
    out = _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--builder",
            "default",
            "--format",
            "{{.Manifest.Digest}}",
            reference,
        ],
        quiet=True,
    )
    return out or None


def copy_image(source: str, target: str) -> None:
    """Registry-to-registry copy of a manifest list, every platform included."""
    _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--builder",
            "default",
            "--tag",
            target,
            source,
        ]
    )


def openlaps_version() -> str:
    """The tag the built openlaps image is pushed under: the checkout it came from."""
    out = _run(
        ["git", "-C", str(REPO_ROOT), "describe", "--always", "--dirty", "--abbrev=12"], quiet=True
    )
    return out or "unknown"


# --- the run -----------------------------------------------------------------


def plan(registry: str) -> Plan:
    """Collect the third-party images across every stack; separate the local build."""
    out = Plan()
    for stack in stacks():
        for image in compose_images(stack):
            if is_local_build(image, registry):
                continue  # built here; sync_openlaps pushes it
            if image in out.mirror:
                continue
            _, _, ref = split_reference(image)
            out.mirror[image] = ref if ref.startswith("sha256:") else local_digest(image)
    return out


def sync_mirror(p: Plan, registry: str, *, dry_run: bool) -> int:
    copied = 0
    for image, wanted in p.mirror.items():
        target = mirror_name(image, registry)
        source = image
        if wanted is None:
            wanted = remote_digest(image)
            if wanted is None:
                raise SyncError(f"{image}: not in the local daemon and not on Docker Hub")
        else:
            _, repository, _ = split_reference(image)
            source = f"{repository}@{wanted}"
        have = remote_digest(target)  # a read, so a dry run reports it too
        if have == wanted:
            print(f"  = {image}  up to date ({wanted[7:19]})")
            continue
        verb = "would copy" if dry_run else "copying"
        print(f"  + {image}  {verb} {wanted[7:19]} -> {target}")
        if not dry_run:
            copy_image(source, target)
        copied += 1
    return copied


def sync_openlaps(registry: str, *, build: bool, dry_run: bool) -> list[str]:
    version = openlaps_version()
    tags = [f"{registry}/{PUSHED_REPO}:{version}", f"{registry}/{PUSHED_REPO}:latest"]
    if build:
        print(f"  {'would build' if dry_run else 'building'} {LOCAL_IMAGE} from {REPO_ROOT}")
        if not dry_run:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(DEPLOY / "Dockerfile"),
                    "-t",
                    LOCAL_IMAGE,
                    str(REPO_ROOT),
                ],
                check=True,
            )
    for tag in tags:
        print(f"  {'would push' if dry_run else 'pushing'} {tag}")
        if not dry_run:
            _run(["docker", "tag", LOCAL_IMAGE, tag])
            _run(["docker", "push", "--quiet", tag])
    return tags


def registry_port(deploy: Path = DEPLOY) -> int:
    """OPENLAPS_REGISTRY_PORT from deploy/.env, the one registry-compose reads."""
    env = deploy / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "OPENLAPS_REGISTRY_PORT" and value.split("#")[0].strip():
                return int(value.split("#")[0].strip())
    return DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--registry",
        default=None,
        help="where to push (default: localhost:<OPENLAPS_REGISTRY_PORT from deploy/.env>)",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help=f"push the {LOCAL_IMAGE} already in the daemon instead of rebuilding it",
    )
    parser.add_argument("--no-openlaps", action="store_true", help="mirror third-party images only")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)
    registry = args.registry or f"localhost:{registry_port()}"
    # Copies take minutes each; a log or a pipe should see each line as it happens.
    sys.stdout.reconfigure(line_buffering=True)

    try:
        p = plan(registry)
        print(
            f"Registry {registry}: {len(p.mirror)} third-party images across {len(stacks())} stacks"
        )
        copied = sync_mirror(p, registry, dry_run=args.dry_run)
        if args.no_openlaps:
            tags: list[str] = []
        else:
            print("openlaps:")
            tags = sync_openlaps(registry, build=not args.no_build, dry_run=args.dry_run)
    except SyncError as exc:
        print(f"registry_sync: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"registry_sync: {' '.join(exc.cmd)} failed ({exc.returncode})", file=sys.stderr)
        return 1

    print(
        f"{'Would copy' if args.dry_run else 'Copied'} {copied}, "
        f"{len(p.mirror) - copied} already current."
    )
    if tags:
        pit_tag = tags[0].split("/", 1)[1]
        print(
            f"Vehicles pull the openlaps image as <pit-lan-address>:{registry.rsplit(':', 1)[1]}"
            f"/{pit_tag} (OPENLAPS_IMAGE in their deploy/.env)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
