#!/usr/bin/env bash
# Build the openlaps-link binary (rust/link) for an architecture and print its path.
#
#   tools/build_link.sh            this machine's architecture
#   tools/build_link.sh aarch64    the car board, cross-compiled
#
# Needs a Rust toolchain (mise rust@stable is used when cargo is not active)
# and, for a cross build, uv (it fetches zig as the cross linker).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)/rust/link"
arch="${1:-$(uname -m)}"

run() {
    # A mise shim for cargo exists even when no version is active, so ask it.
    if cargo --version >/dev/null 2>&1; then "$@"; else mise exec rust@stable -- "$@"; fi
}

if [[ "$arch" == "$(uname -m)" ]]; then
    run cargo build --release --locked >&2
    echo "$PWD/target/release/openlaps-link"
else
    target="${arch}-unknown-linux-gnu"
    run uv tool run --with ziglang cargo-zigbuild zigbuild --release --locked --target "${target}.2.28" >&2
    echo "$PWD/target/${target}/release/openlaps-link"
fi
