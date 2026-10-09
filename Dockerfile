# python:3.12-slim, index digest resolved 2026-10-02
ARG PYTHON_IMAGE=python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

# Builder: uv fetches the agent-core git dependency, so this stage (and only this stage) has git.
FROM ${PYTHON_IMAGE} AS builder

# ghcr.io/astral-sh/uv:0.12.10, index digest resolved 2026-10-02
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer: rebuilt only when the lockfile or project metadata changes.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Application layer.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Final image: the same base, the built environment and the application, with no git and no uv.
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY data/snapshot/ ./data/snapshot/
COPY data/mls/ ./data/mls/
COPY data/llm/ ./data/llm/

# Build provenance reported by /health; empty means unknown.
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ENV GIT_COMMIT=${GIT_COMMIT} \
    GIT_BRANCH=${GIT_BRANCH} \
    SNAPSHOT_DIR=/app/data/snapshot \
    MLS_DIR=/app/data/mls \
    AGENT_CORE_CONFIG=/app/data/llm/agent-core.toml \
    LOCAL_DIR=/app/local \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/local \
    && chown app:app /app/local
USER app

CMD ["feasibility", "serve", "--host", "0.0.0.0", "--port", "4501"]
