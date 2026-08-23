"""Tests de :mod:`garminator.transform.store`, contre une connexion DB en mémoire.

``FakeConnection``/``FakeCursor`` (définies ci-dessous, locales à ce module -- pas
partagées avec ``tests/garmin/fakes.py`` qui sert un tout autre lot) implémentent
seulement la vue structurelle ``garminator.transform.store.DBConnection``. Elles ne se
contentent pas d'enregistrer les requêtes : elles rejouent aussi ``DELETE``/``INSERT``/
``executemany`` sur un état Python en mémoire, table par table, pour que
l'idempotence (deux passages laissent le même état) soit vérifiable sur l'état
lui-même, pas seulement sur la séquence de requêtes.

Fixtures 100% synthétiques (aucun octet des fichiers réels de l'athlète), dans le
style de ``tests/transform/conftest.py``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from garminator.fit.model import LapRow, SampleRow, SwimLengthRow
from garminator.transform.rows import (
    ActivityHrHistogramRow,
    ActivityMetricRow,
    ActivityRow,
    CoreActivityBundle,
)
from garminator.transform.store import store_activity_bundle

START = datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)
ACTIVITY_ID = 42
ATHLETE_ID = 7

_TABLE_RE = re.compile(r"(?:INSERT INTO|DELETE FROM) (core\.\w+)", re.IGNORECASE)


class FakeCursor:
    """Curseur en mémoire : enregistre chaque requête et rejoue DELETE/INSERT/executemany
    sur ``conn.tables`` (un dict table -> liste de tuples de paramètres)."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append((normalized, params))
        table = self._table_of(normalized)
        upper = normalized.upper()
        if upper.startswith("DELETE FROM"):
            activity_id = params[0]
            self._conn.tables.setdefault(table, [])
            self._conn.tables[table] = [
                row for row in self._conn.tables[table] if row[0] != activity_id
            ]
        elif upper.startswith("INSERT INTO CORE.ACTIVITY "):
            self._conn.activity[params[0]] = params

    def executemany(self, query: str, params_seq: Any) -> None:
        normalized = " ".join(query.split())
        rows = list(params_seq)
        self._conn.executed.append((normalized, rows))
        table = self._table_of(normalized)
        self._conn.tables.setdefault(table, [])
        self._conn.tables[table].extend(rows)

    @staticmethod
    def _table_of(normalized_query: str) -> str:
        match = _TABLE_RE.search(normalized_query)
        assert match is not None, f"requête sans table core.* reconnaissable : {normalized_query}"
        return match.group(1).lower()

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeConnection:
    """Connexion DB en mémoire satisfaisant ``garminator.transform.store.DBConnection``."""

    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    activity: dict[int, tuple[Any, ...]] = field(default_factory=dict)
    tables: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


def _activity_row(**overrides: Any) -> ActivityRow:
    base: dict[str, Any] = {
        "activity_id": ACTIVITY_ID,
        "athlete_id": ATHLETE_ID,
        "sport_code": "running",
        "start_time": START,
        "start_time_local": START,
        "local_date": date(2026, 6, 1),
        "tz_offset_s": 7200,
        "duration_s": 3600,
    }
    base.update(overrides)
    return ActivityRow(**base)


def _sample(
    elapsed_s: int, *, power: int | None = None, heart_rate: int | None = None
) -> SampleRow:
    return SampleRow(
        activity_id=ACTIVITY_ID,
        t=START,
        elapsed_s=elapsed_s,
        power=power,
        heart_rate=heart_rate,
    )


def _bundle(**overrides: Any) -> CoreActivityBundle:
    base: dict[str, Any] = {
        "activity": _activity_row(),
        "samples": (),
        "laps": (),
        "swim_lengths": (),
        "hr_histogram": (),
        "power_curve": (),
        "metrics": (),
    }
    base.update(overrides)
    return CoreActivityBundle(**base)


def test_l_activite_est_ecrite_avant_ses_series() -> None:
    conn = FakeConnection()
    bundle = _bundle(samples=(_sample(0, power=200, heart_rate=140),))

    store_activity_bundle(conn, bundle)

    tables_touched = [FakeCursor._table_of(q) for q, _ in conn.executed]
    assert tables_touched[0] == "core.activity"
    assert "core.sample" in tables_touched[1:]


def test_rejouer_le_meme_bundle_laisse_le_meme_etat() -> None:
    conn = FakeConnection()
    bundle = _bundle(
        samples=(_sample(0, power=200, heart_rate=140), _sample(1, power=205, heart_rate=141)),
        hr_histogram=(ActivityHrHistogramRow(activity_id=ACTIVITY_ID, hr_bpm=140, seconds=2),),
    )

    store_activity_bundle(conn, bundle)
    state_after_first = {k: list(v) for k, v in conn.tables.items()}
    store_activity_bundle(conn, bundle)

    assert conn.tables == state_after_first
    assert conn.commit_count == 2
    delete_queries = [
        q for q, _ in conn.executed if q.upper().startswith("DELETE FROM CORE.SAMPLE")
    ]
    assert len(delete_queries) == 2  # une purge à chaque passage, jamais de doublon
    assert len(conn.tables["core.sample"]) == 2


def test_un_none_reste_un_none_et_ne_devient_jamais_zero() -> None:
    conn = FakeConnection()
    # Séance home-trainer avec puissance mais sans FC (cas réel : 4/19 séances de cet
    # athlète) : trimp/hrtss doivent rester NULL, jamais 0.
    bundle = _bundle(
        samples=(_sample(0, power=200, heart_rate=None),),
        metrics=(
            ActivityMetricRow(
                activity_id=ACTIVITY_ID,
                metric="trimp",
                method_code="hr",
                value=None,
                is_canonical=False,
                threshold_id=None,
            ),
        ),
    )

    store_activity_bundle(conn, bundle)

    sample_row = conn.tables["core.sample"][0]
    assert sample_row[8] is None  # heart_rate : ni 0 ni chaîne vide
    metric_row = conn.tables["core.activity_metric"][0]
    assert metric_row[3] is None  # value


def test_les_echantillons_partent_en_un_seul_lot() -> None:
    conn = FakeConnection()
    bundle = _bundle(samples=tuple(_sample(i, power=200) for i in range(500)))

    store_activity_bundle(conn, bundle)

    executemany_sample_calls = [
        params
        for q, params in conn.executed
        if q.upper().startswith("INSERT INTO CORE.SAMPLE") and isinstance(params, list)
    ]
    assert len(executemany_sample_calls) == 1
    assert len(executemany_sample_calls[0]) == 500


def test_natation_sans_echantillon_mais_avec_longueurs() -> None:
    conn = FakeConnection()
    bundle = _bundle(
        activity=_activity_row(sport_code="lap_swimming", duration_s=1800),
        samples=(),
        swim_lengths=(
            SwimLengthRow(
                activity_id=ACTIVITY_ID,
                length_index=0,
                start_time=START,
                duration_s=30.5,
                length_type="active",
            ),
        ),
    )

    result = store_activity_bundle(conn, bundle)

    assert result.samples == 0
    assert result.swim_lengths == 1
    assert conn.tables.get("core.sample", []) == []
    assert len(conn.tables["core.swim_length"]) == 1
    # Le DELETE de core.sample doit tout de même avoir eu lieu (remplacement complet
    # inconditionnel), même si aucune ligne n'existait à supprimer.
    assert any(q.upper().startswith("DELETE FROM CORE.SAMPLE") for q, _ in conn.executed)


def test_changement_de_methode_canonique_entre_deux_passages() -> None:
    """1er passage : hrTSS canonique (pas de puissance). 2e passage : un capteur de
    puissance est apparu, le TSS puissance devient canonique. L'index unique partiel
    ``activity_metric_canonical_uq`` interdirait aux deux lignes canoniques de
    coexister avec des ``method_code`` différents -- le DELETE préalable de ce module
    doit rendre ce cas sans risque, jamais une violation silencieuse."""
    conn = FakeConnection()
    first_pass = _bundle(
        metrics=(
            ActivityMetricRow(
                activity_id=ACTIVITY_ID,
                metric="tss",
                method_code="hr",
                value=80.0,
                is_canonical=True,
                threshold_id=4,
            ),
        )
    )
    store_activity_bundle(conn, first_pass)
    assert len(conn.tables["core.activity_metric"]) == 1
    assert conn.tables["core.activity_metric"][0][2] == "hr"
    assert conn.tables["core.activity_metric"][0][4] is True

    second_pass = _bundle(
        metrics=(
            ActivityMetricRow(
                activity_id=ACTIVITY_ID,
                metric="tss",
                method_code="power",
                value=95.0,
                is_canonical=True,
                threshold_id=1,
            ),
        )
    )
    store_activity_bundle(conn, second_pass)

    rows = conn.tables["core.activity_metric"]
    assert len(rows) == 1  # l'ancienne ligne hr a bien été purgée, pas seulement ignorée
    assert rows[0][2] == "power"
    assert rows[0][4] is True


def test_les_tours_sont_supprimes_puis_reinseres_au_second_passage() -> None:
    conn = FakeConnection()
    bundle_v1 = _bundle(
        laps=(
            LapRow(activity_id=ACTIVITY_ID, lap_index=0, start_time=START, duration_s=600),
            LapRow(activity_id=ACTIVITY_ID, lap_index=1, start_time=START, duration_s=600),
        )
    )
    store_activity_bundle(conn, bundle_v1)
    assert len(conn.tables["core.activity_lap"]) == 2

    # Décodeur corrigé : les deux tours sont recollés en un seul.
    bundle_v2 = _bundle(
        laps=(LapRow(activity_id=ACTIVITY_ID, lap_index=0, start_time=START, duration_s=1200),),
    )
    store_activity_bundle(conn, bundle_v2)

    rows = conn.tables["core.activity_lap"]
    assert len(rows) == 1  # aucune ligne fantôme de l'ancien découpage
    assert rows[0][3] == 1200


def test_ingested_at_jamais_reecrite_et_un_seul_commit() -> None:
    conn = FakeConnection()
    bundle = _bundle()

    result = store_activity_bundle(conn, bundle)

    assert conn.commit_count == 1
    assert result.activity_id == ACTIVITY_ID
    query, params = conn.executed[0]
    assert "INSERT INTO core.activity" in query
    assert "ON CONFLICT (activity_id) DO UPDATE" in query
    assert "ingested_at" not in query.split("ON CONFLICT")[1]  # jamais réécrite
    assert "updated_at = now()" in query
    assert params[0] == ACTIVITY_ID
