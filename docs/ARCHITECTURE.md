# ARCHITECTURE — t0 in diagrams

Components, ingestion flow, MCP server, data model.
Operations: `RUNBOOK.md`. MCP details: `HANDOFF-MCP.md`.

## Contents

1. [Overview](#1-overview)
2. [Ingestion flow (raw-first ELT)](#2-ingestion-flow-raw-first-elt)
3. [MCP server](#3-mcp-server)
4. [Data model and consumers](#4-data-model-and-consumers)

---

## 1. Overview

```mermaid
flowchart LR
    GC[Garmin Connect]
    ATH[Athlete]
    LLM[MCP client]

    subgraph K8s["Kubernetes (Helm chart)"]
        direction LR
        SCH["CronJob scheduler<br/>10h, 15h, 22h"]
        TRF["CronJob transform<br/>+15 min"]
        OPS["CronJob ops-refresh<br/>+30 min"]
        API["ingest API :8080<br/>upload + RPE, no auth"]
        MCP["t0-mcp :8080<br/>read-only"]
        GRAF[Grafana]
        MIG[migrate Job<br/>Helm hook]
        PG[(PostgreSQL<br/>raw / core / mart)]
        PVC[(PVC<br/>.FIT/.TCX)]
    end

    GC -- "poll, 1.5 s rate limit" --> SCH
    SCH -- "payloads → raw" --> PG
    SCH -- "original ZIPs" --> PVC
    ATH -- "port-forward" --> API
    API --> PVC & PG
    PVC --> TRF
    TRF -- "→ core" --> PG
    OPS -- "refresh mart" --> PG
    GRAF -- "reads mart" --> PG
    MCP -- "role t0_mcp_ro" --> PG
    LLM -- "HTTP + Bearer" --> MCP
    MIG -.-> PG
```

- Scheduler never retries Garmin authentication.
- No agent or CI calls Garmin or deploys. The operator does.

## 2. Ingestion flow (raw-first ELT)

- Every payload lands in `raw` before processing.
- Files are fsynced to the PVC before their DB row exists.
- A failed window never stops the run.

```mermaid
sequenceDiagram
    participant SCH as scheduler
    participant GC as Garmin Connect
    participant RAW as raw.*
    participant PVC as PVC
    participant TRF as transform
    participant CORE as core.*

    SCH->>GC: login (tokenstore, no auth retry)
    loop due windows [endpoint, day], max 60/endpoint
        SCH->>RAW: poll_window → in_progress
        SCH->>GC: fetch (rate limit, retry 429/5xx)
        GC-->>SCH: payload
        SCH->>RAW: INSERT api_response + sha256 (deduped)
        SCH->>RAW: poll_window → done | failed
    end
    SCH->>PVC: archive .FIT/.TCX ZIP, fsync
    SCH->>RAW: INSERT fit_file
    Note over TRF: +15 min
    TRF->>RAW: unparsed fit_file rows (batch 200)
    TRF->>PVC: read file
    TRF->>TRF: parse + compute (NP, GAP, TSS…)
    TRF->>CORE: idempotent bundle, then parsed_at
    TRF->>CORE: wellness merged per local day
```

Manual paths:
- `POST /uploads`: archives the file, inserts `raw.fit_file` with `garmin_activity_id = NULL`.
  Transform handles it like any other.
- `POST /activities/{id}/rpe`: writes a `tss` metric with `method_code = 'rpe'`.

## 3. MCP server

Nine curated tools, no raw SQL. Won't start without `T0_MCP_BEARER_TOKEN` and
`T0_MCP_POSTGRES_DSN`.

```mermaid
flowchart LR
    CC[MCP client]

    subgraph MCP["Deployment t0-mcp"]
        GW["HTTPRoute<br/>(hostname required)"]
        SVC[ClusterIP Service]
        BEAR[Bearer check<br/>compare_digest]
        APP[FastMCP /mcp<br/>stateless HTTP]
        subgraph Tools["9 tools"]
            T1[get_pmc]
            T2[list_activities<br/>get_activity]
            T3[get_laps]
            T4[get_power_curve]
            T5[get_wellness]
            T6[get_coach_flags]
            T7[get_ramp_risk]
            T8[get_thresholds]
        end
        POOL[pool 1-4<br/>asyncio.to_thread]
        MET[metrics :9090]
        H["/healthz /readyz<br/>(no Bearer)"]
    end

    PG[("PostgreSQL<br/>role t0_mcp_ro<br/>read_only, statement_timeout")]

    CC -- "Bearer" --> GW --> SVC --> BEAR
    BEAR -- "401 if bad" --> APP --> Tools --> POOL --> PG
    APP --> H
    MET -.-> APP
```

Safety layers:
- Static Bearer token, constant-time compare.
- Read-only role, `statement_timeout` on every connection.
- Hard `LIMIT` (default 500) on every query.
- Envelope `{"rows": [...], "truncated": bool}`.

## 4. Data model and consumers

`raw` (immutable), `core` (normalized), `mart` (materialized views).
Grafana and MCP read the same marts.

```mermaid
flowchart LR
    subgraph SRC["Sources"]
        G[Garmin API]
        U[Manual upload]
        R[Manual RPE]
    end

    subgraph RAW["raw"]
        api[api_response]
        fit[fit_file]
        pw[poll_window]
    end

    subgraph CORE["core"]
        act[activity + legs<br/>sample, activity_lap]
        am[activity_metric<br/>TSS + method]
        hist[activity_hr_histogram<br/>activity_power_curve]
        dw[daily_wellness<br/>sleep_session]
        ref[athlete, threshold<br/>pmc_seed]
    end

    subgraph MART["mart"]
        al[activity_load]
        aw[activity_wide]
        dl[daily_load]
        pmc[pmc<br/>CTL/ATL/TSB]
        rr[ramp_risk]
        wb[wellness_baseline]
        cf[coach_flags]
        pc[power_curve_best]
        z[hr_zone_time<br/>power_pace_zone_time]
        tri[triathlon_session]
    end

    G --> api & fit & pw
    U --> fit
    R --> am
    fit --> act & hist
    api --> dw
    act --> am
    act & am --> aw & dl & tri
    act & hist & ref --> al & z
    hist --> pc
    dl & ref --> pmc
    dl & pmc --> rr
    dw --> wb
    pmc & rr & wb & dw --> cf

    MART --> GRAF[Grafana]
    MART & ref --> MCPS[t0-mcp]
```

Refresh order matters only along `daily_load → pmc → ramp_risk → coach_flags`
and `wellness_baseline → coach_flags`. `ops refresh` handles it.
