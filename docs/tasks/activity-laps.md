# Task: Activity laps via MCP + Grafana

Status: done 2026-11-09 | released v0.1.40 (PR #6), QA passed; run laps pace m:ss and bike laps Start offset fixes in chart 0.1.41
Started: 2026-10-14

## Spec

Problem
- Laps (`core.activity_lap`) and swim lengths (`core.swim_length`) are invisible to the MCP (`t0_mcp_ro` reads `mart.*` only) and to dashboards.
- Splits cannot be reviewed after a race. Watch records auto-lap 1 km on free activities, 1 lap per step on structured workouts: laps are the splits.

Solution
- Two plain views `mart.activity_lap`, `mart.swim_length`; MCP tool `get_laps(activity_id)`; laps table panel in run, swim, bike and tri session dashboards.

Decisions
- Plain VIEW, not materialized: no `MART_VIEWS`/`ops health` change, no refresh lag. No `security_invoker`. Created as role `t0` so default privileges grant `t0_mcp_ro`; no new grant.
- Views join `core.activity`, filter `is_parent = false`, expose `parent_activity_id`, `leg_order`.
- Typed columns whitelisted. `extra` jsonb never exposed (holds lap start/end GPS).
- `get_laps` returns None for unknown or parent id. Else `{rows, truncated, lengths, lengths_truncated}`, two LIMITed queries, each capped by `max_rows`.
- Idle lengths and rest laps included as stored (`length_type`, `num_lengths`).
- `lap_index` exposed as-is (leg laps keep the parent FIT `message_index`); dashboards show `row_number()` per activity.
- HR caveat in `instructions.py` + tool docstring: lap HR is unfiltered sensor value, can be wrong. No per-row field.
- Grafana reads `mart.*`. Tri: one table, Leg column, T1/T2 excluded, order `leg_order, lap_index`.
- Columns: run = lap, duration, distance, pace min/km, avg HR, avg power; swim = lap, lengths, distance, pace /100 m, stroke; bike = lap, duration, distance, avg HR, avg power.
- 2026-10-18 (athlete): run/bike already had a `core.activity_lap` Laps table: repoint to `mart.activity_lap`, Lap = `row_number()`, keep current columns. Swim: add a Laps table, keep Swim lengths, repointed to `mart.swim_length`.
- Run and bike Laps `Start` is the offset from the first lap (was wall-clock time of day, UTC on bike).

Out of scope
- Raw FIT/sample access, `activity_split` km bucketing, HR correction, DB role changes, TCX lengths (always empty).

## Plan (vertical slices, all AFK except 5)

1. **View migration + MCP tool** (proof: TDD)
   - `migrations/0NN_mart_activity_lap.sql`: views, header states owner-rights exposure and rollback = new forward `DROP VIEW` migration.
   - `src/t0/mcp/handlers.py`: `get_laps` handler (SQL const, None on unknown/parent, two caps).
   - `src/t0/mcp/server.py`: register tool. `src/t0/mcp/instructions.py`: laps semantics, truncated/is_parent bullets, HR caveat.
   - Tests: `tests/mcp/test_handlers.py` (FakeRunner: unknown, parent, laps only, laps + lengths, both truncated, safety limit bound), `tests/mcp/test_protocol.py` `EXPECTED_TOOLS`.
2. **Migration proof against real DB** (proof: existing suite + new DB test)
   - Test that `t0_mcp_ro` can SELECT both views and that parent rows are absent; follow `tests/mcp/test_db_reader.py` pattern.
3. **Dashboards run + swim + bike** (proof: static test)
   - `deploy/helm/t0/dashboards/session-detail-{run,swim,bike}.json`: full-width laps table below summary stats, empty state text.
   - `tests/grafana/test_dashboards.py`: add `mart.activity_lap`, `mart.swim_length` columns to the registry.
4. **Dashboard tri** (proof: static test)
   - `session-detail-tri.json`: single table keyed on parent `$activity_id`, via `parent_activity_id`, Leg column, no transitions.
5. **HITL: manual QA + release**
   - QA script (athlete): open 2026-10-11 tri dashboard, expect swim/bike/run legs with ~9 run laps at 1 km, no duplicates; `get_laps` on run leg returns ~9 rows, on parent id returns None; 2026-09-05 tri legs non-empty; a session without laps shows the empty-state text, no `-` grid.
   - Bump `deploy/helm/t0/Chart.yaml` `version` to 0.1.40 with `appVersion` "0.1.40" (0.1.39 taken by PR #5); tag `vX.Y.Z` before deploy (README "Release"). Push only on user request.

## Gates

`.gates.yml`: ruff check, mypy src tests, ruff format --check, pytest. SAST step is a known gap (echo placeholder). Reviewer gets spec + `scripts/review-checklist.ps1` output; T2 -> escalate model tier.

## Risks

- Migration number clash: check latest in `migrations/` at slice 1 start.
- `test_dashboards.py` columns are hand-copied; keep in sync with the view.
- Run auto-lap on the race itself unverified (DB read was denied); QA step 5 confirms.
- CI has no Postgres: DB tests skip without `T0_TEST_POSTGRES_DSN`. Local run: postgres:16 container + `CREATE ROLE t0_mcp_ro` (CNPG creates it in prod), pytest via WSL (`.venv` is Linux).
- Deferred to slice 3: `deploy/helm/t0/README.md` tool → mart table lacks `get_laps` (needs the chart bump slice 3 already makes).

## Metrics

`subagent | tokens | tool_uses | duration | retries | review_iterations | gate_failures | outcome` (tokens from task notifications; `.usage/usage.jsonl` sink absent)
- implementer s1+2 | 58566 | 19 | 230s | 0 | 0 | 0 | done, DB tests unverified (no Docker daemon)
- gate-keeper s1+2 | 15404 | 10 | 53s | 0 | 0 | 0 | GREEN
- reviewer s1+2 (opus) | 45948 | 16 | 74s | 0 | 1 | 0 | REQUEST_CHANGES (2 major: matview not refreshed in DB tests)
- implementer review-fixes s1+2 | 26694 | 12 | 66s | 0 | 1 | 0 | 7/7 fixed, DB tests still unverified
- gate-keeper s1+2 r2 | 16285 | 9 | 52s | 0 | 1 | 0 | GREEN
- reviewer s1+2 r2 (opus) | 69313 | 25 | 92s | 0 | 2 | 0 | APPROVE (root README count fixed by orchestrator)
- gate-keeper s1+2 r3 | n/a | n/a | n/a | 0 | 2 | 0 | GREEN
- gate-keeper s2 DB verify | 17305 | 8 | 130s | 0 | 0 | 0 | GREEN (969 passed incl. DB tests; sast placeholder = known gap)
- implementer s3+4 | 42297 | 35 | 131s | 0 | 0 | 0 | done, SQL run on empty container only
- gate-keeper s3+4 | 18087 | 8 | 88s | 0 | 0 | 0 | GREEN (969 passed; sast placeholder)
- reviewer s3+4 (opus) | 50672 | 15 | 121s | 0 | 1 | 0 | REQUEST_CHANGES (2 blocker pace rounding, 1 major noValue)
- implementer review-fixes s3+4 | 51065 | 33 | 243s | 0 | 1 | 0 | 8/8 fixed, 4 SQL tests red→green
- gate-keeper s3+4 r2 | 17391 | 8 | 84s | 0 | 1 | 0 | GREEN (986 passed; sast placeholder)
- reviewer s3+4 r2 (opus) | 45696 | 16 | 128s | 0 | 2 | 0 | APPROVE (minor: run/bike/lengths SQL untested, 3 DB tests added by orchestrator)
- gate-keeper s3+4 r3 | n/a | n/a | n/a | 0 | 2 | 0 | GREEN (989 passed; sast placeholder)
