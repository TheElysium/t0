# AGENTS.md

t0 — self-hosted pipeline to ingest and visualize Garmin Connect data.
Python 3.12, FastAPI, PostgreSQL 16, Grafana, MCP. Deployed via Helm.

## Map
- `src/t0/scheduler` — Garmin Connect poller (raw-first: payloads archived to PVC).
- `src/t0/fit`, `src/t0/tcx` — file parsers; `src/t0/transform` — raw → core (two-pass).
- `src/t0/db` — migration runner (forward-only, checksummed, advisory lock).
- `src/t0/load`, `src/t0/compute` — TSS/TRIMP/PMC, derived metrics.
- `src/t0/ops` — CLI: refresh (mart), reconcile (PVC↔DB), backfill-status, health, seed-pmc.
- `src/t0/api` — FastAPI manual upload + RPE; `src/t0/mcp` — read-only MCP server over mart.*
- `deploy/helm/t0` — all runtime scheduling as k8s CronJobs:
  scheduler → transform → ops-refresh (+15/+30 min after each poll, 3x/day);
  ops-reconcile and ops-backfill-status daily; pg_dump backup Sundays.
- Migrations run automatically via Helm hook (`migrate-job`); tracking in `public.schema_migration`.
- Root `*.fit`/`*.csv`/`metrics*` are personal, git-ignored; fixtures live in `tests/fixtures/`.

## Rules
- Comments: 2 lines max. Be frugal; the code must be self-descriptive.
- Docs: concise and direct. Short sentences, no prose. Stay pragmatic.
- The repo must stay infra-agnostic: no homelab-specific names, IPs, or hostnames
  in committed docs. Anyone can deploy t0 on their own infrastructure.

## Quality
- mypy is strict. No untyped defs, no `Any` escapes.
- Complexity ≤ 10 (mccabe), branches ≤ 12 — enforced in pyproject, do not widen.
- SAST via ruff (S, DTZ, B). No hardcoded secrets; test tokens only in tests/.

## Verify before handoff (same as CI)
    ruff check .
    ruff format --check .
    mypy src tests
    pytest

## Conventions
- Migrations: numbered SQL files in migrations/; never edit an applied one.
- Any change under deploy/helm/ bumps the chart version in deploy/helm/t0/Chart.yaml.
- Pins in pyproject are deliberate: read the comment above before bumping.
