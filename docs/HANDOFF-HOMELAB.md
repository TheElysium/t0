# HANDOFF — homelab side of the t0 MCP server

Date: 2026-09-01. Audience: the agent (or maintainer) working in the homelab
repository. The t0 chart (branch `feat/mcp-server`) now ships an MCP server —
a read-only HTTP server giving an LLM access to the training marts. It is
fully implemented and gated chart-side; everything it asks of the platform is
in this document. Design contract: [`HANDOFF-MCP.md`](HANDOFF-MCP.md);
Gateway context: [`HANDOFF-GATEWAY.md`](HANDOFF-GATEWAY.md) (ADR-0007).

## 1. What t0 ships (nothing to do homelab-side, context only)

- A **separate Deployment + Service** (`<release>-mcp`, ClusterIP only), same
  image as ingest, command `python -m t0.mcp`.
- Auth: **static Bearer token**, in Secret `t0-mcp-auth` (namespace `t0`,
  key `token`), created by the athlete — never by the chart, never committed.
- Exposure: one **HTTPRoute** `t0-mcp` on the shared `homelab` Gateway,
  hostname `t0-mcp.homelab.internal`, only when `mcp.gateway.enabled=true`.
- Data access: PostgreSQL role **`t0_mcp_ro`**, read-only, its own DSN
  Secret. Statement timeout + connection-level `default_transaction_read_only`
  are set by the app; the role itself must not have write grants to begin with.

## 2. What the homelab repository must provide

### 2.1 The PostgreSQL role — REQUIRED, BEFORE the t0 deploy

t0 migrations `020_mcp_ro_grants.sql` and `021_mcp_core_grants.sql` run
`GRANT ... TO t0_mcp_ro` and an `ALTER DEFAULT PRIVILEGES FOR ROLE t0 IN
SCHEMA mart`. A GRANT to a missing role **fails loudly and aborts the
migration** — that is deliberate. So the role must exist before the first
deploy of this branch.

Create it via CNPG `managed.roles` in the Cluster spec (the app user `t0`
lacks `CREATEROLE`, so this cannot live in a t0 migration):

```yaml
managed:
  roles:
    - name: t0_mcp_ro
      login: true
      passwordSecret: {}        # CNPG generates and stores the password
      connectionLimit: 4        # matches the MCP pool max_size
```

Notes:

- **No `inRole`/membership of `t0` or `pg_read_all_data`**, no
  `CREATEROLE`/`CREATEDB`/`SUPERUSER`: read access comes ONLY from the t0
  migrations (020/021), scoped to `mart.*`, `core.threshold` and
  `core.activity` — role inheritance would silently widen it to `raw.*`.
- The default privileges are bound to `t0` by the migration itself (run as the
  app user), so future marts created by `t0` are automatically readable —
  nothing to maintain homelab-side after this one-time creation.
- Pick the exact `managed.roles` field names from the CNPG version actually
  running; the block above is the contract, not copy-paste YAML.

### 2.2 The DSN Secret — REQUIRED

The chart references but never creates it. In namespace `t0`:

```bash
kubectl -n t0 create secret generic t0-mcp-ro-dsn \
  --from-literal=dsn='postgresql://t0_mcp_ro:<password>@<cluster-rw-service>:5432/t0'
```

- Key name: **`dsn`** (chart value `mcp.postgres.dsnKey`).
- Host: the CNPG cluster's **`-rw` service** (the MCP only reads, but `-rw` is
  the conventional endpoint; switch to `-ro` if a replica pool exists and the
  convention allows it).
- The password is in the CNPG-generated secret created for the role in §2.1 —
  wire it from there rather than retyping it.

### 2.3 Gateway + DNS — verify only

- `kubectl get gateway -A` → `homelab` in `gateway-system`, `PROGRAMMED`,
  address `192.168.1.212` (ADR-0007).
- `allowedRoutes.from: All` is already set, so the `t0` namespace can attach.
- Wildcard DNS `*.homelab.internal → 192.168.1.212` already covers
  `t0-mcp.homelab.internal` — verify resolution from a LAN client.

### 2.4 TLS for `*.homelab.internal` — known separate task

The MCP is plain HTTP on the LAN, exactly like Grafana today. The internal-CA
task in the homelab repo (planned in HANDOFF-GATEWAY.md) covers the MCP for
free once it lands: same Gateway, same hostname pattern, one more HTTPRoute
under the cert. No t0-side change required.

## 3. What must NOT happen

- **No HTTPRoute for the ingest Service or PostgreSQL.** The upload endpoint is
  unauthenticated; the MCP's own route is the only new exposure of this release
  (HANDOFF-GATEWAY.md §4).
- **No write grant ever reaches `t0_mcp_ro`** — not `INSERT` on one table, not
  "temporarily". The read-only guarantee is layered: role grants (020/021),
  connection `default_transaction_read_only`, statement timeout. Weakening the
  first layer defeats the other two.
- **The Bearer token and the DSN never leave the cluster** (no argo-diff
  leakage, no committed values files). The chart fails to render without a
  token on purpose: it never invents one.

## 4. Post-deploy verification (after the athlete deploys t0)

```bash
kubectl -n t0 get deploy,svc,httproute -l app.kubernetes.io/component=mcp
curl -s  http://t0-mcp.homelab.internal/healthz        # {"status":"ok"} — no token
curl -si http://t0-mcp.homelab.internal/mcp | head -1  # HTTP/1.1 401 — no token
```

A `401` on `/mcp` without a token is the expected unauthenticated posture, not
a fault. `/healthz` and `/readyz` are unauthenticated by necessity (kubelet
probes); `/readyz` answers `{"status": "unavailable"}` on a database outage —
it names no dependency. With the token (from `kubectl -n t0 get secret t0-mcp-auth ... |
base64 -d`), the client wires up as:

```bash
claude mcp add --transport http t0 \
  http://t0-mcp.homelab.internal/mcp \
  --header "Authorization: Bearer <token>"
```

NetworkPolicy: `networkPolicy.enabled=true` now ships an MCP stanza (egress
PostgreSQL + DNS, no ingress — same posture as Grafana). After enabling,
re-test through the Gateway URL, not port-forward — Envoy forwards with a
source identity that is not the client pod's, and a pod-to-pod policy can
silently block exactly this path (HANDOFF-GATEWAY.md §4).

Token rotation: with `mcp.auth.bearerTokenExistingSecret` (the deployed
posture), a rotated `t0-mcp-auth` requires
`kubectl -n t0 rollout restart deploy/t0-mcp` — env vars are read at container
start, and the checksum annotation only covers the chart-created Secret path.

## 5. Checklist

- [ ] CNPG `managed.roles`: `t0_mcp_ro` (login, read-only, connection limit) — before any t0 deploy of this branch
- [ ] Secret `t0-mcp-ro-dsn` in namespace `t0`, key `dsn`, host = cluster `-rw` service
- [ ] Gateway `homelab` still `PROGRAMMED` on `192.168.1.212`, `allowedRoutes: All`
- [ ] `t0-mcp.homelab.internal` resolves via the wildcard record
- [ ] After deploy: HTTPRoute `t0-mcp` attached; `/healthz` 200 unauthenticated; `/mcp` 401 unauthenticated; tool call OK with token
- [ ] TLS task: include `t0-mcp.homelab.internal` in the future `*.homelab.internal` certificate
