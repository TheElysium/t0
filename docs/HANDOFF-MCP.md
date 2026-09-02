# HANDOFF — MCP server (read-only data access for an LLM)

Date: 2026-08-31. Source: design interview (`/grill-me`), no code written yet.
This document is the implementation contract for a Model Context Protocol (MCP)
server that exposes the training data to an LLM. It states what to build, why
each choice was made, and the security constraints that must not be relaxed.
Real client on the athlete's side: **Claude Code (CLI)**.

Nothing here is deployed. This is the plan the implementation must follow.

## 1. Purpose and shape

Give an LLM read-only access to the training marts, with **no recurring
friction** (the real objection was the per-session `port-forward`, not the
existence of auth). The server is a **new, separate process** — never merged
into `ingest`.

- Transport: **Streamable HTTP**, **stateless** session mode.
- Language: Python, `mcp` SDK, new subpackage `src/t0/mcp/`.
- Packaging: **same Docker image** as ingest; reuses `t0.db` and
  `t0.settings`. A **new Helm Deployment + Service**, distinct from ingest.
- Only the **MCP Service** gets an HTTPRoute. Ingest stays `ClusterIP`.

Why a separate process: exposing the MCP must never, by construction, expose
the ingest upload endpoint. See §5.

## 2. Exposure and authentication

- **HTTPRoute on the shared `homelab` Gateway** (see
  [`HANDOFF-GATEWAY.md`](HANDOFF-GATEWAY.md)), hostname
  `t0-mcp.homelab.internal` (wildcard `*.homelab.internal` → 192.168.1.212).
- **Static Bearer token**, stored in a K8s Secret, verified by a small **ASGI
  middleware** (~15 lines) in front of the MCP app. Set once in the client
  config; no port-forward per session.

  Client wiring (Claude Code CLI):

  ```bash
  claude mcp add --transport http t0 \
    http://t0-mcp.homelab.internal/mcp \
    --header "Authorization: Bearer <token>"
  ```

- **TLS is decoupled.** Plain HTTP on the LAN for now, exactly like Grafana.
  Real TLS is a separate task in the `homelab` repo (internal CA for
  `*.homelab.internal`); the MCP inherits it unchanged once it lands. Do not
  implement the MCP OAuth 2.1 flow — disproportionate for single-user LAN use,
  and the CLI accepts the static header.

## 3. Data access — dedicated read-only role

The MCP never uses the `t0` app credentials. A dedicated role `t0_mcp_ro`:

- **Role + password created on the CNPG side** (homelab repo, `managed.roles`:
  `login`, `connectionLimit: 4`, no membership, password from the vault) — the
  app user lacks `CREATEROLE`, so role creation cannot live in a t0 migration.
  Verified against CNPG 1.30: `passwordSecret: {}` generates **no** password —
  a role with no referenced Secret is born with a NULL password. The platform
  therefore provisions an explicit basic-auth Secret; its password lives in the
  homelab vault and is never communicated to this repo. Nothing changes on the
  chart side: the DSN Secret (`t0-mcp-ro-dsn`, key `dsn`, pointing at
  `postgres-rw.postgres.svc:5432/t0`) is created by Ansible, never by this
  chart.
- **No pre-existing membership** (`pg_read_all_data`, ...): the cluster also
  hosts the `app` and `cadence` databases. Before migration 020 runs, the role
  can read **nothing at all** — that is the protection, not a gap. Migration
  020 is the **only** read path.
- **Deployment sequencing**: do not deploy the MCP branch before the homelab
  branch is merged and replayed (`ansible-playbook site.yml --tags postgres
  --ask-vault-pass`, then `checks.yml` — the play "t0 MCP database access
  path" validates exactly this prerequisite: role reconciled + DSN Secret
  present). Until then, migration `020_mcp_ro_grants.sql` aborts on its GRANT
  to the missing role — by design.
- **Object grants live in t0 migrations** (`020_mcp_ro_grants.sql` then
  `021_mcp_core_grants.sql`, the fix of 020 which lacked the `core` schema):
  - `GRANT USAGE ON SCHEMA mart TO t0_mcp_ro;`
  - `GRANT SELECT ON ALL TABLES IN SCHEMA mart TO t0_mcp_ro;`
  - `GRANT USAGE ON SCHEMA core TO t0_mcp_ro;`
  - `GRANT SELECT ON core.threshold TO t0_mcp_ro;`
  - `GRANT SELECT ON core.activity TO t0_mcp_ro;` (the `is_parent` join of
    `list_activities`/`get_activity` — not exposed by `mart.activity_wide`)
  - `ALTER DEFAULT PRIVILEGES FOR ROLE t0 IN SCHEMA mart GRANT SELECT ON
    TABLES TO t0_mcp_ro;` (bound to the role that will CREATE future marts —
    the migration runner connects as `t0` — so future marts are covered
    without a manual grant).
- Its **own DSN Secret**, `SecretStr` in settings like every other DSN.
- **`statement_timeout`** set on the connection — a curated query cannot run
  away, which matters more given unbounded output (§4).
- **DSN rotation**: if the password rotates homelab-side (vault edit + replay),
  the Secret `t0-mcp-ro-dsn` is rewritten in the same replay, but the
  environment variable is read at container start — the MCP Deployment must be
  restarted (`kubectl rollout restart deploy/t0-mcp`) to pick it up. The MCP
  must stay stateless across restarts: the pool is created at boot and holds
  no persistent state (already the case, `src/t0/mcp/db.py`).

## 4. Tools — fully curated, no raw SQL

Seven curated tools. **No raw-SQL escape hatch** in v1.

| Tool | Backing mart(s) |
| --- | --- |
| `get_pmc` | `mart.pmc` (CTL/ATL/TSB) |
| `list_activities` | `mart.activity_load` / `mart.activity_wide` |
| `get_activity` | `mart.activity_wide` |
| `get_power_curve` | `mart.power_curve_best` |
| `get_wellness` | `mart.wellness_baseline` |
| `get_coach_flags` | `mart.coach_flags` |
| `get_thresholds` | `core.threshold` |

Output policy (v1):

- **Unbounded raw output** — deliberate "for now" choice. But keep two
  non-negotiable guards, because unbounded output can make the tool call itself
  *fail* (context overflow / token cost): a safety `LIMIT` on every query and
  **double rounding** on numeric fields.
- The server ships an **`instructions` block** recycled from the load-bearing
  `COMMENT ON` text in the migrations (`migrations/007_mart.sql` and later).
- **Interpretation flags are never elided** from tool output:
  - `ctl_reliable` — false during the first ~180 days (initialization
    artifact, not real progression).
  - `tss_method` — HR-derived vs power-derived TSS changes interpretation.
  - `has_estimated_load`, `data_quality` — surface gaps honestly.
  - `is_parent` / `is_canonical` semantics respected so multisport activities
    are not double-counted.

These flags are the whole point: the LLM must not read a number without its
caveat.

## 5. What must NOT happen (security)

- **The ingest upload endpoint is never exposed via the Gateway.** It is
  unauthenticated: whoever reaches it writes into the archive and the database.
  It stays `ClusterIP`, reached via port-forward. The MCP being a *separate
  process/Service* is what guarantees that routing the MCP never routes ingest.
- **The MCP is read-only.** No tool writes. The role `t0_mcp_ro` has no
  `INSERT`/`UPDATE`/`DELETE`/`DDL` grant, so a bug cannot mutate data.
- **The Bearer token and every DSN live in K8s Secrets**, never committed. Same
  care as the Garmin Secret (README "Security — Garmin Secret").
- Real threshold/athlete values stay in the gitignored `deploy/seed/athlete.sql`
  — never surface them from repo fixtures.
- No agent or CI polls Garmin or deploys. **The athlete deploys.**

## 6. Chart gating and ops

- **Gating**: `mcp.enabled` + `mcp.gateway.hostname`. The template **refuses to
  render the HTTPRoute without an explicit hostname** — same idiom as
  `grafana.gateway` and `t0.assertServiceType`.
- **Probes**: `/healthz` (liveness) + `/readyz` (readiness tests the Postgres
  connection), like the ingest Deployment.
- **Post-deploy posture** (expected behavior, not a defect): `/healthz` → 200
  without token, `/mcp` → 401 without token, tool call OK with the token.
  HTTPRoute `t0-mcp` on the shared Gateway only — no route to the ingest.
- **TLS**: nothing chart-side — the future internal `*.homelab.internal`
  certificate covers `t0-mcp.homelab.internal` with no chart change.
- **NetworkPolicy**: the chart's policy is off by default. If ever enabled, add
  ingress Gateway→MCP and egress MCP→namespace `postgres`:5432, and **re-test
  the Gateway path** — Envoy forwards with a source identity that is not the
  client pod's, so a pod-to-pod policy can silently block it (same caveat as
  `HANDOFF-GATEWAY.md §4`).

## 7. Testing

- Tool handlers as **pure functions** (query → shape → round), tested against a
  DB fixture.
- One **protocol smoke test**: the server lists exactly 7 tools and one tool
  round-trips over the transport.
- Fits the existing discipline: `ruff` / `ruff format` / `mypy` / `pytest` in
  CI order.

## 8. Checklist

- [ ] `homelab`: `t0_mcp_ro` role + password via `managed.roles`, DSN Secret
- [ ] t0 migration `020_mcp_ro_grants.sql` (USAGE + SELECT + default privileges)
- [ ] t0 migration `021_mcp_core_grants.sql` (USAGE on core, SELECT on core.threshold/core.activity)
- [ ] `src/t0/mcp/`: server, ASGI Bearer middleware, 7 tool handlers,
      `instructions` block, never-elided flags, safety LIMIT + double rounding
- [ ] `statement_timeout` on the RO connection
- [ ] Helm: gated Deployment + Service + HTTPRoute (`mcp.enabled` /
      `mcp.gateway.hostname`), probes
- [ ] Tests: pure-function handler tests + protocol smoke test
- [ ] TLS: separate `homelab` task, MCP inherits `*.homelab.internal` cert
- [ ] Ingest: still `ClusterIP`-only, no HTTPRoute — by design
