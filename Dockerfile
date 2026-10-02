FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer: rebuilt only when the lockfile or project metadata changes.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Application layer.
COPY src/ ./src/
COPY data/snapshot/ ./data/snapshot/
RUN uv sync --frozen --no-dev

# Build provenance reported by /health; empty means unknown.
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ENV GIT_COMMIT=${GIT_COMMIT} \
    GIT_BRANCH=${GIT_BRANCH} \
    SNAPSHOT_DIR=/app/data/snapshot \
    LOCAL_DIR=/app/local \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/local \
    && chown app:app /app/local
USER app

CMD ["feasibility", "serve", "--host", "0.0.0.0", "--port", "4501"]
