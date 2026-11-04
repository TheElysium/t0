# Chart t0

Self-hosted deployment of the t0 pipeline: Garmin ingestion (`t0-ingest`),
PostgreSQL, Grafana, and an optional MCP server (`t0-mcp`, read-only data
access for an LLM). **No TLS, no Ingress resource.** Default exposure is
strictly local: Services are `ClusterIP`, access via `kubectl port-forward`.
Optional LAN exposure of Grafana is possible either through a `LoadBalancer`
Service or an HTTPRoute on a cluster Gateway (see "Service exposure" below) --
the MCP has its own gated Gateway exposure (see "MCP server" below).
The ingest Service stays `ClusterIP` in every case.

This chart deploys nothing on its own -- the athlete runs `helm install`
on their cluster.

## Before installing: create the Garmin Secret

**This chart never creates the Secret containing Garmin credentials.** This Secret is
equivalent to full account access. It must exist in the target namespace before
`helm install`, under the name given by `garminSecret.name` (default
`t0-garmin-credentials`), with keys `username` and `password`.

```bash
kubectl create namespace t0   # if needed

kubectl create secret generic t0-garmin-credentials \
  --namespace t0 \
  --from-literal=username='REPLACE_WITH_GARMIN_EMAIL' \
  --from-literal=password='REPLACE_WITH_GARMIN_PASSWORD'
```

**A Kubernetes Secret is not encrypted.** Its values are only base64-encoded
(`kubectl get secret t0-garmin-credentials -o jsonpath='{.data.password}' | base64 -d`
reveals them instantly). Anyone with access to the API server, unencrypted `etcd`
at rest, or `etcd` backups can read these credentials in plain text. If the
cluster does not encrypt Secrets at rest (`EncryptionConfiguration`), this is
the only real level of protection. Never share this Secret, never commit it, and
clear shell history after the above command if necessary.

If the keys or Secret name differ, adjust `garminSecret.name`,
`garminSecret.usernameKey` and `garminSecret.passwordKey` in `values.yaml`.

## Before installing: choose passwords (mandatory)

**This chart deliberately generates no random passwords.**

The real PostgreSQL password is frozen by `initdb` at **data volume creation**
and is never read from `POSTGRES_PASSWORD` again. A chart that regenerates the
Secret content on every `helm upgrade` would leave the database intact with the
old password and the application configured with the new one: "authentication
refused" on a database that worked the day before, with no apparent change.
Mechanisms based on `lookup` do not protect against this scenario (`lookup`
returns an empty value during `helm template`, during `--dry-run`, and in any
`helm template | kubectl apply` flow), and the `helm.sh/resource-policy: keep`
annotation only protects against *deletion*, never against *updating* content
(it also leaves the Secret orphaned after `helm uninstall`, causing the next
reinstall to fail).

Generate passwords **once**, then keep them in a local values file **excluded
from the git repository**:

```bash
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32   # PostgreSQL
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32   # admin Grafana
```

```yaml
# my-values.yaml - NEVER COMMIT
postgresql:
  auth:
    password: "..."
grafana:
  admin:
    password: "..."
```

Variant without password in a values file: create the Secrets manually and
reference them via `postgresql.auth.existingSecret` (keys `username` and
`password`) and `grafana.admin.existingSecret` (keys `admin-user` and
`admin-password`).

If nothing is provided, `helm template` and `helm install` fail with an explicit
message. This is the intended behavior: better to refuse on day one than an
authentication failure on the first `helm upgrade`.

**Changing `postgresql.auth.password` after the first deployment does not change
the password in the database.** First run `ALTER USER t0 WITH
PASSWORD '...';` on the instance, then update the value.

## External PostgreSQL (CloudNativePG)

The chart can skip its own StatefulSet and connect to a server it does not
manage -- e.g. a CloudNativePG cluster:

```yaml
postgresql:
  enabled: false
  auth:
    existingSecret: t0-postgresql   # keys `username` and `password`
  external:
    host: <cluster>-rw.<namespace>.svc.cluster.local
    port: 5432
    namespace: <namespace>          # used by the NetworkPolicy egress rule
```

Notes:

- The CNPG `<cluster>-app` Secret already carries the `username`/`password`
  keys, but Secrets are namespaced: copy it (or create an equivalent Secret for
  the dedicated `t0` user) into the release namespace first.
- The chart creates no database and no user. Point `postgresql.auth.database`
  and `postgresql.auth.username` at an existing pair, or create `t0`/`t0` on the
  external server before installing.
- The schema migrations still run the same way, through the hook Job.
- The NetworkPolicy egress peer becomes a namespace selector on
  `external.namespace` (a pod selector never crosses a namespace boundary).

## Installation

```bash
helm lint deploy/helm/t0 -f my-values.yaml

helm install t0 deploy/helm/t0 \
  --namespace t0 \
  --create-namespace \
  -f my-values.yaml \
  --set ingest.image.repository=ghcr.io/YOUR_ORG/t0 \
  --set ingest.image.tag=0.1.0
```

Parameters to review before a real first deployment (see `values.yaml`, commented):

- `ingest.image.repository` / `ingest.image.tag` -- image published by CI (batch L0) on GHCR.
- `postgresql.persistence.storageClassName`, `fitArchive.persistence.storageClassName`,
  `grafana.persistence.storageClassName` -- StorageClass of the cluster (empty = cluster
  default StorageClass).
- Volume sizes (`*.persistence.size`), to adjust to the athlete's actual history
  (backfill from 2025-04-03, batch L10).
- `backup.schedule` and `backup.retention` if the default weekly rhythm does not suit.
- `scheduling.nodeSelector` / `scheduling.affinity` / `scheduling.tolerations` -- applied to
  every workload of the release. Mandatory on a **multi-node** cluster with node-local
  storage: the `fit-archive` PVC is `ReadWriteOnce` and is mounted by five workloads
  (`ingest`, `scheduler`, `transform`, `ops-reconcile`, `backup`), so any pod scheduled on
  another node stays `Pending` forever. Pin the release to the node holding the volume:

  ```yaml
  scheduling:
    nodeSelector:
      kubernetes.io/hostname: <node-holding-the-volume>
  ```

  A `ReadWriteMany` StorageClass (NFS, Longhorn) declared in
  `fitArchive.persistence.storageClassName` plus `accessModes: [ReadWriteMany]` removes the
  need for this pinning. Nothing to set on a single-node cluster.
- `grafana.plugins` -- Grafana.com plugins installed at pod start. The default is an
  empty list: none of the shipped dashboards needs a plugin. While this list is
  non-empty, the NetworkPolicy opens outbound HTTPS for the Grafana pod (plugin
  download at startup); keeping it empty restores PostgreSQL-only egress.

## Schema migrations (hook Job `migrate`)

The chart creates the schema itself. `templates/migrate-job.yaml` runs
`python -m t0.db migrate` as a Helm hook:

| Phase          | When it runs                                                      |
| -------------- | ----------------------------------------------------------------- |
| `post-install` | after the PostgreSQL StatefulSet exists -- on a fresh install there is nothing to connect to before the main phase. |
| `pre-upgrade`  | before the new image starts, so the schema is ready when it reads. |

`helm install` and `helm upgrade` block until the Job completes and **fail if it
fails**: a release reported as `deployed` has a migrated schema.

Points worth knowing:

- An init container waits up to `migrate.waitForPostgresqlSeconds` (300 s) for
  PostgreSQL to accept connections, so a first install does not fail on a database
  that is merely still starting.
- The runner takes a PostgreSQL **session advisory lock** in non-blocking mode: a
  second concurrent run fails immediately instead of half-applying anything. It also
  refuses to run if a previously applied file's checksum has changed.
- The Job uses the **ingest image** -- same artifact, different command. The `.sql`
  files live at `/app/migrations` in the image, pointed at by `T0_MIGRATIONS_DIR`;
  they are not part of the Python wheel.
- A **successful** Job is deleted; a **failed** one is kept, so
  `kubectl logs job/<release>-migrate` still answers after a failed upgrade. The next
  release deletes it before recreating it (`before-hook-creation`), because a Job spec
  is immutable.
- The Job is idempotent: rerunning it on an up-to-date schema applies nothing.
- `migrate.enabled: false` disables it. Only do this to apply migrations by hand --
  the scheduler CronJob fails on its first write against an empty database.

What this hook does **not** do: seed the athlete and their thresholds. That data is
personal and is never in the repository; see `docs/RUNBOOK.md`, section 1.

## Garmin polling (CronJob `scheduler`)

This is the only chart object that calls Garmin Connect. It replaces the polling
loop that never existed in the `ingest` Deployment -- that one only serves the
manual upload endpoint.

```bash
# Trigger an immediate run, without waiting for the scheduled cadence
kubectl create job -n t0 --from=cronjob/t0-scheduler manual-poll

# Follow the current run
kubectl logs -n t0 -l app.kubernetes.io/component=scheduler --tail=100 -f
```

`values.yaml` keys (`scheduler.*`): `enabled`, `schedule` (default `0 10,15,22 * * *`),
`maxWindowsPerEndpoint` (default `60`, limits the number of windows -- and thus
requests -- per run), `activityFiles` (set to `false` to add `--no-activity-files`:
JSON polling alone, without downloading .FIT/.TCX), and `endpoints`, which must
remain aligned with `DEFAULT_ENDPOINTS` on the code side (`tests/test_config_coherence.py`
verifies it).

Historical catchup has no dedicated Job: this same CronJob consumes remaining
windows run after run, from `ingest.config.backfillStartDate`. The CronJob
`ops-backfill-status` publishes its progress, read-only.

`concurrencyPolicy: Forbid` and `backoffLimit: 0` are deliberate: two simultaneous
runs or an immediate retry after failure would multiply SSO authentications on the
same account -- this is exactly what causes a Garmin Connect account to be blocked.

### Transformation `raw` -> `core` (`transform`)

The `transform` CronJob (keys `transform.*`, default `15 10,15,22 * * *`) decodes archived
files and writes `core.*` rows from which `mart.*` views derive. It is scheduled
**between** each polling cycle (`0 10,15,22`) and its view refresh (`30 10,15,22`), and this order is a requirement:
refreshing before transformation succeeds -- by recopying emptiness -- and leaves
dashboards blank with no error signal. `tests/test_config_coherence.py` refuses any
value that would break this order.

The archive PVC is mounted at the **same `mountPath`** as in the polling CronJob
(`storage_path` is an absolute path stored in the database) and as `readOnly: true`:
transformation reads the archive, never modifies it.

`transform.batchLimit` (default `200`, aligned with code default) limits the number of
files per run. Since each activity is committed individually, a run interrupted loses
only the current file. See section 10 of `RUNBOOK` for reading exit codes -- notably
that a **deferred** file (Garmin summary not yet ingested) is not an error.

**Prerequisites**: `core.athlete` and `core.threshold` must be seeded manually
(`deploy/seed/athlete.sql.example`), otherwise this Job exits with code 2 without
writing anything.

### PVC <-> database reconciliation (`ops-reconcile`)

The `ops-reconcile` CronJob (keys `opsReconcile.*`, default `0 7 * * *`, i.e., after
polling and refresh) compares the `raw.fit_file` inventory to the actual content of
the archive volume. It **deletes nothing and writes nothing**: the PVC is mounted as
`readOnly: true`, which is not a convention but a kubelet guarantee.

The two anomalies are not symmetric, and the exit code is limited to the one that
matters: a **row without file** (forbidden by the atomicity rule in
`migrations/002_raw.sql`) fails the Job; an **orphan file** leaves it in success,
deliberately -- a Job that fails over an unused file eventually stops being read, and
that is the other alert we cannot afford to miss.

`opsReconcile.deep: true` recalculates the sha256 of each file instead of comparing
size: this catches constant-size corruption, at the cost of a full volume re-read.
Reserve for diagnostics. Procedure and remediation:
[`docs/RUNBOOK.md`](../../../docs/RUNBOOK.md) section 9.

## Service access (port-forward)

None of the services are exposed outside the cluster. From a station with cluster
access:

```bash
# Grafana (web interface)
kubectl port-forward -n t0 svc/t0-grafana 3000:3000
# then http://localhost:3000 - admin credential: the one you provided
# in `grafana.admin.password`, readable from the Secret if needed:
kubectl get secret -n t0 t0-grafana \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo

# Manual .FIT file upload endpoint (batch L7)
kubectl port-forward -n t0 svc/t0-ingest 8080:8080

# PostgreSQL, for ad-hoc queries (psql, DBeaver, ...)
kubectl port-forward -n t0 svc/t0-postgresql 5432:5432
```

### Service exposure

The chart **refuses** to render a Service type it is not designed for, with an
explicit error message rather than a silent exposure. The two Services do not get
the same rule, because they do not carry the same risk:

| Service | Accepted types | Why |
|---|---|---|
| `<release>-grafana` | `ClusterIP`, `LoadBalancer` | Asks for the admin password. Serving it on a trusted LAN is a reasonable choice. |
| `<release>-ingest` | `ClusterIP` only | The upload endpoint is **unauthenticated**: whoever reaches it writes into the archive and the database. |

Neither serves TLS. A `LoadBalancer` Grafana is plain HTTP on the LAN, so the
password crosses the network in clear text and nothing here should ever be routed
in from outside that network.

To give Grafana a stable LAN address, set the type and let the load balancer know
which address to claim. The annotation key belongs to the implementation, so the
chart assumes none:

```yaml
grafana:
  service:
    type: LoadBalancer
    annotations:
      # Cilium LB-IPAM; MetalLB uses metallb.universe.tf/loadBalancerIPs
      lbipam.cilium.io/ips: "<lan-address>"
```

Grafana then answers on `http://<lan-address>:3000`. A hostname is a separate,
optional layer: an entry in the `hosts` file of each machine, or a record on the
local DNS resolver, pointing at that address. Dropping the `:3000` and the
`hosts`-file edits requires routing through a Gateway -- see below.

### Gateway exposure (HTTPRoute, optional)

For clusters that run the Kubernetes Gateway API (e.g. Cilium's Envoy
Gateway), Grafana can be published under a hostname instead of
a LoadBalancer address. The route consumes no address from the LB-IPAM pool and
is served by the Gateway the cluster already runs -- no extra component:

```yaml
grafana:
  gateway:
    enabled: true
    hostname: t0.example.internal        # MANDATORY, chart refuses to render without it
    parentRefs:                          # defaults below; override for another Gateway
      - name: <gateway-name>
        namespace: gateway-system
        sectionName: http
```

The hostname must be covered by the DNS record pointing at the Gateway address
(e.g. a wildcard `*.example.internal` -> `<gateway-address>`), and the Gateway
listener must allow routes from the release namespace
(`allowedRoutes.namespaces.from: All` on the shared Gateway).

The Service type is independent of this switch. During a migration, keeping
`service.type: LoadBalancer` while enabling the gateway makes Grafana reachable
through **both** paths at once -- attach the route, verify
`curl http://t0.example.internal/login`, then flip the Service back to
`ClusterIP` and drop the address-claiming annotation once the hostname is the
habit. The LoadBalancer path stays the fallback for clusters without a Gateway.

Same trust model as the LoadBalancer mode: plain HTTP behind the admin
password, LAN only, never routed in from outside.

**The ingest Service gets no HTTPRoute, ever** -- the upload endpoint is
unauthenticated; an HTTPRoute would be the same hole with a hostname on it. If
a remotely reachable upload is ever wanted, it needs authentication first.

The PostgreSQL application password (account `t0`, distinct from the
Garmin account) is retrieved the same way:

```bash
kubectl get secret -n t0 t0-postgresql \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

## MCP server (optional, read-only LLM access)

`mcp.enabled: true` (default `false`) deploys the MCP server: a **new, separate
process** -- never merged into `ingest`. It reuses the **same Docker image**
(same artifact, another entry point: the container runs `python -m t0.mcp`
instead of the ingest API server) but gets its own Deployment and Service
(`<release>-mcp`, `ClusterIP` only). This separation is what guarantees, by
construction, that exposing the MCP never exposes the unauthenticated ingest
upload endpoint.

The MCP gives an LLM read-only access to the training marts through curated
tools -- no raw SQL, no writes:

| Tool | Reads |
| --- | --- |
| `get_pmc` | `mart.pmc` (CTL/ATL/TSB) |
| `list_activities` | `mart.activity_wide` |
| `get_activity` | `mart.activity_wide` |
| `get_power_curve` | `mart.power_curve_best` |
| `get_wellness` | `mart.wellness_baseline` |
| `get_ramp_risk` | `mart.ramp_risk` |
| `get_coach_flags` | `mart.coach_flags` |
| `get_thresholds` | `core.threshold` |

Multi-row tools answer `{"rows": [...], "truncated": bool}`. Filters validate
their vocabulary and fail loudly on an unknown value -- never a silent `[]`.

### Required secrets

**The chart never creates the read-only DSN Secret, and generates no token.**

1. **Bearer token** (`mcp.auth.*`) -- static token verified by the MCP ASGI
   middleware; the client sends it on every request. Create it once, then
   reference the Secret (preferred form):

   ```bash
   openssl rand -base64 32 | tr -d '/+=' | cut -c1-32
   kubectl create secret generic t0-mcp-auth \
     --namespace t0 \
     --from-literal=token='<the token>'
   ```

   ```yaml
   mcp:
     auth:
       bearerTokenExistingSecret: t0-mcp-auth   # key: bearerTokenKey, default `token`
   ```

   For offline validation only, `mcp.auth.bearerToken` lets the chart create
   the Secret from an explicit value -- never in a committed values file. With
   neither set, `helm template`/`helm install` **fails** with an explicit
   message: no token is randomly generated, because a token regenerated on
   every `helm upgrade` would silently break the client configuration.

2. **Read-only DSN** (`mcp.postgres.dsnExistingSecret`) -- the MCP connects to
   PostgreSQL through the **dedicated read-only role `t0_mcp_ro`**, never the
   application credentials. The role is created on the server side (CNPG
   `managed.roles`, or any role-creation path) and its DSN Secret with it; the chart
   only **references** it (same logic as `postgresql.auth.existingSecret`):

   ```bash
   kubectl create secret generic t0-mcp-ro-dsn \
     --namespace t0 \
     --from-literal=dsn='postgresql://t0_mcp_ro:<password>@<host>:5432/t0'
   ```

   ```yaml
   mcp:
     postgres:
       dsnExistingSecret: t0-mcp-ro-dsn   # key: dsnKey, default `dsn`
   ```

   If `mcp.enabled` is true and this value is empty, rendering **fails** with
   the Secret creation command in the message. Object grants for `t0_mcp_ro`
   live in the t0 migrations `020_mcp_ro_grants.sql` + `021_mcp_core_grants.sql`
   (USAGE/SELECT on `mart.*` and `core.threshold`/`core.activity`, default
   privileges for future tables).

### LAN exposure through the Gateway

Same mechanism and same gating idiom as `grafana.gateway`: the HTTPRoute is
rendered only when both `mcp.enabled` and `mcp.gateway.enabled` are true, and
the chart **refuses to render without an explicit hostname**:

```yaml
mcp:
  enabled: true
  gateway:
    enabled: true
    hostname: t0-mcp.example.internal     # MANDATORY, chart refuses to render without it
    parentRefs:                           # defaults below; override for another Gateway
      - name: <gateway-name>
        namespace: gateway-system
        sectionName: http
```

The hostname must be covered by the wildcard DNS record pointing at the Gateway
(e.g. a wildcard `*.example.internal` -> `<gateway-address>`). Plain HTTP on
the LAN, like every exposure of this chart; real TLS is a separate task the
MCP inherits unchanged. The MCP Service accepts `ClusterIP` only (`t0.assertServiceType`):
its one exposure mode is this route, behind the Bearer token.

Without the Gateway route, the Service stays reachable via port-forward only.

### Client wiring (Claude Code CLI)

```bash
claude mcp add --transport http t0 \
  http://t0-mcp.example.internal/mcp \
  --header "Authorization: Bearer <token>"
```

Set once -- no per-session `port-forward`. The token is readable from the
Secret if needed:

```bash
kubectl get secret -n t0 t0-mcp-auth \
  -o jsonpath='{.data.token}' | base64 -d; echo
```

### Prometheus metrics

The MCP serves `/metrics` on a dedicated port (`mcp.metrics.port`, default
`9090`) of the same Service: request counters (method/path/status — 401s
included) and latency histogram. The HTTPRoute forwards ALL paths, so the
metrics port is deliberately NOT routed through the Gateway — no LAN exposure.
Scraping is opt-in:

```yaml
mcp:
  metrics:
    port: 9090
    serviceMonitor:
      enabled: true          # renders ServiceMonitor <release>-mcp
      interval: 30s
      additionalLabels: {}   # extra labels, e.g. the Prometheus release selector
```

### Guardrails

- `mcp.config.statementTimeoutMs` (default `10000`) -- `statement_timeout` on
  the read-only connection: a curated query cannot run away.
- `mcp.config.maxRows` (default `500`) -- safety `LIMIT` ceiling applied to
  every tool query: unbounded output can make the tool call itself fail
  (context overflow / token cost).
- Probes: `/healthz` (liveness) and `/readyz` (readiness -- tests the Postgres
  connection), same pattern as the ingest Deployment.

### NetworkPolicy caveat

When `networkPolicy.enabled` is turned on, the chart renders an MCP stanza
(egress PostgreSQL + DNS, no ingress — same posture as Grafana). After
enabling, **re-test the Gateway path explicitly**: Envoy forwards traffic with
a source identity that is not the client pod's, so a pod-to-pod policy can
silently block the route.

**The ingest Service gets no HTTPRoute and the MCP never shares its process:
by design, not by configuration.**

## Persistent volumes

Three PVCs:

| PVC | Content | Notes |
|---|---|---|
| `<release>-postgresql` (via StatefulSet `volumeClaimTemplates`) | PostgreSQL data | `PGDATA` in `pgdata` subdirectory, never at volume root |
| `<release>-fit-archive` | archived `.FIT` files (`fit-files/`), **Garmin token store** (`garmin-tokens/`), PostgreSQL dumps (`postgres-backups/`) | single volume with three subdirectories (`subPath`), to remain at three PVCs |
| `<release>-grafana` | Grafana data (dashboards edited at rest, plugins, etc.) | optional, can be disabled via `grafana.persistence.enabled: false` |

**Beware `ReadWriteOnce`**: the `fit-archive` PVC is mounted both by the ingest
pod and by the backup `CronJob` pod. With the default `ReadWriteOnce` access mode,
both pods must run **on the same node**; on a multi-node cluster, the backup Job
would remain indefinitely `Pending`. On a single-node cluster, there is no issue.
Otherwise, change `fitArchive.persistence.accessModes` to `ReadWriteMany`
(compatible StorageClass required) or separate dumps to their own PVC.

Pods sharing this PVC use **the same `fsGroup`** (that of
`ingest.securityContext.fsGroup`, default 10001). This is essential: kubelet
applies `fsGroup` recursively to the volume on each mount, and two different
values would overwrite each other -- ingest would lose write permission to its
FIT archive and token store after the first backup run.

The ingest pod runs with `readOnlyRootFilesystem: true`. The only writable paths
are therefore the `fit-archive` PVC (FIT archive + token store) and an `emptyDir`
mounted on `/tmp` (bounded by `ingest.tmpDirSizeLimit`). Variables `HOME`,
`TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` and `PYTHONPYCACHEPREFIX` are
redirected to `/tmp` for this reason, and `PYTHONDONTWRITEBYTECODE=1` prevents
any attempt to write `.pyc` to `site-packages` (read-only). Any new dependency
that would write elsewhere must be added to this list.

The Garmin token store (`GARMINTOKENS`, environment variable read by
`t0-ingest`) is **persistent**: without it, each ingest pod restart
would trigger a complete SSO authentication with Garmin, exposing the account to lockout
(see principle #1 of the macro plan and issue #233).

## Restoration from a backup

The `<release>-postgresql-backup` `CronJob` runs `pg_dump --format=custom`
every week (default Sunday 03:00, `backup.schedule`) to
`<pvc fit-archive>/postgres-backups/`, with simple rotation (`backup.retention`
dumps kept).

A dump that has never been restored is a hypothesis, not a backup: `docs/RUNBOOK.md`,
section 4, carries a restore drill that replays the latest dump into a throwaway
database without touching `t0`. Run it once, after the first successful backup.

Three guarantees of the backup script:

- `pg_dump` is **not** piped: its exit code comes back directly and `set -e`
  fails the Job. An empty dump also fails explicitly. A silent backup failure is
  worse than no backup -- monitor `kubectl get jobs -n t0`.
- The dump is written as `.dump.part` then renamed: an interrupted dump never
  bears the final name, is never counted by rotation and can never be chosen for
  restoration.
- Rotation uses neither `ls -1t` nor `tail` nor `xargs -r`: names carry fixed-width
  UTC timestamps, so lexicographic glob order is chronological order. No dependency
  on GNU options whose presence is not guaranteed in alpine image busybox.

To manually restore a dump (run outside ingestion hours, ideally after setting
`ingest.replicaCount: 0` for the duration of the operation):

```bash
# 1. List available dumps, via a temporary pod mounted on the PVC.
kubectl run -n t0 restore-shell --rm -it --restart=Never \
  --image=postgres:18-alpine \
  --overrides='{"spec":{"containers":[{"name":"restore-shell","image":"postgres:18-alpine","command":["sh"],"stdin":true,"tty":true,"volumeMounts":[{"name":"fit-archive","mountPath":"/backups","subPath":"postgres-backups"}]}],"volumes":[{"name":"fit-archive","persistentVolumeClaim":{"claimName":"t0-fit-archive"}}]}}' \
  -- ls -la /backups

# 2. Copy the chosen dump out of the cluster (or use it directly from
#    a pod with network access to PostgreSQL), then restore:
kubectl port-forward -n t0 svc/t0-postgresql 5432:5432 &
pg_restore --host=localhost --port=5432 \
  --username=t0 --dbname=t0 \
  --clean --if-exists \
  path/to/t0-YYYYMMDDTHHMMSSZ.dump
```

**Dump / PVC consistency (batch L10):** a `pg_dump` alone restores only PostgreSQL
state, not archived `.FIT` files or the Garmin token store. For a complete consistent
restoration at the same instant, take a snapshot of the `fit-archive` PVC **at the same
time** as the `pg_dump` -- otherwise the database may reference FIT files absent from
the restored archive, or vice versa. The detailed operational procedure is the scope of
batch L10.

## NetworkPolicy (optional)

Disabled by default (`networkPolicy.enabled: false`): on a self-hosted cluster,
network is generally already trusted, and a misconfigured NetworkPolicy can
silently block Garmin polling (HTTPS outbound) without any Service signaling it.
If enabled, it limits PostgreSQL to Release pods, Grafana to a single outbound
to PostgreSQL, and ingestion to PostgreSQL + DNS + outbound HTTPS (necessary to
reach the Garmin API).

## Grafana dashboards

The `<release>-grafana-dashboards` ConfigMap is built from every `*.json` file in
the chart's `dashboards/` directory (`.Files.Glob`) and mounted at
`/var/lib/grafana/dashboards`, with the provisioning provider already in place
(`/etc/grafana/provisioning/dashboards`). Adding or removing a dashboard is a
matter of adding or removing a file in `dashboards/` -- no template change is
necessary. Set `grafana.dashboards.configMapName` to mount an externally managed
ConfigMap instead, in which case this chart creates none.

Dashboard uids must stay unique: a collision makes Grafana reject the whole
provisioning provider, not just the offending file.

## Offline validation

This chart is designed to be fully validated without touching a cluster:

Six reference value sets are provided in `ci/` (test passwords, never deployed):

```bash
for v in deploy/helm/t0/ci/*.yaml; do
  helm lint     deploy/helm/t0 -f "$v"
  helm template t0 deploy/helm/t0 -f "$v" > /dev/null
done
```

- `ci/01-default-values.yaml` - default configuration, secrets provided.
- `ci/02-external-secrets-values.yaml` - all pre-existing Secrets, NetworkPolicy enabled.
- `ci/03-minimal-values.yaml` - without Grafana, without backup, without persistence.
- `ci/04-lan-grafana-values.yaml` - Grafana on a LoadBalancer with a claimed address.
- `ci/05-gateway-grafana-values.yaml` - Grafana behind an HTTPRoute on the shared Gateway.
- `ci/06-mcp-gateway-values.yaml` - MCP enabled, secrets referenced, HTTPRoute on the shared Gateway.

Without a values file, `helm template` **fails deliberately** (passwords not provided,
see above).

Kubernetes schema validation (`kubeconform`): to be run on the athlete's side; the
tool is not installed in the agent environment and its installation would require
network access.

```bash
helm template t0 deploy/helm/t0 -f my-values.yaml \
  | kubeconform -strict -summary
```

No `helm install`, `helm upgrade` or `kubectl apply` commands are executed by the
agents developing this repository (principle #5 of the macro plan).
