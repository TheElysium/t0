"""Tests de :mod:`garminator.transform.runner`, contre une base en mémoire.

Ce que ces tests cherchent à protéger n'est pas le chemin nominal (qui se voit) mais
les trois décisions d'orchestration qui, si elles se déréglaient, ne produiraient
AUCUNE erreur visible :

1. l'ordre d'écriture bundle-puis-``parsed_at`` (l'inverse perd des séances en silence) ;
2. un résumé Garmin absent traité comme un report, pas comme une erreur (sans quoi le
   fichier sortirait de la file par défaut et n'y reviendrait jamais seul) ;
3. l'exclusion des fichiers déjà en échec de la sélection par défaut (sans quoi un
   fichier définitivement indigeste serait retenté chaque nuit, indéfiniment).

``FakeConnection`` répond à chaque requête d'après la table qu'elle interroge, et
enregistre tout. Aucun octet des fichiers réels de l'athlète : les décodeurs sont
remplacés par une doublure, et le résumé Garmin est écrit à la main.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from garminator.transform import runner as runner_module
from garminator.transform.runner import (
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
    make_sample,
    parsed_source,
)

if TYPE_CHECKING:
    from pathlib import Path

    from garminator.fit.model import ParsedActivitySource

ACTIVITY_ID = 9001

#: Résumé Garmin minimal mais complet, écrit à la main (cf. docstring du module).
SUMMARY = {
    "activityId": ACTIVITY_ID,
    "activityType": {"typeKey": "running"},
    "startTimeLocal": "2026-06-01 09:00:00",
    "startTimeGMT": "2026-06-01 07:00:00",
    "duration": 3600.0,
    "distance": 12000.0,
    "activityName": "Sortie longue",
}


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._result: list[Any] = []

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append((normalized, params))
        upper = normalized.upper()
        if "FROM CORE.THRESHOLD" in upper:
            self._result = list(self._conn.threshold_rows)
        elif "SELECT SEX FROM CORE.ATHLETE" in upper:
            # Reponse indexee par athlete_id, jamais la premiere ligne venue : une
            # doublure plus permissive que la table laisserait passer une lecture
            # rattachee au mauvais athlete.
            sex = self._conn.sexes.get(int(params[0]))
            self._result = [] if sex is None else [(sex,)]
        elif "FROM CORE.ATHLETE" in upper:
            self._result = [(a,) for a in self._conn.athlete_ids]
        elif "COUNT(*) FROM RAW.FIT_FILE" in upper:
            self._result = [(len(self._conn.parse_errors),)]
        elif "FROM RAW.FIT_FILE" in upper:
            retry = "PARSE_ERROR IS NULL" not in upper
            self._result = [
                (f.fit_file_id, f.garmin_activity_id, f.storage_path)
                for f in self._conn.pending
                if retry or f.fit_file_id not in self._conn.parse_errors
            ]
        elif "FROM RAW.API_RESPONSE" in upper:
            summary = self._conn.summaries.get(int(params[0]))
            self._result = [(summary,)] if summary is not None else []
        elif upper.startswith("UPDATE RAW.FIT_FILE SET PARSED_AT"):
            self._conn.parsed[params[1]] = params[0]
            self._conn.parse_errors.pop(params[1], None)
            self._result = []
        elif upper.startswith("UPDATE RAW.FIT_FILE SET PARSE_ERROR"):
            self._conn.parse_errors[params[1]] = params[0]
            self._result = []
        else:
            # Écritures core.* de store_activity_bundle : déjà couvertes par test_store.
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
    """Remplace les deux décodeurs : ce lot orchestre, il ne relit aucun fichier.

    Les échantillons sont réétiquetés avec l'``activity_id`` demandé : ``build_core_rows``
    refuse (à raison) un décodé dont les échantillons ne portent pas l'identifiant du
    contexte, et cette doublure n'a pas à contourner cette vérification.
    """
    gabarit = [make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60)]

    def _decode(path: object, *, activity_id: int) -> ParsedActivitySource:
        return parsed_source([replace(s, activity_id=activity_id) for s in gabarit])

    monkeypatch.setattr(runner_module, "decode_fit_file", _decode)
    monkeypatch.setattr(runner_module, "decode_tcx_file", _decode)


def _conn(tmp_path: Path, **kwargs: Any) -> FakeConnection:
    fichier = tmp_path / "activity.fit"
    fichier.write_bytes(b"")  # le contenu n'est jamais lu : le décodeur est doublé.
    conn = FakeConnection(**kwargs)
    conn.pending = [PendingFile(1, ACTIVITY_ID, str(fichier))]
    conn.summaries = {ACTIVITY_ID: dict(SUMMARY)}
    return conn


def test_le_bundle_est_valide_avant_que_parsed_at_soit_pose(
    tmp_path: Path, fake_decode: None
) -> None:
    """L'invariant central du module : marquer parsé AVANT d'écrire perdrait la séance.

    Une panne après le marquage laisserait un fichier réputé traité dont l'activité
    n'existe pas -- et aucun passage ultérieur ne le reprendrait, puisque la file se
    définit précisément par ``parsed_at IS NULL``.
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


def test_resume_garmin_absent_reporte_le_fichier_sans_le_marquer_en_erreur(
    tmp_path: Path, fake_decode: None
) -> None:
    """Le .FIT et la fenêtre by-date n'arrivent pas forcément par le même passage.

    Marquer une erreur ici sortirait le fichier de la sélection par défaut jusqu'à un
    ``--retry-failed`` manuel, pour une cause qui se résout d'elle-même au sondage
    suivant.
    """
    conn = _conn(tmp_path)
    conn.summaries = {}
    rapport = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert (rapport.stored, rapport.skipped, rapport.failed) == (0, 1, 0)
    assert conn.parse_errors == {}
    assert conn.parsed == {}


def test_un_fichier_indigeste_est_isole_et_ne_tue_pas_le_passage(
    tmp_path: Path, fake_decode: None
) -> None:
    """Un sport inconnu concerne UNE séance ; les autres n'ont pas à en pâtir."""
    autre = tmp_path / "autre.fit"
    autre.write_bytes(b"")
    conn = _conn(tmp_path)
    conn.pending.append(PendingFile(2, 9002, str(autre)))
    conn.summaries[9002] = {**SUMMARY, "activityId": 9002, "activityType": {"typeKey": "curling"}}

    rapport = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert (rapport.stored, rapport.failed) == (1, 1)
    assert conn.parsed == {1: runner_module.PARSER_VERSION}
    assert "curling" in conn.parse_errors[2]
    # rollback AVANT l'écriture du message : sans lui, psycopg refuserait la requête et
    # l'incident ne laisserait aucune trace.
    statements = conn.statements()
    index_rollback = statements.index("ROLLBACK")
    index_erreur = next(
        i
        for i, q in enumerate(statements)
        if q.upper().startswith("UPDATE RAW.FIT_FILE SET PARSE_ERROR")
    )
    assert index_rollback < index_erreur


def test_les_fichiers_deja_en_echec_sont_hors_selection_par_defaut() -> None:
    """Sans cette exclusion, un fichier définitivement fautif serait retenté chaque nuit."""
    conn = FakeConnection(pending=[PendingFile(1, ACTIVITY_ID, "/x.fit")], parse_errors={1: "boum"})

    assert select_pending_files(conn) == ()
    assert len(select_pending_files(conn, retry_failed=True)) == 1

    requete_defaut = conn.statements()[0]
    assert "parse_error IS NULL" in requete_defaut
    # L'ordre est celui que sert fit_file_unparsed_idx ; le changer imposerait un tri.
    assert "ORDER BY downloaded_at" in requete_defaut


def test_un_succes_efface_le_parse_error_precedent(tmp_path: Path, fake_decode: None) -> None:
    """Sinon un fichier réparé resterait signalé comme problématique par ops health."""
    conn = _conn(tmp_path, parse_errors={1: "échec d'une version antérieure"})
    transform_pending_files(conn, athlete_id=ATHLETE_ID, retry_failed=True)

    assert conn.parse_errors == {}
    assert conn.parsed == {1: runner_module.PARSER_VERSION}


def test_athlete_ambigu_ou_absent_arrete_le_passage_au_lieu_de_deviner() -> None:
    """Rattacher un historique au mauvais athlète donnerait des seuils faux, donc des TSS
    faux, sans qu'aucune contrainte ne s'en aperçoive."""
    with pytest.raises(ValueError, match="0 ligne"):
        resolve_athlete_id(FakeConnection(athlete_ids=[]))
    with pytest.raises(ValueError, match="2 ligne"):
        resolve_athlete_id(FakeConnection(athlete_ids=[1, 2]))
    assert resolve_athlete_id(FakeConnection(athlete_ids=[7])) == 7


def test_le_resume_est_cherche_dans_le_payload_le_plus_recent() -> None:
    """``raw.api_response`` est append-only et Garmin republie ses données : figer le
    résumé sur le premier état connu irait contre la raison d'être de cette table."""
    conn = FakeConnection(summaries={ACTIVITY_ID: dict(SUMMARY)})
    assert load_activity_summary(conn, ACTIVITY_ID) == SUMMARY
    assert load_activity_summary(conn, 424242) is None

    requete = conn.statements()[0]
    # Pas de scope_key pour cet endpoint : le tableau doit être déplié.
    assert "jsonb_array_elements" in requete
    assert "ORDER BY r.fetched_at DESC" in requete


def test_le_rapport_ne_detaille_que_les_cas_anormaux(tmp_path: Path, fake_decode: None) -> None:
    """Sur un rattrapage de 200 fichiers, lister les succès noierait les incidents."""
    conn = _conn(tmp_path)
    conn.summaries = {}
    texte = format_transform_report(transform_pending_files(conn, athlete_id=ATHLETE_ID))

    assert "0 écrite(s), 1 reportée(s), 0 en échec" in texte
    assert "résumé Garmin absent" in texte


def test_les_seuils_sont_lus_une_seule_fois_pour_tout_le_lot(
    tmp_path: Path, fake_decode: None
) -> None:
    """Ils sont historisés par période, pas par activité : les relire par fichier ferait
    des centaines de requêtes pour un résultat invariant."""
    autre = tmp_path / "autre.fit"
    autre.write_bytes(b"")
    conn = _conn(tmp_path)
    conn.pending.append(PendingFile(2, 9002, str(autre)))
    conn.summaries[9002] = {**SUMMARY, "activityId": 9002}

    transform_pending_files(conn, athlete_id=ATHLETE_ID)

    lectures = [q for q in conn.statements() if "FROM core.threshold" in q]
    assert len(lectures) == 1


def test_extension_inconnue_est_une_erreur_de_fichier_pas_un_plantage(tmp_path: Path) -> None:
    """Un membre d'archive ni .fit ni .tcx signale un défaut d'archivage à corriger, mais
    ne doit pas emporter le passage."""
    bizarre = tmp_path / "activity.gpx"
    bizarre.write_bytes(b"")
    conn = FakeConnection(pending=[PendingFile(1, ACTIVITY_ID, str(bizarre))])
    conn.summaries = {ACTIVITY_ID: dict(SUMMARY)}

    rapport = transform_pending_files(conn, athlete_id=ATHLETE_ID)

    assert rapport.failed == 1
    assert ".gpx" in conn.parse_errors[1]


def test_le_seuil_retenu_est_celui_en_vigueur_a_la_date_de_la_seance(
    tmp_path: Path, fake_decode: None
) -> None:
    """Le passage doit relayer les seuils jusqu'à ``build_core_rows`` : sans eux, aucune
    métrique de charge n'est produite et les tableaux de bord restent vides sans erreur."""
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
    assert inserts, "aucune métrique écrite alors que des seuils étaient disponibles"


def test_la_limite_de_lot_est_transmise_a_la_requete() -> None:
    """Plusieurs passages courts valent mieux qu'un long qu'une éviction ramènerait à zéro."""
    conn = FakeConnection()
    select_pending_files(conn, limit=25)
    assert conn.executed[0][1] == (25,)


def test_le_resume_accepte_un_jsonb_deja_serialise() -> None:
    """Selon le pilote ou la doublure, ``payload`` arrive en objet Python ou en texte."""
    import json

    conn = FakeConnection()
    conn.summaries = {ACTIVITY_ID: json.dumps(SUMMARY)}  # type: ignore[dict-item]
    assert load_activity_summary(conn, ACTIVITY_ID) == SUMMARY


def test_l_instant_ecrit_en_base_est_celui_du_fichier_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifie le CABLAGE, pas le calcul (couvert par test_activity_start_time).

    Le decode et le resume sont volontairement decales de 37 secondes : sans cet ecart,
    l'assertion passerait quelle que soit la source retenue, et ne prouverait rien.
    """
    instant_fichier = datetime(2026, 6, 1, 7, 0, 37, tzinfo=UTC)
    gabarit = [make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60)]

    def _decode(path: object, *, activity_id: int) -> ParsedActivitySource:
        samples = [
            replace(s, activity_id=activity_id, t=instant_fichier + timedelta(seconds=s.elapsed_s))
            for s in gabarit
        ]
        return parsed_source(samples)

    monkeypatch.setattr(runner_module, "decode_fit_file", _decode)

    conn = _conn(tmp_path)
    transform_pending_files(conn, athlete_id=ATHLETE_ID)

    insert = next(p for q, p in conn.executed if q.upper().startswith("INSERT INTO CORE.ACTIVITY "))
    assert instant_fichier in insert
