# ARCHITECTURE — t0 in diagrams

Mermaid diagrams of the running system: components, ingestion flow, MCP server,
data model. Operational procedures live in `RUNBOOK.md`; MCP details in `HANDOFF-MCP.md`.

## Contents

1. [Overview](#1-overview)
2. [Ingestion flow (raw-first ELT)](#2-ingestion-flow-raw-first-elt)
3. [MCP server](#3-mcp-server)
4. [Data model and consumers](#4-data-model-and-consumers)

---

## 1. Overview

Five components: `t0-ingest` (this repo), PostgreSQL, Grafana, the optional `t0-mcp`
server, and the Helm chart that deploys them.

```mermaid
flowchart LR
    GC[Garmin Connect]
    ATH[Athlete]
    LLM[Claude Code<br/>MCP client]

    subgraph K8s["Kubernetes cluster (Helm chart)"]
        direction LR
        SCH["CronJob: python -m t0.scheduler<br/>polls Garmin + archives original files<br/>3x daily"]
        API["t0-ingest API :8080<br/>manual upload + RPE<br/>local access only, no auth"]
        TRF["CronJob: python -m t0.transform<br/>parses raw files + wellness<br/>into normalized rows, +15 min"]
        OPS["CronJob: python -m t0.ops refresh<br/>refreshes mart views, +30 min"]
        MCP["t0-mcp :8080<br/>FastMCP, read-only"]
        GRAF[Grafana dashboards]
        MIG[migrate Job<br/>Helm hook]

        PG[(PostgreSQL 16<br/>raw / core / mart)]

        PVC[(PVC<br/>.FIT/.TCX files)]
    end

    GC -- "poll, 1.5 s rate limit" --> SCH
    SCH -- "payloads (raw-first)<br/>→ schema raw" --> PG
    SCH -- "ORIGINAL ZIPs" --> PVC
    ATH -- "port-forward:<br/>/uploads, /rpe" --> API
    API --> PVC
    PVC -- "unparsed files" --> TRF
    TRF -- "bundles → schema core" --> PG
    OPS -- "refresh → schema mart" --> PG
    GRAF -- "reads mart" --> PG
    MCP -- "role t0_mcp_ro,<br/>reads mart + core" --> PG
    LLM -- "HTTP + Bearer" --> MCP
    MIG -.-> PG
```

Not shown for readability: the API also writes `raw.fit_file` rows, the MCP
server also reads `core.threshold` and `core.activity`, and the scheduler never
retries Garmin authentication.

Design rule: no agent or CI calls Garmin Connect and no agent or CI deploys to the
cluster — the athlete does.

## 2. Ingestion flow (raw-first ELT)

Every payload is written to `raw` before any processing. FIT files are fsynced to the
PVC before their DB row exists. Poll windows follow a state machine
`in_progress → done | failed`; a failed window never stops the run.

```mermaid
sequenceDiagram
    participant SCH as scheduler CronJob
    participant GC as Garmin Connect
    participant RAW as raw.api_response
    participant PVC as PVC (FIT)
    participant TRF as transform CronJob
    participant CORE as core.*

    SCH->>GC: login() (tokenstore, zero auth retries)
    loop due windows [endpoint, day] (max 60/endpoint)
        SCH->>RAW: begin_window → in_progress
        SCH->>GC: client.fetch(endpoint) (rate limit + retry 429/5xx)
        GC-->>SCH: payload
        SCH->>RAW: INSERT payload + sha256 (raw-first, deduped)
        SCH->>RAW: complete_window → done
    end
    SCH->>PVC: archive ZIP .FIT/.TCX (fsync before DB row)
    SCH->>RAW: INSERT raw.fit_file
    Note over TRF: +15 min
    TRF->>RAW: queue of unparsed fit_file rows (batch 200)
    TRF->>PVC: read file
    TRF->>TRF: parse FIT/TCX + compute (NP, GAP, TSS, PMC…)
    TRF->>CORE: store idempotent bundle, then set parsed_at
    TRF->>CORE: wellness: merge per local day (source_sha256)
```

Manual path: `POST /uploads` archives the file and inserts `raw.fit_file` with
`garmin_activity_id = NULL`; the transform pass picks it up identically.
`POST /activities/{id}/rpe` writes a canonical `tss` row with `method_code = 'rpe'`.

## 3. MCP server

Read-only FastMCP server exposing exactly eight curated tools — no raw-SQL escape
hatch. Requires `T0_MCP_BEARER_TOKEN` and `T0_MCP_POSTGRES_DSN`; refuses to start
without them.

```mermaid
flowchart LR
    CC[Claude Code CLI<br/>MCP client]

    subgraph MCP["Deployment t0-mcp"]
        GW["Gateway API HTTPRoute<br/>(hostname mandatory)"]
        SVC[ClusterIP Service]
        BEAR[BearerTokenMiddleware<br/>compare_digest]
        APP[FastMCP /mcp<br/>stateless HTTP, JSON]
        subgraph Tools["8 curated tools"]
            T1[get_pmc]
            T2[list_activities / get_activity]
            T3[get_power_curve]
            T4[get_wellness]
            T5[get_coach_flags]
            T6[get_ramp_risk]
            T7[get_thresholds]
        end
        HAND[handlers<br/>asyncio.to_thread]
        POOL[Reader<br/>ConnectionPool 1-4]
        MET[Metrics :9090<br/>Prometheus]
        H[("/healthz /readyz<br/>(no Bearer)")]
    end

    PG[("PostgreSQL<br/>mart.* + core.threshold<br/>role t0_mcp_ro<br/>read_only=on, statement_timeout")]

    CC -- "Authorization: Bearer" --> GW --> SVC --> BEAR
    BEAR -- "401 on bad token" --> APP --> HAND --> POOL --> PG
    APP --> H
    MET -.-> APP
```

Safety layers: static Bearer token (`hmac.compare_digest`), read-only DB role,
`statement_timeout` baked into connections, hard `LIMIT` (default 500) injected into
every query, `{"rows": [...], "truncated": bool}` envelope.

## 4. Data model and consumers

Three schemas: `raw` (immutable API responses and files), `core` (normalized),
`mart` (materialized training-load views). Grafana and MCP read the same marts.

```mermaid
flowchart LR
    subgraph Sources["Sources"]
        G[Garmin API]
        U[Manual upload]
        R[Manual RPE]
    end

    subgraph RAW["raw (immutable)"]
        api[api_response]
        fit[fit_file]
        pw[poll_window]
        ir[ingest_run]
    end

    subgraph CORE["core (normalized)"]
        act[activity, samples, laps]
        am[activity_metric<br/>TSS + method]
        dw[daily_wellness, sleep_session]
        th[threshold]
    end

    subgraph MART["mart (materialized views)"]
        pmc[pmc CTL/ATL/TSB]
        aw[activity_wide]
        pcurve[power_curve_best]
        wb[wellness_baseline]
        rr[ramp_risk]
        cf[coach_flags]
    end

    G --> api & fit & pw & ir
    U --> fit
    R --> am
    fit --> act
    api --> dw
    act --> am
    am & th --> pmc
    act --> aw & pcurve
    dw --> wb & rr & cf

    pmc & aw & pcurve & wb & rr & cf --> GRAF[Grafana]
    pmc & aw & pcurve & wb & rr & cf & th --> MCP[t0-mcp<br/>read-only]
```
