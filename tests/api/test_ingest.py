"""Tests de garminator.api.ingest : logique metier pure du lot L7.

Ne dependent ni de FastAPI, ni de python-multipart, ni de httpx (aucun des deux
derniers n'est installe dans le venv de ce depot -- voir le rapport de fin de lot) :
c'est precisement pour cela que la logique metier vit dans un module separe de la
couche HTTP (garminator.api.upload). Toutes les fixtures sont des octets synthetiques
construits ici, jamais derives de fichiers reels de l'athlete.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

import pytest

from garminator.api.errors import HostileArchiveError, PayloadTooLargeError
from garminator.api.ingest import (
    DEFAULT_MAX_COMPRESSION_RATIO,
    UploadOutcome,
    check_zip_safety,
    ingest_upload,
)
from garminator.fit.errors import ArchiveError
from garminator.garmin.raw_store import FitFileShaConflictError

if TYPE_CHECKING:
    from pathlib import Path

    from tests.api.conftest import FakeRawStoreConnection

# Memes octets-signature que tests/fit/test_archive.py : structure FIT reconnaissable
# par sniff_format (signature ".FIT" au bon offset) mais PAS un flux FIT reellement
# decodable (aucun message definition/CRC valide) -- ideal pour tester "archive reussi,
# parsing echoue".
_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20

_VALID_TCX = b"""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2">
  <Activities>
    <Activity Sport="Biking">
      <Lap StartTime="2026-08-01T06:00:00Z">
        <Track>
          <Trackpoint>
            <Time>2026-08-01T06:00:00Z</Time>
            <Extensions>
              <TPX xmlns="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
                <Watts>100</Watts>
                <Cadence>75</Cadence>
              </TPX>
            </Extensions>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""

# TCX dont le contenu XML est syntaxiquement valide et sniffe comme TCX, mais dont
# aucun Trackpoint ne porte de Time exploitable : decode_tcx doit lever TcxParseError
# (cf. tests/tcx/test_reader_hardening.py pour l'equivalent au niveau lecteur).
_CORRUPT_TCX = b"""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2">
  <Activities>
    <Activity Sport="Running">
      <Lap StartTime="2026-08-01T06:00:00Z">
        <Track>
          <Trackpoint>
            <HeartRateBpm><Value>150</Value></HeartRateBpm>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- Chemin heureux : TCX valide, direct puis via ZIP -------------------------------


def test_ingest_upload_valid_tcx_produces_ok_summary(tmp_path: Path) -> None:
    outcome = ingest_upload(_VALID_TCX, tmp_path)

    assert isinstance(outcome, UploadOutcome)
    assert outcome.parse_error is None
    assert outcome.parsed is not None
    assert outcome.parsed.n_samples == 1
    assert outcome.parsed.has_session is False  # jamais de session en TCX
    assert outcome.archived.source_format == "tcx"
    assert outcome.archived.storage_path.exists()
    assert outcome.archived.sha256 == hashlib.sha256(_VALID_TCX).digest()


def test_ingest_upload_valid_tcx_wrapped_in_zip(tmp_path: Path) -> None:
    zip_data = _zip_bytes({"activity.tcx": _VALID_TCX})

    outcome = ingest_upload(zip_data, tmp_path)

    assert outcome.parse_error is None
    assert outcome.parsed is not None
    assert outcome.parsed.n_samples == 1
    assert outcome.archived.source_format == "tcx"


def test_ingest_upload_uses_content_addressed_filename_not_client_supplied_name(
    tmp_path: Path,
) -> None:
    # Preuve falsifiable : deux appels avec le meme contenu produisent le meme chemin
    # de stockage, alors qu'aucun nom de fichier n'est jamais passe a ingest_upload --
    # si le nommage dependait d'un nom externe, ce test ne pourrait pas le verifier
    # (il n'y a justement aucun nom externe ici) et une implementation qui nommerait
    # au hasard (uuid a chaque appel) le ferait echouer.
    first = ingest_upload(_VALID_TCX, tmp_path)
    second = ingest_upload(_VALID_TCX, tmp_path)

    assert first.archived.storage_path == second.archived.storage_path


# --- Raw-first : archivage reussi malgre un echec de parsing ------------------------


def test_ingest_upload_fit_shaped_but_undecodable_is_archived_then_reported_as_unparsed(
    tmp_path: Path,
) -> None:
    outcome = ingest_upload(_FAKE_FIT, tmp_path)

    # Contre-epreuve : si le principe "raw-first" n'etait pas respecte, le fichier ne
    # serait pas sur le volume malgre l'echec de parsing -- ici il DOIT y etre.
    assert outcome.archived.storage_path.exists()
    assert outcome.archived.source_format == "fit"
    assert outcome.parsed is None
    assert outcome.parse_error is not None


def test_ingest_upload_tcx_without_usable_trackpoint_is_archived_then_reported_as_unparsed(
    tmp_path: Path,
) -> None:
    outcome = ingest_upload(_CORRUPT_TCX, tmp_path)

    assert outcome.archived.storage_path.exists()
    assert outcome.parsed is None
    assert outcome.parse_error is not None
    assert "Trackpoint" in outcome.parse_error


def test_ingest_upload_unrecognized_content_raises_archive_error_with_no_side_effect(
    tmp_path: Path,
) -> None:
    with pytest.raises(ArchiveError):
        ingest_upload(b"ni FIT ni TCX ni ZIP, juste du texte quelconque", tmp_path)

    # Contre-epreuve du raw-first : un contenu non reconnu ne doit RIEN laisser sur
    # le volume (pas de fichier orphelin), sinon cette assertion echouerait.
    assert list(tmp_path.iterdir()) == []


# --- Taille maximale -----------------------------------------------------------------


def test_ingest_upload_rejects_oversized_payload_before_touching_disk(tmp_path: Path) -> None:
    with pytest.raises(PayloadTooLargeError):
        ingest_upload(_VALID_TCX, tmp_path, max_upload_bytes=len(_VALID_TCX) - 1)

    assert list(tmp_path.iterdir()) == []


def test_ingest_upload_accepts_payload_exactly_at_the_limit(tmp_path: Path) -> None:
    # Contre-epreuve de la borne stricte : "> max" rejette, "== max" doit passer --
    # une implementation off-by-one (>=) ferait echouer ce test.
    outcome = ingest_upload(_VALID_TCX, tmp_path, max_upload_bytes=len(_VALID_TCX))
    assert outcome.archived.storage_path.exists()


# --- ZIP hostile : chemins d'echappement ---------------------------------------------


def test_check_zip_safety_rejects_parent_directory_escape() -> None:
    zip_data = _zip_bytes({"../../etc/evil.tcx": _VALID_TCX})

    with pytest.raises(HostileArchiveError, match="suspect"):
        check_zip_safety(zip_data)


def test_check_zip_safety_rejects_absolute_path_member() -> None:
    zip_data = _zip_bytes({"/etc/evil.tcx": _VALID_TCX})

    with pytest.raises(HostileArchiveError, match="suspect"):
        check_zip_safety(zip_data)


def test_check_zip_safety_accepts_plain_relative_member_name() -> None:
    zip_data = _zip_bytes({"activity.tcx": _VALID_TCX})

    check_zip_safety(zip_data)  # ne doit rien lever


def test_ingest_upload_rejects_zip_with_escaping_member_with_no_side_effect(
    tmp_path: Path,
) -> None:
    zip_data = _zip_bytes({"../evil.tcx": _VALID_TCX})

    with pytest.raises(HostileArchiveError):
        ingest_upload(zip_data, tmp_path)

    # Contre-epreuve raw-first : une archive hostile ne doit RIEN archiver, meme pas
    # le contenu qu'elle porte -- le rejet a lieu avant tout appel a archive_download.
    assert list(tmp_path.iterdir()) == []


# --- ZIP hostile : bombe de decompression ---------------------------------------------


def _zip_bomb_bytes(ratio_target: float) -> bytes:
    """Construit une archive ZIP dont un membre annonce un ratio de compression eleve,
    sans jamais materialiser en memoire le contenu decompresse complet (on ecrit
    directement une entree fortement compressible -- des zeros repetes -- assez grande
    pour depasser ``ratio_target`` une fois compressee par le degre defaut de zlib)."""
    payload = b"\x00" * int(4 * 1024 * 1024)  # 4 Mio de zeros : compresse a quelques
    # centaines d'octets par zlib, largement au-dessus du ratio par defaut du module.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.tcx", payload)
    data = buf.getvalue()
    # Verifie que la fixture atteint bien le ratio vise avant de servir au test --
    # sinon le test suivant serait un faux positif silencieux.
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.infolist()[0]
        assert info.file_size / max(info.compress_size, 1) > ratio_target
    return data


def test_check_zip_safety_rejects_high_compression_ratio_member() -> None:
    zip_data = _zip_bomb_bytes(DEFAULT_MAX_COMPRESSION_RATIO)

    with pytest.raises(HostileArchiveError, match="bombe"):
        check_zip_safety(zip_data)


def test_check_zip_safety_rejects_declared_uncompressed_size_above_bound() -> None:
    zip_data = _zip_bytes({"huge.tcx": _VALID_TCX})
    with pytest.raises(HostileArchiveError, match="octets décompressés"):
        check_zip_safety(zip_data, max_member_uncompressed_bytes=len(_VALID_TCX) - 1)


# --- ZIP simplement invalide : pas hostile, laisse a archive_download ----------------


def test_check_zip_safety_is_silent_on_malformed_zip_and_lets_archive_download_reject_it(
    tmp_path: Path,
) -> None:
    malformed = b"PK\x03\x04not a real zip stream"

    check_zip_safety(malformed)  # ne leve pas HostileArchiveError

    with pytest.raises(ArchiveError, match="invalide"):
        ingest_upload(malformed, tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- Insertion raw.fit_file (conn optionnelle, garmin_activity_id=None) -------------


def test_ingest_upload_without_conn_leaves_fit_file_id_none(tmp_path: Path) -> None:
    """``conn`` reste optionnelle : sans elle, cette fonction ne fait QUE valider."""
    outcome = ingest_upload(_VALID_TCX, tmp_path)

    assert outcome.fit_file_id is None


def test_ingest_upload_with_conn_inserts_raw_fit_file_row_as_manual_upload(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    outcome = ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    assert outcome.fit_file_id is not None
    sha = hashlib.sha256(_VALID_TCX).digest()
    row = raw_store_conn.rows_by_sha[sha]
    # La convention posee par la migration 012 : NULL est le SEUL marqueur de
    # provenance d'un envoi manuel, jamais un garmin_activity_id sentinelle.
    assert row.garmin_activity_id is None
    assert row.fit_file_id == outcome.fit_file_id
    assert row.storage_path == str(outcome.archived.storage_path)
    assert row.size_bytes == outcome.archived.size_bytes
    assert raw_store_conn.commits >= 1


def test_ingest_upload_inserts_row_even_when_local_parse_validation_fails(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    """Un envoi archive-mais-non-reparsable-localement doit quand meme entrer en file --
    c'est garminator.transform qui tentera le parsing reel, pas ce point d'entree."""
    outcome = ingest_upload(_FAKE_FIT, tmp_path, conn=raw_store_conn)

    assert outcome.parse_error is not None
    assert outcome.fit_file_id is not None


def test_ingest_upload_writes_file_on_volume_before_inserting_raw_fit_file_row(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'invariant central du module raw.fit_file : ecrit-fsync-avant-la-ligne.

    Une implementation qui inverserait l'ordre laisserait passer tous les autres tests
    de ce module (ils ne verifient que l'etat final) -- seule une inspection AU MOMENT
    de l'insertion peut distinguer les deux ordres, d'ou l'espionnage ci-dessous.
    """
    import garminator.api.ingest as ingest_module

    original_insert = ingest_module.insert_fit_file
    contenu_du_volume_a_l_insertion: list[list[Path]] = []

    def _spy_insert(conn: FakeRawStoreConnection, **kwargs: object) -> int:
        contenu_du_volume_a_l_insertion.append(list(tmp_path.iterdir()))
        return original_insert(conn, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(ingest_module, "insert_fit_file", _spy_insert)

    ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    assert len(contenu_du_volume_a_l_insertion) == 1
    assert contenu_du_volume_a_l_insertion[0] != []  # le fichier existe deja sur le volume


def test_ingest_upload_raises_conflict_when_sha_belongs_to_a_different_activity(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    sha = hashlib.sha256(_VALID_TCX).digest()
    raw_store_conn.preload(sha256=sha, garmin_activity_id=555)

    with pytest.raises(FitFileShaConflictError):
        ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    # Raw-first : le fichier reste archive sur le volume malgre le conflit d'insertion,
    # aucune perte -- meme garantie que pour un echec de parsing local.
    assert list(tmp_path.iterdir()) != []


def test_ingest_upload_reuses_existing_row_for_same_manual_upload_content_replayed(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    """Deux envois manuels identiques (meme sha256, les deux garmin_activity_id=None)
    ne sont PAS un conflit : None == None est un accord de provenance."""
    first = ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)
    second = ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    assert first.fit_file_id == second.fit_file_id
    assert len(raw_store_conn.rows_by_sha) == 1
