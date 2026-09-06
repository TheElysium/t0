"""Tests for :mod:`t0.transform.runner`, against an in-memory database.

What these tests protect is not the nominal path (which is visible) but the three
orchestration decisions that, if they broke, would produce NO visible error:

1. write-order bundle-then-``parsed_at`` (reverse loses sessions silently);
2. missing Garmin summary treated as deferral, not error (otherwise file exits default
   queue and never returns by itself);
3. excluding already-failed files from default selection (else a permanently indigestible
   file would be retried every night, forever).

``FakeConnection`` answers each query based on the table it queries, and records
everything. No bytes of the athlete's real files: decoders are replaced with a double,
and Garmin summary is written by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from t0.transform import runner as runner_module
from t0.transform.runner import (
    PendingFile,
    format_transform_report,
    load_activity_summary,
    resolve_athlete_id,
    select_pending_files,
    transform_pending_files,
)
from tests.transform.conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    RUN_THRESHOLD,
    START,
    make_sample,
    make_session,
    parsed_source,
)

if TYPE_CHECKING:
    from pathlib import Path

    from t0.fit.model import ParsedActivitySource

ACTIVITY_ID = 9001

#: Minimal but complete Garmin summary, written by hand (see module docstring).
SUMMARY = {
    "activityId": ACTIVITY_ID,
    "activityType": {"typeKey": "running"},
    "startTimeLocal": "2026-06-01 09:00:00",
    "startTimeGMT": "2026-06-01 07:00:00",
    "duration": 3600.0,
    "distance": 12000.0,
    "activityName": "Long run",
}


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._result: list[Any] = []

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append((normalized, params))
        upper = normalized.upper()
        if upper.startswith(("UPDATE ", "INSERT ")):
            self._write(upper, params)
        else:
            self._result = self._select(upper, params)

    def _select(self, upper: str, params: Any) -> list[Any]:
        result = self._select_core(upper, params)
        if result is not None:
            return result
        return self._select_raw(upper, params)

    def _select_core(self, upper: str, params: Any) -> list[Any] | None:
        """``core.*`` reads. Returns ``None`` (not handled here) to fall through to
        :meth:`_select_raw`, so neither half alone accumulates too many return points."""
        manual = self._select_manual(upper, params)
        if manual is not None:
            return manual
        if "FROM CORE.THRESHOLD" in upper:
            return list(self._conn.threshold_rows)
        if "SELECT SEX FROM CORE.ATHLETE" in upper:
            # Response indexed by athlete_id, never first row: a more permissive double
            # than the table would let a read attached to the wrong athlete pass.
            sex = self._conn.sexes.get(int(params[0]))
            return [] if sex is None else [(sex,)]
        if "FROM CORE.ATHLETE" in upper:
            return [(a,) for a in self._conn.athlete_ids]
        return None

    def _select_manual(self, upper: str, params: Any) -> list[Any] | None:
        """Reads specific to the manual-upload path (migration 014)."""
        if "SELECT HOME_TIMEZONE FROM CORE.ATHLETE" in upper:
            # Checked BEFORE the generic "FROM CORE.ATHLETE" branch above: both queries
            # contain that substring, and the generic branch answers with athlete_ids,
            # not a timezone name -- a collision that would silently feed a bogus
            # ZoneInfo argument to build_manual_activity_context.
            return [(self._conn.home_timezone,)]
        if "ACTIVITY_ID FROM CORE.ACTIVITY WHERE FIT_FILE_ID" in upper and "MANUAL" in upper:
            activity_id = self._conn.manual_activities.get(int(params[0]))
            return [] if activity_id is None else [(activity_id,)]
        if "NEXTVAL('CORE.MANUAL_ACTIVITY_ID_SEQ')" in upper:
            self._conn.manual_seq_calls += 1
            next_id = 9000000000000000000 + self._conn.manual_seq_calls
            return [(next_id,)]
        if "NEXTVAL('CORE.LEG_ACTIVITY_ID_SEQ')" in upper:
            self._conn.leg_seq_calls += 1
            return [(9200000000000000000 + self._conn.leg_seq_calls,)]
        if "LEG_ORDER, ACTIVITY_ID FROM CORE.ACTIVITY" in upper:
            return sorted(self._conn.leg_rows.items())
        return None

    def _select_raw(self, upper: str, params: Any) -> list[Any]:
        """``raw.*`` reads."""
        if "COUNT(*) FROM RAW.FIT_FILE" in upper:
            return [(len(self._conn.parse_errors),)]
        if "FROM RAW.FIT_FILE" in upper:
            retry = "PARSE_ERROR IS NULL" not in upper
            return [
                (f.fit_file_id, f.garmin_activity_id, f.storage_path)
                for f in self._conn.pending
                if retry or f.fit_file_id not in self._conn.parse_errors
            ]
        if "FROM RAW.API_RESPONSE" in upper:
            summary = self._conn.summaries.get(int(params[0]))
            return [(summary,)] if summary is not None else []
        return []

    def _write(self, upper: str, params: Any) -> None:
        if upper.startswith("UPDATE RAW.FIT_FILE SET PARSED_AT"):
            self._conn.parsed[params[1]] = params[0]
            self._conn.parse_errors.pop(params[1], None)
        elif upper.startswith("UPDATE RAW.FIT_FILE SET PARSE_ERROR"):
            self._conn.parse_errors[params[1]] = params[0]
        elif upper.startswith("INSERT INTO CORE.ACTIVITY ("):
            # Only this one write is tracked (activity_id, fit_file_id, source), to make
            # the manual-upload idempotence tests self-consistent across two passes on
            # the same FakeConnection, mirroring what a real ``activity_manual_file_uq``
            # lookup would answer next time. Every other core.* write is already covered
            # by test_store and is left ignored, as before.
            if params[40] == "manual":
                self._conn.manual_activities[params[36]] = params[0]
        # else: other core.* writes from store_activity_bundle, covered by test_store.
        self._result = []

    def executemany(self, query: str, params_seq: Any) -> None:
        self._conn.executed.append((" ".join(query.split()), list(params_seq)))

    def fetchall(self) -> list[Any]:
        return self._result

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeConnection:
    pending: list[PendingFile] = field(default_factory=list)
    summaries: dict[int, dict[str, Any]] = field(default_factory=dict)
    athlete_ids: list[int] = field(default_factory=lambda: [ATHLETE_ID])
    sexes: dict[int, str] = field(default_factory=lambda: {ATHLETE_ID: "male"})
    threshold_rows: list[Any] = field(default_factory=list)
    executed: list[tuple[str, Any]] = field(default_factory=list)
    parsed: dict[int, str] = field(default_factory=dict)
    parse_errors: dict[int, str] = field(default_factory=dict)
    commits: int = 0
    rollbacks: int = 0
    home_timezone: str = "Europe/Paris"
    #: fit_file_id -> already-stored manual activity_id (migration 014 idempotence).
    manual_activities: dict[int, int] = field(default_factory=dict)
    manual_seq_calls: int = 0
    #: leg_order -> activity_id, already-stored legs of the parent (migration 023).
    leg_rows: dict[int, int] = field(default_factory=dict)
    leg_seq_calls: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.executed.append(("COMMIT", None))

    def rollback(self) -> None:
        self.rollbacks += 1
        self.executed.append(("ROLLBACK", None))

    def statements(self) -> list[str]:
        return [q for q, _ in self.executed]


@pytest.fixture
def fake_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the two decoders: this layer orchestrates, it re-reads no files.

    Samples are re-tagged with the requested ``activity_id``: ``build_core_rows`` refuses
    (rightly) a decoded whose samples don't carry the context's identifier, and this
    double doesn't have to work around that verification.
    """
    gabarit = [make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60)]

    def _decode(path: object, *, activity_id: int) -> ParsedActivitySource:
        return parsed_source([replace(s, activity_id=activity_id) for s in gabarit])

    monkeypatch.setattr(runner_module, "decode_fit_file", _decode)
    monkeypatch.setattr(runner_module, "decode_tcx_file", _decode)


def _conn(tmp_path: Path, **kwargs: Any) -> FakeConnection:
    file = tmp_path / "activity.fit"
    file.write_bytes(b"")  # content is never read: the decoder is a double.
    conn = FakeConnection(**kwargs)
    conn.pending = [PendingFile(1, ACTIVITY_ID, str(file))]
    conn.summaries = {ACTIVITY_ID: dict(SUMMARY)}
    return conn


def test_bundle_is_valid_before_parsed_at_is_set(tmp_path: Path, fake_decode: None) -> None:
    """Central invariant of the module: marking parsed BEFORE writing loses the session.

    A crash after marking would leave a file considered processed whose activity doesn't
    exist -- and no later pass would retake it, since the queue is defined precisely by
    ``parsed_at IS NULL``.
    """
    conn = _conn(tmp_path)
    rapport = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert rapport.stored == 1
    statements = conn.statements()
    index_insert = next(
        i for i, q in enumerate(statements) if q.upper().startswith("INSERT INTO CORE.ACTIVITY ")
    )
    index_commit_bundle = next(
        i for i, q in enumerate(statements) if q == "COMMIT" and i > index_insert
    )
    index_parsed = next(
        i
        for i, q in enumerate(statements)
        if q.upper().startswith("UPDATE RAW.FIT_FILE SET PARSED_AT")
    )
    assert index_insert < index_commit_bundle < index_parsed
    assert conn.parsed[1] == runner_module.PARSER_VERSION


def test_missing_garmin_summary_defers_file_without_marking_error(
    tmp_path: Path, fake_decode: None
) -> None:
    """The .FIT and by-date window don't necessarily arrive in the same pass.

    Marking an error here would exit the file from default selection until a manual
    ``--retry-failed``, for a cause that resolves itself on next poll.
    """
    conn = _conn(tmp_path)
    conn.summaries = {}
    rapport = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert (rapport.stored, rapport.skipped, rapport.failed) == (0, 1, 0)
    assert conn.parse_errors == {}
    assert conn.parsed == {}


def test_indigestible_file_is_isolated_and_does_not_kill_pass(
    tmp_path: Path, fake_decode: None
) -> None:
    """Unknown sport concerns ONE session; others needn't suffer."""
    other = tmp_path / "autre.fit"
    other.write_bytes(b"")
    conn = _conn(tmp_path)
    conn.pending.append(PendingFile(2, 9002, str(other)))
    conn.summaries[9002] = {**SUMMARY, "activityId": 9002, "activityType": {"typeKey": "curling"}}

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert (report.stored, report.failed) == (1, 1)
    assert conn.parsed == {1: runner_module.PARSER_VERSION}
    assert "curling" in conn.parse_errors[2]
    # rollback BEFORE writing the message: without it, psycopg would refuse the query and
    # the incident would leave no trace.
    statements = conn.statements()
    index_rollback = statements.index("ROLLBACK")
    index_error = next(
        i
        for i, q in enumerate(statements)
        if q.upper().startswith("UPDATE RAW.FIT_FILE SET PARSE_ERROR")
    )
    assert index_rollback < index_error


def test_already_failed_files_excluded_from_default_selection() -> None:
    """Without this exclusion, a permanently faulty file would be retried every night."""
    conn = FakeConnection(pending=[PendingFile(1, ACTIVITY_ID, "/x.fit")], parse_errors={1: "boum"})

    assert select_pending_files(conn) == ()
    assert len(select_pending_files(conn, retry_failed=True)) == 1

    default_query = conn.statements()[0]
    assert "parse_error IS NULL" in default_query
    # Order is what fit_file_unparsed_idx serves; changing it would require a sort.
    assert "ORDER BY downloaded_at" in default_query


def test_success_clears_prior_parse_error(tmp_path: Path, fake_decode: None) -> None:
    """Otherwise a repaired file would still be flagged as problematic by ops health."""
    conn = _conn(tmp_path, parse_errors={1: "failure from earlier version"})
    transform_pending_files(conn, athlete_id=ATHLETE_ID, retry_failed=True)

    assert conn.parse_errors == {}
    assert conn.parsed == {1: runner_module.PARSER_VERSION}


def test_ambiguous_or_missing_athlete_stops_pass_instead_of_guessing() -> None:
    """Attaching history to the wrong athlete gives wrong thresholds, thus wrong TSS,
    undetected by any constraint."""
    with pytest.raises(ValueError, match="0 row"):
        resolve_athlete_id(FakeConnection(athlete_ids=[]))
    with pytest.raises(ValueError, match="2 row"):
        resolve_athlete_id(FakeConnection(athlete_ids=[1, 2]))
    assert resolve_athlete_id(FakeConnection(athlete_ids=[7])) == 7


def test_summary_is_looked_up_in_most_recent_payload() -> None:
    """``raw.api_response`` is append-only and Garmin republishes: freezing the summary
    at first-known state would go against this table's purpose."""
    conn = FakeConnection(summaries={ACTIVITY_ID: dict(SUMMARY)})
    assert load_activity_summary(conn, ACTIVITY_ID) == SUMMARY
    assert load_activity_summary(conn, 424242) is None

    query = conn.statements()[0]
    # No scope_key for this endpoint: the array must be unfolded.
    assert "jsonb_array_elements" in query
    assert "ORDER BY r.fetched_at DESC" in query


def test_report_details_only_abnormal_cases(tmp_path: Path, fake_decode: None) -> None:
    """On a 200-file catchup, listing successes would drown out incidents."""
    conn = _conn(tmp_path)
    conn.summaries = {}
    text = format_transform_report(transform_pending_files(conn, athlete_id=ATHLETE_ID))

    assert "0 stored, 1 deferred, 0 failed" in text
    assert "Garmin summary missing" in text


def test_thresholds_read_once_for_entire_batch(tmp_path: Path, fake_decode: None) -> None:
    """They are historized by period, not per activity: re-reading per file would make
    hundreds of queries for an invariant result."""
    other = tmp_path / "autre.fit"
    other.write_bytes(b"")
    conn = _conn(tmp_path)
    conn.pending.append(PendingFile(2, 9002, str(other)))
    conn.summaries[9002] = {**SUMMARY, "activityId": 9002}

    transform_pending_files(conn, athlete_id=ATHLETE_ID)

    reads = [q for q in conn.statements() if "FROM core.threshold" in q]
    assert len(reads) == 1


def test_unknown_extension_is_file_error_not_crash(tmp_path: Path) -> None:
    """An archive member that is neither .fit nor .tcx flags an archiving defect to fix,
    but must not crash the pass."""
    weird = tmp_path / "activity.gpx"
    weird.write_bytes(b"")
    conn = FakeConnection(pending=[PendingFile(1, ACTIVITY_ID, str(weird))])
    conn.summaries = {ACTIVITY_ID: dict(SUMMARY)}

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert report.failed == 1
    assert ".gpx" in conn.parse_errors[1]


def test_chosen_threshold_is_one_in_force_at_session_date(
    tmp_path: Path, fake_decode: None
) -> None:
    """The pass must relay thresholds to ``build_core_rows``: without them, no load
    metrics are produced and dashboards stay empty with no error."""
    conn = _conn(tmp_path)
    conn.threshold_rows = [
        (
            RUN_THRESHOLD.threshold_id,
            "run",
            RUN_THRESHOLD.valid_from,
            None,
            None,
            None,
            RUN_THRESHOLD.ftp_pace_s_per_km,
            None,
            None,
            None,
            None,
        ),
        (
            ALL_THRESHOLD.threshold_id,
            "all",
            ALL_THRESHOLD.valid_from,
            None,
            None,
            None,
            None,
            None,
            ALL_THRESHOLD.lthr_bpm,
            ALL_THRESHOLD.hr_max_bpm,
            ALL_THRESHOLD.hr_rest_bpm,
        ),
    ]

    rapport = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert rapport.stored == 1
    inserts = [
        q for q in conn.statements() if q.upper().startswith("INSERT INTO CORE.ACTIVITY_METRIC")
    ]
    assert inserts, "no metric written even though thresholds were available"


def test_batch_limit_is_passed_to_query() -> None:
    """Several short passes beat one long one a pod eviction would reset to zero."""
    conn = FakeConnection()
    select_pending_files(conn, limit=25)
    assert conn.executed[0][1] == (25,)


def test_summary_accepts_already_serialized_jsonb() -> None:
    """Depending on the driver or double, ``payload`` arrives as Python object or text."""
    import json

    conn = FakeConnection()
    conn.summaries = {ACTIVITY_ID: json.dumps(SUMMARY)}  # type: ignore[dict-item]
    assert load_activity_summary(conn, ACTIVITY_ID) == SUMMARY


def test_instant_written_to_db_is_from_decoded_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies the WIRING, not the computation (covered by test_activity_start_time).

    Decode and summary are deliberately offset by 37 seconds: without this gap, the
    assertion would pass regardless of which source is retained, proving nothing.
    """
    file_instant = datetime(2026, 6, 1, 7, 0, 37, tzinfo=UTC)
    gabarit = [make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60)]

    def _decode(path: object, *, activity_id: int) -> ParsedActivitySource:
        samples = [
            replace(s, activity_id=activity_id, t=file_instant + timedelta(seconds=s.elapsed_s))
            for s in gabarit
        ]
        return parsed_source(samples)

    monkeypatch.setattr(runner_module, "decode_fit_file", _decode)

    conn = _conn(tmp_path)
    transform_pending_files(conn, athlete_id=ATHLETE_ID)

    insert = next(p for q, p in conn.executed if q.upper().startswith("INSERT INTO CORE.ACTIVITY "))
    assert file_instant in insert


def _decode_manual_fit(
    path: object, *, activity_id: int, sport: str = "running", sub_sport: str | None = None
) -> ParsedActivitySource:
    """Double for a manual FIT upload: no Garmin summary, so the ``session`` message
    (sport/sub_sport/total_timer_time) is what ``build_manual_activity_context`` needs."""
    from t0.fit.model import SessionSummary

    samples = [
        replace(s, activity_id=activity_id)
        for s in (make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60))
    ]
    raw: dict[str, Any] = {"sport": sport, "total_timer_time": 3600.0}
    if sub_sport is not None:
        raw["sub_sport"] = sub_sport
    return parsed_source(samples, session=SessionSummary(raw=raw))


def test_manual_upload_idempotence_reuses_activity_id_and_does_not_draw_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second pass over the same manual ``fit_file_id`` must find the activity written
    by the first pass instead of drawing a fresh id from ``core.manual_activity_id_seq``:
    otherwise every nightly re-run would mint an orphan duplicate of the same file."""
    monkeypatch.setattr(runner_module, "decode_fit_file", _decode_manual_fit)

    file = tmp_path / "manual.fit"
    file.write_bytes(b"")
    conn = FakeConnection(pending=[PendingFile(3, None, str(file))])

    first = transform_pending_files(conn, athlete_id=ATHLETE_ID)
    assert first.stored == 1
    assert conn.manual_seq_calls == 1
    first_activity_id = conn.manual_activities[3]

    second = transform_pending_files(conn, athlete_id=ATHLETE_ID)
    assert second.stored == 1
    assert conn.manual_activities[3] == first_activity_id
    # No second draw from the sequence: the existing-activity lookup short-circuited it.
    assert conn.manual_seq_calls == 1


def test_manual_upload_isolation_one_bad_file_does_not_kill_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One manual file with an unrecognized sport must fail on its own, leaving the other
    manual file (and any Garmin file) in the same pass to succeed."""

    def _decode(path: object, *, activity_id: int) -> ParsedActivitySource:
        sport = "curling" if "bad" in str(path) else "running"
        return _decode_manual_fit(path, activity_id=activity_id, sport=sport)

    monkeypatch.setattr(runner_module, "decode_fit_file", _decode)

    good = tmp_path / "good.fit"
    good.write_bytes(b"")
    bad = tmp_path / "bad.fit"
    bad.write_bytes(b"")
    conn = FakeConnection(pending=[PendingFile(3, None, str(good)), PendingFile(4, None, str(bad))])

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert (report.stored, report.failed) == (1, 1)
    assert conn.parsed == {3: runner_module.PARSER_VERSION}
    assert "curling" in conn.parse_errors[4]
    # The good file's activity_id must have been persisted despite the bad file's failure.
    assert 3 in conn.manual_activities


# --- Multisport split (migration 023) ----------------------------------------------


def _activity_inserts(conn: FakeConnection) -> list[tuple[Any, ...]]:
    return [
        params
        for query, params in conn.executed
        if query.upper().startswith("INSERT INTO CORE.ACTIVITY ")
        and isinstance(params, tuple)
        and len(params) == 41
    ]


def _decode_multisport_fit(
    path: object, *, activity_id: int, second_sport: str = "running"
) -> ParsedActivitySource:
    """Double for a multisport FIT file: two contiguous sessions (swim then leg 2).

    Contiguous HALF-OPEN windows by construction: the sample AT the second session's
    start must belong to the second leg, mirroring the VERIFIED 2026-09-05 file where
    the bike session starts at the exact second T1's timer ends.
    """
    samples = [
        replace(s, activity_id=activity_id)
        for s in (make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 2000, 30))
    ]
    sessions = (
        make_session(START, sport="swimming", sub_sport="open_water", duration_s=1200.0),
        make_session(START + timedelta(seconds=1200), sport=second_sport, duration_s=800.0),
    )
    return parsed_source(
        samples,
        sessions=sessions,
    )


MULTISPORT_SUMMARY = {
    **SUMMARY,
    "activityType": {"typeKey": "multi_sport"},
    "duration": 4000.0,
}


def test_multisport_garmin_file_stores_parent_and_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-05 defect, end to end: parent + one bundle per session, SAME
    transaction, before ``parsed_at``."""
    monkeypatch.setattr(
        runner_module,
        "decode_fit_file",
        lambda p, *, activity_id: _decode_multisport_fit(p, activity_id=activity_id),
    )
    conn = _conn(tmp_path)
    conn.summaries = {ACTIVITY_ID: dict(MULTISPORT_SUMMARY)}

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert report.stored == 1
    inserts = _activity_inserts(conn)
    assert len(inserts) == 3  # parent + swim leg + run leg
    parent, swim, run = inserts
    assert parent[0] == ACTIVITY_ID
    assert parent[33] is True  # is_parent
    assert swim[0] == 9200000000000000001  # first draw, leg seq
    assert swim[2] == "open_water"
    assert swim[34] == 1  # leg_order
    assert run[0] == 9200000000000000002
    assert run[2] == "running"
    assert run[34] == 2
    for leg in (swim, run):
        assert leg[32] == ACTIVITY_ID  # parent_activity_id
        assert leg[33] is False
        assert leg[40] == "leg"
        assert leg[36] == 1  # fit_file_id: same file as the parent
    statements = conn.statements()
    assert "DELETE FROM core.activity WHERE parent_activity_id = %s AND source = 'leg';" in (
        statements
    )
    # One commit for parent + legs, BEFORE parsed_at: see module write-order docstring.
    insert_indices = [
        i for i, q in enumerate(statements) if q.upper().startswith("INSERT INTO CORE.ACTIVITY ")
    ]
    commit_index = next(i for i, q in enumerate(statements) if q == "COMMIT")
    parsed_index = next(
        i
        for i, q in enumerate(statements)
        if q.upper().startswith("UPDATE RAW.FIT_FILE SET PARSED_AT")
    )
    assert max(insert_indices) < commit_index < parsed_index


def test_leg_split_failure_marks_file_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A triathlon stored MINUS one leg would corrupt every load average silently:
    the split is all-or-nothing, one unknown session sport fails the whole file."""
    monkeypatch.setattr(
        runner_module,
        "decode_fit_file",
        lambda p, *, activity_id: _decode_multisport_fit(
            p, activity_id=activity_id, second_sport="curling"
        ),
    )
    conn = _conn(tmp_path)
    conn.summaries = {ACTIVITY_ID: dict(MULTISPORT_SUMMARY)}

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert report.failed == 1
    assert "curling" in conn.parse_errors[1]
    assert _activity_inserts(conn) == []


def test_leg_ids_are_reused_across_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second pass must re-attach to the SAME legs, never re-draw and orphan the
    previous series (same reasoning as manual idempotence, migration 014)."""
    monkeypatch.setattr(
        runner_module,
        "decode_fit_file",
        lambda p, *, activity_id: _decode_multisport_fit(p, activity_id=activity_id),
    )
    conn = _conn(tmp_path)
    conn.summaries = {ACTIVITY_ID: dict(MULTISPORT_SUMMARY)}
    conn.leg_rows = {1: 9200000000000000099}

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert report.stored == 1
    inserts = _activity_inserts(conn)
    assert len(inserts) == 3
    # Leg order 1 already stored: reused; order 2 (run) drawn fresh from the sequence.
    assert inserts[1][0] == 9200000000000000099
    assert inserts[2][0] == 9200000000000000001
    assert conn.leg_seq_calls == 1


def test_manual_multisport_upload_stores_parent_and_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manually uploaded triathlon FIT follows the SAME split (t0.transform.manual
    detects >1 session), otherwise /uploads stays second-class for race files."""
    monkeypatch.setattr(
        runner_module,
        "decode_fit_file",
        lambda p, *, activity_id: _decode_multisport_fit(p, activity_id=activity_id),
    )

    file = tmp_path / "triathlon.fit"
    file.write_bytes(b"")
    conn = FakeConnection(pending=[PendingFile(5, None, str(file))])

    report = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert report.stored == 1
    inserts = _activity_inserts(conn)
    assert len(inserts) == 3
    parent, swim, run = inserts
    # Parent is the manual activity: provenance tracked, id from the manual range.
    assert parent[40] == "manual"
    assert parent[33] is True
    assert conn.manual_activities[5] == parent[0]
    assert parent[0] >= 9000000000000000000
    for leg in (swim, run):
        assert leg[40] == "leg"
        assert leg[32] == parent[0]
    assert swim[34] == 1
    assert run[34] == 2
