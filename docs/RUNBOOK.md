# RUNBOOK — Running t0

This document covers the operational procedures actually available today. It describes
only what exists: no command, endpoint, or table mentioned here is hypothetical. Where
a piece is still missing, it is stated explicitly, with the reason.

All commands below assume `T0_POSTGRES_DSN` is defined in the environment
(never `--dsn` on the command line in a shared context: see "Why not `--dsn`" below).

## Contents

1. [First startup](#1-first-startup)
2. [Recovering from a failure](#2-recovering-from-a-failure)
3. [A window stuck in `in_progress`](#3-a-window-stuck-in-in_progress)
4. [Restoring from a backup](#4-restoring-from-a-backup)
5. [Refreshing `mart` views manually](#5-refreshing-mart-views-manually)
6. [Garmin returns 401](#6-garmin-returns-401)
7. [Why not `--dsn`](#7-why-not---dsn)
8. [Daily polling (CronJob `scheduler`)](#8-daily-polling-cronjob-scheduler)
9. [Reconciling the PVC and `raw.fit_file`](#9-reconciling-the-pvc-and-rawfit_file)
10. [Daily transformation (CronJob `transform`)](#10-daily-transformation-cronjob-transform)
11. [Uploading a file manually](#11-uploading-a-file-manually)
12. [Seeding the PMC](#12-seeding-the-pmc)

---

## 1. First startup

1. Apply migrations:

   ```
   python -m t0.db migrate
   ```

   Verify the absence of drift: `python -m t0.db status` should display
   "Drifts detected: " with no `!` lines below.

2. The six `mart.*` views are automatically populated on creation by migrations 007/008
   (`CREATE MATERIALIZED VIEW` without `WITH NO DATA`): no additional action is needed at
   this stage for them to exist, but they will be EMPTY as long as no activity has been
   ingested into `core.activity` -- this is normal, not a failure.

3. **Seed the athlete and their thresholds.** MANDATORY step, and the only one that is
   not automatable: copy `deploy/seed/athlete.sql.example` **outside the repository**,
   fill it in, then execute it:

   ```
   cp deploy/seed/athlete.sql.example ~/t0-seed.sql
   $EDITOR ~/t0-seed.sql
   psql "$T0_POSTGRES_DSN" -f ~/t0-seed.sql
   ```

   This is not a migration because these values are personal data (weight, heart rates,
   thresholds): the repository carries the procedure, never the data -- same reasoning as
   for the GPS traces in .FIT files. The price to pay is that **this step must be
   repeated for each new database**.

   Without it, `python -m t0.transform` exits with code 2 ("core.athlete
   contains 0 row(s)"): it refuses to guess the athlete, because a wrong athlete would
   give wrong thresholds, so false TSS -- without violating any constraint or producing
   any visible symptom for months.

4. Observe the starting state:

   ```
   python -m t0.ops health
   ```

   On a new database, expect: no endpoint in "Last success per endpoint" (raw.poll_window
   is empty), zero failed windows, zero stuck windows, and all six `mart` views at 0 rows
   (populated but empty).

5. Trigger the first poll. There is nothing special to do: the CronJob `<release>-scheduler`
   (`scheduler-cronjob.yaml`) runs daily and catches up itself all missing history since
   `T0_HISTORY_START`, at a rate of `scheduler.maxWindowsPerEndpoint` windows per
   endpoint per run -- see section 8. To avoid waiting for the next scheduled run:

   ```
   kubectl create job --from=cronjob/<release>-scheduler first-poll
   ```

   On a very first backfill, this first run also downloads the original files (.FIT/.TCX)
   for each activity encountered. To spread out this batch, a JSON-only run is possible
   via `scheduler.activityFiles: false` (flag `--no-activity-files`): files will be
   retrieved on subsequent runs, once the flag is set back to `true`.

6. Follow the backfill progress:

   ```
   python -m t0.ops backfill-status
   ```

   This command is a read-only REPORT on `raw.poll_window`: it triggers no polling. It is
   the CronJob above that progresses, run after run.

## 2. Recovering from a failure

The polling (`t0.scheduler.service.run_scheduled_poll`, used by the ingestion
service) is designed to be safely replayable: a window already `done` is only
re-downloaded if it falls back into the recent refresh sliding window (`refresh_lookback_days`,
3 days by default). Recovering from a failure (pod crash, network outage, cluster restart)
therefore requires **no manual action**: restarting the ingestion service is enough, it
will resume exactly where it left off.

To check what remains to be done before restarting:

```
python -m t0.ops backfill-status --endpoint <endpoint> [--endpoint <endpoint> ...]
```

Replace `<endpoint>` with the names of endpoints actually polled by the ingestion service
(this runbook does not invent them: consult the ingestion service configuration for the
current list).

## 3. A window stuck in `in_progress`

A window remains `in_progress` when the process was interrupted (kill, OOM, node crash)
between the moment it was marked `in_progress` and when it should have been marked `done`
or `failed`.

**No manual database intervention is necessary or recommended.** The calculation of due
windows (`t0.scheduler.planning.due_windows`) automatically considers any
`in_progress` window whose `last_attempted_at` exceeds `DEFAULT_STALE_IN_PROGRESS_AFTER`
(2 hours) as due: on the next poll cycle, it will be retried normally, without duplication
(the window is simply reprocessed as if it were `pending`).

To observe this:

```
python -m t0.ops health
```

section "Stuck in_progress windows". If a window remains listed there across MULTIPLE
consecutive executions separated by more than 2 hours, it is no longer a normal wait case:
it means the polling service itself is no longer running (otherwise it would have picked
it up). Diagnose the ingestion pod state first (`kubectl get pods`, `kubectl logs`) before
any manipulation of `raw.poll_window`.

Never manually `UPDATE raw.poll_window SET status = 'failed'` to "unblock" a window: it
is unnecessary (the mechanism above handles it) and risks masking a polling job that is
truly stopped (the visible symptom would disappear without the cause being addressed).

## 4. Restoring from a backup

Backups are produced by the existing CronJob
(`deploy/helm/t0/templates/backup-cronjob.yaml`), in `pg_dump --format=custom`
format, in `fitArchive.subPaths.postgresBackups` of the shared PVC, named
`t0-<UTC-timestamp>.dump`.

1. Identify the dump to restore (the most recent, unless explicitly needing a point in
   the past): list the contents of the backup subdirectory on the PVC.
2. Restore with `pg_restore` against a reachable PostgreSQL database:

   ```
   pg_restore --clean --if-exists --no-owner --dbname="$T0_POSTGRES_DSN" t0-<timestamp>.dump
   ```

   `--clean --if-exists` deletes existing objects before recreating them: the restore
   completely replaces the contents of the target database -- only execute against a
   database explicitly intended to receive this restore, never against a production
   database without prior confirmation.

3. After restore, verify the absence of schema drift and the state of the views:

   ```
   python -m t0.db status
   python -m t0.ops health
   ```

   If the `mart` views appear "NEVER POPULATED" after restore (possible if the dump was
   produced by a tool that excludes materialized view data), see the next section:
   `python -m t0.ops refresh` automatically switches to non-concurrent refresh
   for this specific case.

## 5. Refreshing `mart` views manually

```
python -m t0.ops refresh
```

Refreshes the six `mart.*` views in the order imposed by their actual dependencies
(`mart.daily_load` before `mart.pmc`, which reads it -- the other four views have no
imposed order among themselves). Each view is refreshed independently: the failure of one
view does not prevent subsequent ones from being attempted, and the exit code of the
command (`1`) signals a partial failure without needing to reread the logs.

This manual refresh is a COMPLEMENT to the periodic refresh `CronJob`
(`ops-refresh-cronjob.yaml`, schedule in `opsRefresh.schedule`), useful immediately
after a restore or massive catch-up, without waiting for the next scheduled run.

## 6. Garmin returns 401

A 401 means that the session persisted in `GARMINTOKENS` is no longer valid (password
changed, session expired on Garmin's side, or account locked after too many attempts).

1. **Do not restart the service in a loop**: each failed reconnection attempt with bad
   credentials brings the account (without 2FA -- see athlete profile) closer to a total
   lockout.
2. Verify that `T0_GARMIN_USERNAME`/`T0_GARMIN_PASSWORD` (container
   environment variables, fed from the `username`/`password` keys of the Kubernetes Secret
   referenced by `ingest-deployment.yaml` and the scheduler CronJob) are still correct.
   The `T0_` prefix is mandatory: `Settings` is a `BaseSettings` with
   `env_prefix="T0_"` and `extra="ignore"`, so an unprefixed variable is silently
   ignored and the pod fails on startup with a missing required field -- a symptom that
   does NOT look like a credentials problem.
3. If the credentials are correct but the session has simply expired, purge the contents
   of `GARMINTOKENS` (subdirectory `fitArchive.subPaths.garminTokens` of the PVC) to
   force a new full authentication on the next ingestion pod startup -- do this
   consciously, not systematically: each full authentication is itself more visible to
   Garmin than session resumption.
4. If the lockout persists after a successful manual authentication outside the cluster
   (browser), it is no longer a problem this repository can solve with code: it is a
   Garmin account incident, to be handled directly on their interface.

This batch adds no automatic 401 detection: `python -m t0.ops health` will see
such an incident only indirectly, through the accumulation of `failed` windows (the error
message persisted in `raw.poll_window.last_error` by polling, if the 401 propagates there,
will give the exact detail -- the `health` CLI already displays it per window).

## 7. Why not `--dsn`

`python -m t0.ops` accepts `--dsn` for occasional interactive use, but the
environment variable `T0_POSTGRES_DSN` remains the recommended default choice
and the one used by all Helm manifests in this batch: a DSN passed as a command-line
argument carries the PostgreSQL password in the clear and remains visible in the
container's process list (`ps`, `/proc/<pid>/cmdline`, and `spec.containers[].command`
via `kubectl describe pod`) for the entire lifetime of the process -- the environment
variable is only exposed via `/proc/<pid>/environ`, already the channel used by the rest
of the repository (including Garmin credentials) and judged acceptable for this level of
risk.

---

## 8. Daily polling (CronJob `scheduler`)

This is the piece that actually queries Garmin. Manifest:
`deploy/helm/t0/templates/scheduler-cronjob.yaml`, values under `scheduler.*`.

| Key | Default | Effect |
| --- | --- | --- |
| `scheduler.enabled` | `true` | Completely disables polling if `false`. |
| `scheduler.schedule` | `"0 5 * * *"` | Schedule. One run per day is enough: Garmin consolidates sleep and HRV several hours after waking. |
| `scheduler.maxWindowsPerEndpoint` | `60` | Windows processed per endpoint per run. Bounds the duration of a run and the number of requests to a non-public API. |
| `scheduler.activityFiles` | `true` | When `false`, adds `--no-activity-files`: JSON polling only, without downloading .FIT/.TCX. |
| `scheduler.endpoints` | the 10 of `DEFAULT_ENDPOINTS` | Must stay aligned with the code catalog; `tests/test_config_coherence.py` verifies it. |

What a run does, in order: it calculates due windows (every day not yet `done` since
`T0_HISTORY_START`, plus the `T0_REFRESH_LOOKBACK_DAYS` last days
systematically replayed, plus always the current day), polls each endpoint, then archives
on the PVC the original files of activities the poll just listed -- without an additional
request to re-ask for this list.

**`concurrencyPolicy: Forbid` is not a comfort.** Two simultaneous runs would authenticate
two Garmin sessions in parallel on the same account, and a wave of authentications is
precisely what locks a Garmin Connect account. For the same reason `backoffLimit: 0`: a
failed window is already persisted as `'failed'` in the database and will be retried on
the next run, whereas immediately retrying an authentication failure only accelerates the
lockout. Do not restart the Job in a loop after a failure -- see section 6.

Historical catch-up has no dedicated command: it is this same CronJob that, run after run,
consumes the remaining windows. `python -m t0.ops backfill-status` gives its
progress.

---

## 9. Reconciling the PVC and `raw.fit_file`

`migrations/002_raw.sql` establishes the atomicity rule -- **the file is written and
fsynced on the PVC BEFORE its row is INSERTed, never the other way around** -- and
qualifies the two possible disagreements. The CronJob `ops-reconcile` (daily at 07:00)
looks for them. It is **strictly read-only**: the PVC is mounted `readOnly` for it, it
deletes no files and modifies no rows.

| Anomaly | Severity | What it means |
|---|---|---|
| **Orphan file** (on PVC, no row) | benign | Archive succeeded, INSERT failed. This is the case the atomicity rule deliberately makes possible. The file is simply unused. |
| **Orphan row** (in database, no file) | **grave** | The row asserts the existence of a source file. Any reconstruction of `core` from `raw` will rely on it and fail, or silently skip the activity. |
| **Size (or sha256) divergent** | **grave** | Truncation or overwrite: the row no longer describes its file. |

The Job exits with failure only on a **grave** anomaly. An orphan file leaves it in
success, deliberately: a CronJob that fails for an unused file ends up not being read at
all, and it is the "orphan row" alert that cannot be afforded to ignore.

### Run a scan on demand

```bash
kubectl create job -n t0 --from=cronjob/t0-ops-reconcile manual-scan
```

Then read the report:

```bash
kubectl logs -n t0 job/manual-scan
```

### Deep scan

By default the scan compares **sizes**: on ~1,090 files of several MB, re-reading the
entire volume for a daily check is expensive, and size catches the vast majority of
cases. On suspicion of silent corruption (same size, different content), pass
`opsReconcile.deep=true` -- or occasionally run the module by hand from a pod that mounts
the PVC:

```bash
python -m t0.ops reconcile --archive-root /data/fit-archive --deep
```

### What to do with anomalies

- **Orphan files** -- nothing urgent. They only take up space. Before deleting one, verify
  that no row references it (the report already did, but deletion is irreversible and the
  report may be stale). A sudden accumulation is however a symptom to understand, not to
  sweep away.
- **Orphan rows** -- this is the grave case. The source file is lost: the `raw.fit_file`
  row can no longer be replayed. If the activity still exists at Garmin, the repair path
  is to **delete the row** then let polling re-download it (a window changed to `failed`
  will be picked up -- see section 2). If it comes from a manual upload, the original file
  is the only source.
- **All reported rows missing at once** -- this is not data loss, it is almost always a
  **PVC mounted elsewhere** than at write time. Compare `--archive-root` to
  `ingest.config.fitArchivePath` and the `subPath` of the mount. The scan is deliberately
  loud in this case rather than silent.

Useful historical context: until the ZIP download hash fix in `t0.scheduler.activity_files`,
each run on the current day's window re-archived that day's activities. A deployment before
this fix has therefore very likely accumulated orphan files -- benign, but numerous.

## 10. Daily transformation (CronJob `transform`)

```
python -m t0.transform [--limit N] [--athlete-id N] [--retry-failed]
```

This is the link between the archive and the dashboards: it consumes files from
`raw.fit_file` whose `parsed_at` is NULL, decodes them, and writes the `core.*` rows
that the `mart.*` views derive from. The CronJob `<release>-transform` runs it daily,
between polling and view refresh (`transform.schedule` in `values.yaml`).

**This order is not a preference.** Refreshing views before transformation SUCCEEDS: the
refresh simply copies empty data. The dashboards remain blank for an entire day without
any lights turning on. A test (`tests/test_config_coherence.py`) verifies that the three
`schedule` entries in the chart stay in the order poll -> transform -> refresh; do not
work around it.

### Read the exit code

- **`0`** -- normal run, **including with deferred files**. A file whose Garmin summary
  is not yet ingested is an expected transitory state: neither `parsed_at` nor
  `parse_error` are set, the file returns itself to the queue on the next run. Counting
  it as a failure would fail the CronJob almost every night, and a CronJob that fails
  every night is one you stop reading.
- **`1`** -- at least one file failed. The incident is already recorded in
  `raw.fit_file.parse_error`; the exit code just makes it visible without reading logs.
- **`2`** -- the run could not start: DSN missing, or `core.athlete` empty or ambiguous
  (see step 3 of first startup). Distinct from `1` because **nothing was attempted**: it
  is a configuration error, not faulty data.

### A file in failure

```
SELECT fit_file_id, garmin_activity_id, storage_path, parse_error
FROM raw.fit_file WHERE parse_error IS NOT NULL ORDER BY downloaded_at DESC;
```

A file carrying a `parse_error` **leaves the queue** and is no longer retried: without
this, an indigestible file would fail the run every night. Once the cause is fixed
(missing sport mapping, decoder to repair):

```
python -m t0.transform --retry-failed
```

Success clears the previous `parse_error`. Replaying **before** fixing the cause only
reproduces the same failure.

### Catching up on a backlog

Each activity is committed individually: a run interrupted (pod eviction, node restart)
loses only the current file, never the batch. Initial backfill thus clears over multiple
nights, at `transform.batchLimit` files per run. To speed it up without waiting:

```
kubectl create job --from=cronjob/<release>-transform transform-catchup
```

`concurrencyPolicy: Forbid` prevents this manual Job from crossing the scheduled run.

### What transformation never writes before finishing

The write order follows the same principle as archiving (file fsynced on the PVC BEFORE
its row is inserted): the `core.*` bundle is **committed before** `parsed_at` is set. A
cut between the two leaves the file in the queue, and it will be reprocessed -- the write
being idempotent, this does not produce a duplicate. Setting `parsed_at` first would have
silently made the activity disappear from the queue without being written anywhere.

## 11. Uploading a file manually

```bash
curl -F "file=@/path/to/activity.fit" http://localhost:8000/uploads
```

The `POST /uploads` endpoint (`t0.api.upload`) is **local only** for now -- no
Ingress, no TLS, no authentication (decision taken, see the batch report that introduced
it). It serves for an activity that Garmin polling has not or cannot retrieve: import from
another tool, activity deleted on Garmin's side before the `scheduler` CronJob run, etc.

The format (FIT, TCX, or ZIP containing either) is detected **by content**, never by
filename extension. The file is archived on the PVC -- same atomicity rule as polling:
written and fsynced BEFORE its `raw.fit_file` row is inserted -- with `garmin_activity_id = NULL`.
This is the only mark of origin: `raw.fit_file` carries no column dedicated to
distinguishing a manual upload from a Garmin download, the NULL value suffices and is
the convention established by `migrations/012_raw_fit_file_manual_upload.sql`.

Possible responses:

| Code | Meaning |
|---|---|
| `201` | Archived and parsed successfully. The body returns `fit_file_id`. |
| `409` | The content (same `sha256`) is already attached in the database to a DIFFERENT origin -- real Garmin activity, or distinct manual upload. Nothing is lost: the file remains archived, no rows are created or modified. |
| `413` | Request body above the size limit. |
| `422` | Content not recognized (neither FIT nor TCX, empty/multi-member/dangerous ZIP), or archiving succeeded but local parsing failed -- the response body specifies which in both cases, and the archive is never lost even if parsing fails. |

### What a manual upload becomes in `core.*`

A manual upload goes all the way to `core.*`, through a path of its own
(`t0.transform.manual`, migrations 012 and 014). Two things a Garmin download gets
for free had to be produced elsewhere:

1. **The context** -- sport, timezone, duration. `t0.transform.summary` reads them
   from the Garmin JSON summary, which a manual upload has never had.
   `build_manual_activity_context` derives them from the archived file itself: the FIT
   `session` message, or the `Sport` attribute of the TCX `Activity` element. Timezone is
   the only element the file truly cannot carry: `core.athlete.home_timezone` stands in for
   it, the case its own column comment sanctions (`migrations/003_core_ref.sql`).
2. **The primary key.** `core.activity.activity_id` **is** the Garmin identifier -- bigint
   primary key, no sequence, no default. A manual upload draws instead from
   `core.manual_activity_id_seq`, which starts at 9e18, eight orders of magnitude above any
   identifier Garmin has emitted. `core.activity.source` records the provenance, and the
   `activity_manual_id_range` CHECK forbids the column and the identifier range from ever
   diverging. Re-running the transformation does not duplicate anything: the partial unique
   index `activity_manual_file_uq` keys a manual activity on its `fit_file_id`.

**The sport is never guessed.** A FIT `(sport, sub_sport)` pair or a TCX `Sport` value with
no entry in the mapping tables raises `UnknownManualSportError`, naming the observed value,
and the file lands with `parse_error` -- rather than folding to a default that would
silently skew TSS. Two consequences worth knowing before uploading:

- TCX only expresses `Running` | `Biking` | `Other`. It cannot say indoor vs. outdoor, so a
  home-trainer ride uploaded as TCX lands as `cycling`, not `virtual_cycling`. That is the
  intended, honest outcome. `Other` is deliberately unmapped -- too coarse to trust.
- A FIT sport absent from `SPORT_CODE_BY_FIT_SPORT` (`t0.transform.manual`) is
  refused rather than approximated. Adding it means adding the pair there, the row in
  `core.sport` if missing, and the row in `core.sport_fit_map`
  (`migrations/015_core_sport_fit_map.sql`) -- a static test fails if the three drift apart.

---

## 12. Seeding the PMC

`mart.pmc` **stays empty until `core.pmc_seed` holds a row.** The view is a recursive CTE:
it walks the calendar forward from a seed, and with no seed there is no calendar to walk.
Every fitness/fatigue panel in Grafana reads that view, so this step is not optional --
it is simply invisible until someone opens the dashboard.

```bash
python -m t0.ops seed-pmc --athlete-id 1 --dry-run
```

`--dry-run` computes the seed and prints it without writing. Run it first, look at the
numbers, then re-run without the flag to persist.

| Flag | Meaning |
|---|---|
| `--athlete-id` | Required. The `core.athlete.athlete_id` the seed is computed for. |
| `--seed-date` | Start of the 42-day averaging window. Defaults to the first day carrying a known load, read from `mart.daily_load`. |
| `--dry-run` | Compute and print, write nothing. |

**Why the default `--seed-date` is not simply the first activity.** Days with no computable
TSS are not a history start -- an activity whose sport has no threshold in effect (see
section 10 of `tools/validate_schema.sql`) contributes 0, and averaging those in would drag
CTL down and make the athlete look detrained on day one. The default therefore reads the
first day that actually carries load, not the first day that carries an activity.

### Re-seeding

`core.pmc_seed` keeps its history: seeding again inserts a new row, it does not overwrite
the previous one, and `mart.pmc` takes the most recent. That is the intended way to correct
a seed computed too early -- nothing needs deleting. Section 7 of `tools/validate_schema.sql`
checks the consequence: `pmc_rows` must equal the number of days since the seed in effect,
never a multiple of it.

### Reading the result

CTL is a 42-day exponential average, so it needs 42 days of history before it means
anything and 180 before it is trustworthy -- `mart.pmc.ctl_reliable` is exactly that flag,
and it reads `false` for the whole warm-up. A PMC seeded yesterday will show `ctl_reliable
= false` everywhere; that is correct, not a failure.
