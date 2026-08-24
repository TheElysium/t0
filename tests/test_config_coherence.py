"""Safeguards on values duplicated across code, SQL, and Helm chart.

This file tests no behavior: it tests *agreements*. Each constant verified here exists
deliberately in multiple places (to avoid absurd dependencies between modules, or
because a Helm chart cannot import Python), and each silent divergence has already cost
at least once:

- ``mode="scheduled"`` (L11 batch) violated ``ingest_run_mode_ck``: rigorously inoperant
  in production, invisible in tests because the stub was more permissive than the schema;
- a history start date divergent between scheduler and ``ops backfill-status`` compares
  two different scopes, so reports gaps that aren't -- or worse, masks real ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from garminator.logging_setup import LOG_LEVEL_ENV_VAR
from garminator.ops.backfill import DEFAULT_HISTORY_START_ISO
from garminator.scheduler.catalog import DEFAULT_ENDPOINTS
from garminator.settings import Settings
from garminator.transform.runner import DEFAULT_BATCH_LIMIT
from tests.scheduler.fakes import INGEST_RUN_MODES

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VALUES_YAML = _REPO_ROOT / "deploy" / "helm" / "garminator" / "values.yaml"
_MIGRATION_RAW = _REPO_ROOT / "migrations" / "002_raw.sql"
_INGEST_CONFIGMAP = (
    _REPO_ROOT / "deploy" / "helm" / "garminator" / "templates" / "ingest-configmap.yaml"
)
_TEMPLATES = _REPO_ROOT / "deploy" / "helm" / "garminator" / "templates"

#: Variables ``GARMINATOR_*`` from ConfigMap that deliberately do not correspond to
#: any :class:`Settings` field, with the reason. Any other prefixed key not matching
#: anything is a dead setting, and the test below rejects it.
_OUTSIDE_SETTINGS: dict[str, str] = {
    LOG_LEVEL_ENV_VAR: (
        "read by garminator.logging_setup, deliberately outside Settings: adjusting "
        "verbosity must not require Garmin credentials or DSN"
    ),
}


def test_ingest_run_modes_double_matches_the_sql_check_constraint() -> None:
    """``INGEST_RUN_MODES`` must exactly match migration vocabulary."""
    sql = _MIGRATION_RAW.read_text(encoding="utf-8")
    match = re.search(r"ingest_run_mode_ck\s+CHECK\s*\(\s*mode\s+IN\s*\(([^)]*)\)", sql)
    assert match is not None, "ingest_run_mode_ck constraint not found in 002_raw.sql"

    declared = set(re.findall(r"'([^']+)'", match.group(1)))
    assert declared == set(INGEST_RUN_MODES), (
        "Test stub vocabulary diverged from schema: "
        f"SQL={sorted(declared)} vs tests={sorted(INGEST_RUN_MODES)}"
    )


def test_history_start_agrees_across_settings_ops_and_helm() -> None:
    """The three declarations of history start day must match."""
    settings_default = Settings.model_fields["history_start"].default
    assert settings_default.isoformat() == DEFAULT_HISTORY_START_ISO

    values = _VALUES_YAML.read_text(encoding="utf-8")
    helm_values = re.findall(
        r"^\s*(?:historyStart|backfillStartDate):\s*\"?([\d-]+)\"?\s*$", values, re.M
    )
    assert helm_values, "no history date found in values.yaml"
    assert set(helm_values) == {DEFAULT_HISTORY_START_ISO}, (
        "values.yaml diverged from code: "
        f"Helm={sorted(set(helm_values))} vs code={DEFAULT_HISTORY_START_ISO}"
    )


def _helm_endpoint_lists(values: str) -> dict[str, list[str]]:
    """Extract ``endpoints:`` lists from values.yaml, by top-level block.

    Regex read rather than YAML parser: avoids adding PyYAML to project dependencies for
    this test alone. Target format is the tightly-constrained one versioned here -- an
    ``endpoints:`` key followed by indented ``- name`` elements.
    """
    lists: dict[str, list[str]] = {}
    current_block: str | None = None
    collecting = False
    for line in values.splitlines():
        top_level = re.match(r"^([A-Za-z][\w]*):\s*$", line)
        if top_level:
            current_block = top_level.group(1)
            collecting = False
            continue
        if re.match(r"^\s+endpoints:\s*$", line):
            collecting = current_block is not None
            if collecting and current_block is not None:
                lists[current_block] = []
            continue
        if collecting:
            item = re.match(r"^\s+-\s+(\S+)\s*$", line)
            if item and current_block is not None:
                lists[current_block].append(item.group(1))
            elif line.strip() and not line.strip().startswith("#"):
                collecting = False
    return lists


def test_helm_endpoint_lists_match_the_code_catalog() -> None:
    """``scheduler`` and ``opsBackfill`` must poll and audit the SAME scope.

    An endpoint present on the audit side but missing on the polling side produces an
    eternal backlog (windows that are never due); the reverse hides a real gap. Both must
    follow ``DEFAULT_ENDPOINTS``, which stays the source of truth.
    """
    lists = _helm_endpoint_lists(_VALUES_YAML.read_text(encoding="utf-8"))

    assert set(lists) >= {
        "scheduler",
        "opsBackfill",
    }, f"endpoint blocks found in values.yaml: {sorted(lists)}"
    for block, endpoints in lists.items():
        assert endpoints == list(DEFAULT_ENDPOINTS), (
            f"values.yaml/{block}.endpoints diverged from DEFAULT_ENDPOINTS: "
            f"Helm={endpoints} vs code={list(DEFAULT_ENDPOINTS)}"
        )


def test_every_prefixed_configmap_key_is_actually_read_by_the_code() -> None:
    """A ``GARMINATOR_*`` chart key must map to a ``Settings`` field.

    REAL DEFECT. The ConfigMap exposed ``LOG_LEVEL`` unprefixed, right below a comment
    explaining that an unprefixed key is ignored by ``Settings``. No code ever read it:
    ``ingest.config.logLevel`` was a disconnected knob, and the operator turning it for
    more detail got nothing, without a single message. This test makes that kind of dead
    setting impossible to reintroduce.
    """
    text = _INGEST_CONFIGMAP.read_text(encoding="utf-8")
    keys = {
        m.group(1)
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        if (m := re.match(r"^\s{2}(GARMINATOR_[A-Z0-9_]+):", line))
    }
    assert keys, "no GARMINATOR_* key found in the ingest ConfigMap"

    known = {f"GARMINATOR_{name.upper()}" for name in Settings.model_fields}
    for key in sorted(keys):
        assert key in known or key in _OUTSIDE_SETTINGS, (
            f"{key} is read by nobody: neither a Settings field nor an exception "
            f"documented in _OUTSIDE_SETTINGS. Dead setting -- exactly the defect "
            f"LOG_LEVEL was."
        )


def _minutes_of_day(schedule: str) -> int:
    """Convert ``"15 5 * * *"`` into minutes since midnight.

    Rejects anything that is not a fixed daily schedule: the ordering comparison
    below would be meaningless on a ``*/10`` or a restricted weekday.
    """
    minute, hour, day_of_month, month, day_of_week = schedule.split()
    assert (day_of_month, month, day_of_week) == (
        "*",
        "*",
        "*",
    ), f"non-daily schedule: {schedule!r}"
    return int(hour) * 60 + int(minute)


def _helm_schedule(block: str) -> str:
    """Read ``<block>.schedule`` from values.yaml without a YAML parser."""
    text = _VALUES_YAML.read_text(encoding="utf-8")
    m = re.search(
        rf"^{block}:\n(?:[ \t].*\n|\n)*?  schedule:\s*\"([^\"]+)\"",
        text,
        re.MULTILINE,
    )
    assert m is not None, f"no `schedule` found under block `{block}:` of values.yaml"
    return m.group(1)


def test_transform_is_scheduled_between_polling_and_refresh() -> None:
    """The order of the three daily CronJobs is a requirement, not a preference.

    Polling writes ``raw.*``, transform derives ``core.*`` from it, refresh rebuilds
    ``mart.*``. Swapping two of them breaks NOTHING visible: refreshing views on top
    of ``core.*`` tables not yet fed succeeds -- it copies emptiness -- and the
    dashboards stay blank for a whole day without a single alarm. One changed number
    in values.yaml is enough to cause it; hence this guard.
    """
    polling = _minutes_of_day(_helm_schedule("scheduler"))
    transform = _minutes_of_day(_helm_schedule("transform"))
    refresh = _minutes_of_day(_helm_schedule("opsRefresh"))

    assert polling < transform < refresh, (
        f"daily order broken: polling {polling}, transform {transform}, "
        f"refresh {refresh} (minutes since midnight). Transform must follow polling "
        f"and precede the view refresh."
    )


def test_chart_batch_limit_matches_the_code_default() -> None:
    """``transform.batchLimit`` is passed as an argument: it must stay the code default.

    The CronJob passes ``--limit`` explicitly rather than letting the default apply, so
    the value is readable in the manifest. The price is duplication: if the code default
    changes without the chart following, production runs on the old value while everyone
    reads the new one.
    """
    text = _VALUES_YAML.read_text(encoding="utf-8")
    m = re.search(r"^transform:\n(?:[ \t].*\n|\n)*?  batchLimit:\s*(\d+)", text, re.MULTILINE)
    assert m is not None, "no `batchLimit` under the `transform:` block of values.yaml"
    assert int(m.group(1)) == DEFAULT_BATCH_LIMIT


def test_all_cronjobs_declare_the_same_timezone() -> None:
    """Without ``timeZone``, kube-controller-manager applies UTC.

    The ordering test above compares minutes since midnight: that comparison only
    means something if the CronJobs are read in the SAME timezone. A single manifest
    without ``timeZone`` would be enough to make the real order differ from the one
    values.yaml announces -- and, under daylight saving, to shift it a second time.
    """
    manifests = sorted(_TEMPLATES.glob("*-cronjob.yaml"))
    assert manifests, "no CronJob manifest found: the glob must have drifted"

    without_timezone = [
        m.name
        for m in manifests
        if "timeZone: {{ .Values.ingest.config.timezone | quote }}"
        not in m.read_text(encoding="utf-8")
    ]
    assert not without_timezone, (
        f"CronJob(s) without `timeZone`: {without_timezone}. They would run in UTC "
        f"while the others follow `ingest.config.timezone`."
    )
