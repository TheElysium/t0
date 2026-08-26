# t0

Self-hosted Garmin Connect data ingestion and visualization pipeline for triathlon training
tracking.

## Components

- **`t0-ingest`** (this repository) : Garmin Connect API polling, `.FIT` file parsing,
  immutable calculations at ingestion, scheduler, manual `.FIT` upload endpoint.
- **PostgreSQL** : three-schema storage -- `raw` (API responses and raw files, never modified),
  `core` (normalized data), `mart` (materialized training load views).
- **Grafana** : dashboards (PMC, power-duration curve, swimming, wellness, sleep).
- **Helm Chart** : deployment on a personal Kubernetes cluster (see security note below).

The repository produces code and manifests. **No agent or CI calls Garmin Connect and no agent
or CI deploys to the cluster** : the athlete deploys.

## Package structure

```
src/t0/
|-- db/          # migrations and PostgreSQL access
|-- garmin/      # Garmin Connect client (persistent session, backoff, rate limiting)
|-- fit/         # .FIT file parsing (garmin-fit-sdk)
|-- compute/     # immutable calculations (NP, GAP, power-duration curve, ...) and load (TSS, PMC)
|-- scheduler/   # scheduling (daily polling, backfill, view refresh)
\-- api/         # manual .FIT file upload endpoint
```

## What runs on the cluster

The chart deploys, in addition to PostgreSQL and Grafana, five periodic workloads :

| Object | Values | Role |
| --- | --- | --- |
| CronJob `scheduler` | `scheduler.*` -- default `0 5 * * *` | **Garmin polling.** Also catches up history, pass by pass. Archives .FIT/.TCX activity files on PVC (can be disabled via `scheduler.activityFiles: false`, which passes `--no-activity-files`). |
| CronJob `ops-refresh` | `opsRefresh.*` -- default `30 5 * * *` | Refreshes the six materialized views `mart.*`. |
| CronJob `ops-backfill-status` | `opsBackfill.*` -- default `0 6 * * *`, enabled | Read-only report on backfill progress. Does not poll anything. |
| CronJob `ops-reconcile` | `opsReconcile.*` -- default `0 7 * * *`, enabled | Compares `raw.fit_file` inventory to actual PVC content. Read-only both sides, no deletions. Only fails on rows without files (orphaned files are harmless). |
| CronJob `postgresql-backup` | `backup.*` | `pg_dump --format=custom` to PVC. |
| Deployment `ingest` | `ingest.*` | Serves the manual `.FIT` upload endpoint. Does not poll Garmin. |

Operations procedures detail : [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Development

Prerequisites : Python 3.12 (exact version, see `requires-python`).

```bash
python -m venv .venv
source .venv/bin/activate   # ou .venv\Scripts\activate sous Windows
pip install -e ".[dev]"
```

Checks, in the order CI executes them :

```bash
ruff check .
ruff format --check .
mypy src tests
pytest
```

## Configuration

Settings are read from environment via `t0.settings.Settings` (pydantic-settings).
See `.env.example` for the list of expected variables -- this file does not and must never
contain real values.

No required field has a default value : the service refuses to start rather than fall back on
unintended implicit behavior.

Sensitive values (Garmin credentials **and** PostgreSQL DSN, which carries the database password)
are `SecretStr` : their `repr`, their `str`, and their JSON serialization are masked.
`get_settings()` is the only supported entry point -- it converts pydantic validation errors to
`SettingsError` which cites only *names* of variables. Without this conversion, pydantic copies
the entire input dictionary, Garmin password in plain text, into the exception message, hence
into container logs on first failed startup. Two tests lock down this property (`tests/test_settings.py`).

### `GARMINTOKENS` : must be set, risk of account lockout otherwise

`garminconnect` 0.3.2 reads the **`GARMINTOKENS`** variable as-is (`os.getenv("GARMINTOKENS")`,
without the `T0_` prefix) and **has no default path**. If it is not set, or if it points
to anything other than a **persistent volume**, the session is never reused : each container
restart performs a full SSO authentication against Garmin. Repeated, this sequence is the known
path to account lockout.

The Docker image sets it to `/data/garmin-tokens` -- this path **must** be mounted on a
persistent volume (PVC), otherwise persistence is illusory. On the code side, the corresponding
field is `Settings.garmin_tokens_path`, declared with `validation_alias="GARMINTOKENS"` precisely
to share the name expected by the library.

## Security -- Garmin Secret

> **The Kubernetes Secret containing Garmin Connect credentials is never committed to this
> repository.** It is created directly on the cluster, by the athlete, outside any versioned
> manifest (the Helm chart only *references* it by name). Treat it with the same care as a
> root password.

## Docker image

Multi-stage `Dockerfile` : wheel build in an image with build tools, execution in a final image
without compiler, under a non-root user, with `HEALTHCHECK`.

The `.dockerignore` restricts the build context : personal data (`Activities.csv`,
`metrics&wellness_1year/`, `*.fit`), `.env` and caches never enter it. This guarantees that no
personal data can end up in an image published to GHCR, even if an overly broad `COPY` is
introduced later.

```bash
docker build -t t0-ingest .
```

The image is published to GHCR by the `.github/workflows/publish-image.yml` workflow, triggered on
push of a tag `vX.Y.Z`.
