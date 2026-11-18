#!/usr/bin/env bash
# Build the openlaps_canfast wheel (rust/canfast): the vehicle agent's CAN
# ingest path as a compiled Python extension.
#
#     tools/build_canfast.sh                 # for this machine
#     tools/build_canfast.sh aarch64         # cross-build for an ARM64 board
#
# Prints the wheel's path on the last line. Needs a Rust toolchain (cargo on
# PATH, or `mise install rust@stable`) and uv; a cross-build also needs the
# target's std (`rustup target add aarch64-unknown-linux-gnu`) and links with
# zig, which uv fetches as the `ziglang` package. The wheel uses Python's
# stable ABI, so one build serves every Python from 3.12 on.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)/rust/canfast"
arch="${1:-$(uname -m)}"
out="$PWD/dist"

run() {
    # A mise shim for cargo exists even when no version is active, so ask it.
    if cargo --version >/dev/null 2>&1; then "$@"; else mise exec rust@stable -- "$@"; fi
}

if [[ "$arch" == "$(uname -m)" ]]; then
    run uv tool run maturin build --release -o "$out" >&2
else
    run uv tool run --with ziglang maturin build --release \
        --target "${arch}-unknown-linux-gnu" --zig --compatibility manylinux_2_28 -o "$out" >&2
fi
ls -t "$out"/openlaps_canfast-*"${arch}".whl | head -1
