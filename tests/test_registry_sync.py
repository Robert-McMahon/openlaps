"""tools/registry_sync.py: the mirror path is docker's, and the list is compose's.

A daemon with `registry-mirrors` set asks the mirror for `library/nats`
when a compose file says `nats:2.12-alpine`, so the one thing the sync
cannot get wrong is the path it pushes under. The other thing it must not
do is keep its own list of images: the compose files are the list, so a
bumped tag or a new target directory is mirrored without a second edit.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import registry_sync  # noqa: E402

ROOT = Path(__file__).parents[1]
REG = "localhost:5000"


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("nats:2.12-alpine", (None, "library/nats", "2.12-alpine")),
        ("grafana/grafana:12.4.9", (None, "grafana/grafana", "12.4.9")),
        ("docker.io/library/nats:2.12-alpine", (None, "library/nats", "2.12-alpine")),
        ("eclipse-mosquitto", (None, "library/eclipse-mosquitto", "latest")),
        ("localhost:5000/openlaps", ("localhost:5000", "openlaps", "latest")),
        ("192.168.12.203:5000/openlaps:abc", ("192.168.12.203:5000", "openlaps", "abc")),
        ("ghcr.io/astral-sh/uv:0.9", ("ghcr.io", "astral-sh/uv", "0.9")),
        (
            "nats@sha256:" + "a" * 64,
            (None, "library/nats", "sha256:" + "a" * 64),
        ),
    ],
)
def test_references_split_the_way_docker_splits_them(image, expected):
    assert registry_sync.split_reference(image) == expected


def test_official_images_live_under_library_in_the_mirror():
    assert registry_sync.mirror_name("nats:2.12-alpine", REG) == f"{REG}/library/nats:2.12-alpine"
    assert (
        registry_sync.mirror_name("grafana/grafana:12.4.9", REG) == f"{REG}/grafana/grafana:12.4.9"
    )


def test_a_non_hub_image_is_refused_rather_than_silently_skipped():
    with pytest.raises(registry_sync.SyncError, match="not on Docker Hub"):
        registry_sync.mirror_name("ghcr.io/astral-sh/uv:0.9", REG)


@pytest.mark.parametrize(
    "image",
    ["openlaps:local", "openlaps:abc123", f"{REG}/openlaps:latest", "192.168.12.203:5000/openlaps"],
)
def test_the_pit_built_image_is_recognised_under_every_name(image):
    assert registry_sync.is_local_build(image, REG)


@pytest.mark.parametrize("image", ["nats:2.12-alpine", "someone/openlaps:1"])
def test_third_party_images_are_not_mistaken_for_the_local_build(image):
    assert not registry_sync.is_local_build(image, REG)


def test_every_required_compose_variable_gets_a_placeholder(monkeypatch):
    files = [ROOT / "deploy" / "pit-compose.yaml", ROOT / "deploy" / "vehicle-compose.yaml"]
    monkeypatch.delenv("NATS_TLS_DIR", raising=False)
    monkeypatch.setenv("TIMESCALE_PASSWORD", "real")
    placeholders = registry_sync.required_placeholders(files)
    assert "NATS_TLS_DIR" in placeholders
    assert "NATS_LEAF_PASSWORD" in placeholders
    assert "TIMESCALE_PASSWORD" not in placeholders, "an operator's value is never shadowed"


def test_one_vehicle_stack_per_target_directory():
    labels = [s.label for s in registry_sync.stacks()]
    targets = sorted(p.parent.name for p in (ROOT / "deploy" / "targets").glob("*/target.env"))
    assert labels == ["pit"] + [f"vehicle/{t}" for t in targets]
    luckfox = next(s for s in registry_sync.stacks() if s.label.endswith("luckfox-omni3576"))
    assert luckfox.files[-1].name == "compose.yaml", "the target overlay is merged in"
    assert luckfox.all_profiles, "the video profile's image is part of the stack"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_resolves_every_stack_to_mirrorable_names():
    """`config --images` renders without a populated .env, and nothing is foreign."""
    seen: set[str] = set()
    for stack in registry_sync.stacks():
        images = registry_sync.compose_images(stack)
        assert images, stack.label
        for image in images:
            if registry_sync.is_local_build(image, REG):
                continue
            registry_sync.mirror_name(image, REG)  # raises for a non-Hub image
            seen.add(image)
    assert "nats:2.12-alpine" in seen
    assert any(i.startswith("alexxit/go2rtc:") for i in seen)
