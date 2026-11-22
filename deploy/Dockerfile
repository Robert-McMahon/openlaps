# One image for every openlaps entry point.
#
# The vehicle and pit services share their Python dependencies. Separate
# images would mean repeated builds, repeated pulls
# and more chances for one of them to drift a version behind; a single
# image with `command:` selecting the entry point is simply less to keep in
# step. `[project.scripts]` already declares them all, so nothing here needs
# a per-service wrapper.
#
# Build from the repository root, not from deploy/:
#     docker build -f deploy/Dockerfile -t openlaps:local .

FROM python:3.13-slim AS build

# uv resolves and installs from the checked-in uv.lock, so an image built
# today and one built next month contain the same dependency versions.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than
# the source, so a source edit rebuilds only the second install.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# The link tunnel (rust/link): one small binary, used only by the optional
# `link` service of the pit stack. Built against an older Debian than the
# runtime image so the binary's libc requirement is always met.
FROM rust:1-slim-bookworm AS link
WORKDIR /src
COPY rust/link/Cargo.toml rust/link/Cargo.lock ./
COPY rust/link/src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked && cp target/release/openlaps-link /openlaps-link

FROM python:3.13-slim AS runtime

# libpq for psycopg's binary wheel is already vendored; nothing else is
# needed at runtime. curl is here for the compose healthchecks, which hit
# each service's /health over HTTP.
#
# chrony is here for `chronyc` alone, never the daemon. The host collector
# shells out to `chronyc tracking` to publish sys.host.clock_* (offset,
# source, stratum, root dispersion) -- on the vehicle via the agent, and at
# the pit via the pit-monitor, which reuses the same collector. Without the
# binary those four channels fail silently and the clock-health panels are
# permanently empty -- which is what the containerised vehicle stack did until
# 2026-08-29, and why the bench never saw it: the bench runs the agent on the
# host, where chronyc already exists. Both containers share the host network
# namespace, so chronyc reaches chronyd on 127.0.0.1:323 with no socket mount.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl chrony \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 openlaps

WORKDIR /app

COPY --from=build --chown=openlaps:openlaps /app/.venv /app/.venv
COPY --from=build --chown=openlaps:openlaps /app/src /app/src

# Deploy-time helpers that are not part of the wheel: the pit stream
# provisioner is run once at bring-up, by hand or by the compose one-shot.
COPY --chown=openlaps:openlaps deploy/provision_pit_streams.py /app/deploy/
COPY --from=link /openlaps-link /usr/local/bin/openlaps-link

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN install -d -o openlaps -g openlaps /var/lib/openlaps/agent /data

USER openlaps

# No default command: every compose service names its own entry point.
# openlaps-migrate | openlaps-ingest-writer | openlaps-live-decoder |
# openlaps-session-control | openlaps-ntrip-client | openlaps-pit-monitor
CMD ["python", "-c", "import sys; sys.exit('name an openlaps-* entry point as the command')"]
