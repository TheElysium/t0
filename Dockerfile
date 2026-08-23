# syntax=docker/dockerfile:1

# --- Étage 1 : construction du wheel ---------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip hatchling

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# --- Étage 2 : image finale, sans compilateur -------------------------------
FROM python:3.12-slim AS runtime

# Pas de compilateur ni d'outils de build dans l'image finale : les wheels
# binaires (psycopg[binary], etc.) sont installés tels quels.
RUN groupadd --system garminator \
    && useradd --system --gid garminator --home-dir /app --shell /usr/sbin/nologin garminator

WORKDIR /app

COPY --from=builder /wheels /wheels

RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

# Volumes attendus à l'exécution : session Garmin (GARMINTOKENS) et archive FIT.
RUN mkdir -p /data/garmin-tokens /data/fit-archive \
    && chown -R garminator:garminator /data /app

USER garminator

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GARMINTOKENS=/data/garmin-tokens \
    GARMINATOR_FIT_ARCHIVE_PATH=/data/fit-archive

EXPOSE 8000

# Le point d'entrée réel (API FastAPI de L7, endpoint d'upload FIT) n'existe
# pas encore à ce stade du dépôt (lot L0) ; l'exécution de référence pour le
# healthcheck est donc l'import du paquet, qui échoue si l'environnement ou
# les dépendances sont cassés.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import garminator" || exit 1

CMD ["python", "-c", "import garminator; print(garminator.__version__)"]
