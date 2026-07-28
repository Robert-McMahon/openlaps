# One image, five entry points.
#
# The pit runs four long-lived services plus a one-shot migrator, and they
# share every dependency they have. Four images would be four builds, four
# pulls and four chances for one of them to drift a version behind; a single
# image with `command:` selecting the entry point is simply less to keep in
# step. `[project.scripts]` already declares all five, so nothing here needs
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


FROM python:3.13-slim AS runtime

# libpq for psycopg's binary wheel is already vendored; nothing else is
# needed at runtime. curl is here for the compose healthchecks, which hit
# each service's /health over HTTP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 openlaps

WORKDIR /app

COPY --from=build --chown=openlaps:openlaps /app/.venv /app/.venv
COPY --from=build --chown=openlaps:openlaps /app/src /app/src

# Deploy-time helpers that are not part of the wheel: the pit stream
# provisioner is run once at bring-up, by hand or by the compose one-shot.
COPY --chown=openlaps:openlaps deploy/provision_pit_streams.py /app/deploy/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER openlaps

# No default command: every compose service names its own entry point.
# openlaps-migrate | openlaps-ingest-writer | openlaps-live-decoder |
# openlaps-session-control | openlaps-ntrip-client
CMD ["python", "-c", "import sys; sys.exit('name an openlaps-* entry point as the command')"]
