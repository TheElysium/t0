# syntax=docker/dockerfile:1

# --- Stage 1: wheel build -------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip hatchling

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# --- Stage 2: final image, no compiler ----------------------------------------
FROM python:3.12-slim AS runtime

# No compiler or build tools in the final image: binary wheels
# (psycopg[binary], etc.) are installed as-is.
RUN groupadd --system garminator \
    && useradd --system --gid garminator --home-dir /app --shell /usr/sbin/nologin garminator

WORKDIR /app

COPY --from=builder /wheels /wheels

RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

# Expected volumes at runtime: Garmin session (GARMINTOKENS) and FIT archive.
RUN mkdir -p /data/garmin-tokens /data/fit-archive \
    && chown -R garminator:garminator /data /app

USER garminator

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GARMINTOKENS=/data/garmin-tokens \
    GARMINATOR_FIT_ARCHIVE_PATH=/data/fit-archive

EXPOSE 8000

# The healthcheck intentionally stays independent of runtime configuration: the
# FastAPI factory loads settings only when a request needs them.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import garminator" || exit 1

# Serve the local manual-upload API by default. Scheduler and operations jobs
# explicitly override this command in their Helm manifests.
CMD ["uvicorn", "garminator.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
