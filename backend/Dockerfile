# Backend Dockerfile — multi-stage (dev + prod)
# Base : python:3.14-slim (latest stable)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 1 — Base (uv + external deps only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_CACHE_DIR=/app/.uv-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# Install uv (official installer)
RUN pip install --no-cache-dir --root-user-action=ignore uv

WORKDIR /app

# Copy deps first (for Docker layer caching)
COPY pyproject.toml uv.lock* ./

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 2 — Dev (hot reload, full dev deps)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM base AS dev

# Install external deps only (local package installed after COPY .)
RUN uv sync --frozen --no-install-project

# Copy all source code (alembic, src, tests, scripts, spike)
COPY . .

# Now install the local agentive-backend package
RUN uv sync --frozen

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "agentive_backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 3 — Prod (no dev deps, non-root user)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM base AS prod

# Prod deps only — no dev group — skip local package for now
RUN uv sync --frozen --no-install-project --no-group dev --no-dev

# Copy code
COPY . .

# Install local package (prod mode)
RUN uv sync --frozen --no-group dev --no-dev

# Non-root user for security.
# `useradd -m` crée explicitement `/home/agentive` (sinon `useradd -r` seul
# sur Debian respecte CREATE_HOME=no et le home dir n'existe pas → uv run
# tente de créer `/home/agentive/.cache/uv` et échoue avec EACCES).
# Le UV_CACHE_DIR pointe explicitement vers `/app/.uv-cache` pour ne pas
# dépendre du home dir et garder un chemin prévisible (writable car chown
# -R sous /app).
RUN useradd -r -m -u 1001 -g root agentive \
    && mkdir -p /app/.uv-cache \
    && chown -R agentive:root /app
USER agentive

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" || exit 1

CMD ["uv", "run", "uvicorn", "agentive_backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
