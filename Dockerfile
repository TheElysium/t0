# syntax=docker/dockerfile:1

# Base image pinned by digest, not by tag: `python:3.12-slim` is republished on every
# upstream rebuild, so the tag alone makes a build non-reproducible -- two builds of
# the same commit can ship different system libraries. The tag is kept next to the
# digest for readability; only the digest is authoritative. Bumping it is a deliberate
# act, which also means base-image CVE fixes land only when this line is updated.
ARG PYTHON_BASE=python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

# --- Stage 1: wheel build -------------------------------------------------------
FROM ${PYTHON_BASE} AS builder

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip hatchling

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# --- Stage 2: final image, no compiler ----------------------------------------
FROM ${PYTHON_BASE} AS runtime

# No compiler or build tools in the final image: binary wheels
# (psycopg[binary], etc.) are installed as-is.
RUN groupadd --system t0 \
    && useradd --system --gid t0 --home-dir /app --shell /usr/sbin/nologin t0

WORKDIR /app

COPY --from=builder /wheels /wheels

RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

# The migrations are plain .sql files living outside the Python package, so the wheel
# does not carry them. T0_MIGRATIONS_DIR below points the runner at this copy: its
# fallback path is derived from the source tree layout and is wrong once the package
# is installed in site-packages.
COPY migrations ./migrations

# Expected volumes at runtime: Garmin session (GARMINTOKENS) and FIT archive.
RUN mkdir -p /data/garmin-tokens /data/fit-archive \
    && chown -R t0:t0 /data /app

USER t0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GARMINTOKENS=/data/garmin-tokens \
    T0_FIT_ARCHIVE_PATH=/data/fit-archive \
    T0_MIGRATIONS_DIR=/app/migrations

# Must stay in sync with the CMD below and with `ingest.service.port` in the chart.
EXPOSE 8080

# The healthcheck intentionally stays independent of runtime configuration: the
# FastAPI factory loads settings only when a request needs them.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import t0" || exit 1

# Serve the local manual-upload API by default. Scheduler and operations jobs
# explicitly override this command in their Helm manifests.
CMD ["uvicorn", "t0.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
