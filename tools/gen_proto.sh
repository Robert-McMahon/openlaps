#!/usr/bin/env bash
# Regenerate Python protobuf bindings from proto/telemetry.proto.
#
# Output lands in src/core/pb/ (checked in — consumers should not need
# grpcio-tools installed just to import the generated module). Re-run this
# whenever proto/telemetry.proto changes.
#
# Usage:
#   tools/gen_proto.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT_DIR="src/core/pb"
mkdir -p "${OUT_DIR}"

uv run --extra dev python -m grpc_tools.protoc \
    -I proto \
    --python_out="${OUT_DIR}" \
    --pyi_out="${OUT_DIR}" \
    proto/telemetry.proto

echo "Generated ${OUT_DIR}/telemetry_pb2.py (+ .pyi) from proto/telemetry.proto"
