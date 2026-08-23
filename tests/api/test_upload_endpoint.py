"""Tests HTTP de garminator.api.upload : la route d'envoi manuel, via le client de
test de FastAPI (aucun serveur reel demarre).

Requiert ``python-multipart`` (pour que FastAPI puisse meme DEFINIR la route -- voir
garminator/api/upload.py) et ``httpx`` (dependance du TestClient de starlette). Aucun
des deux n'est installe dans le venv de ce depot au moment de l'ecriture de ce lot :
tout ce module est saute proprement (``pytest.importorskip``) plutot que d'echouer --
voir le rapport de fin de lot pour la liste des dependances a ajouter.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("multipart", reason="python-multipart absent du venv (voir rapport de lot)")
httpx = pytest.importorskip("httpx", reason="httpx absent du venv (voir rapport de lot)")

from fastapi.testclient import TestClient  # noqa: E402

from garminator.api.app import create_app  # noqa: E402
from garminator.api.upload import get_upload_db_conn, get_upload_dest_dir  # noqa: E402
from tests.api.conftest import FakeRawStoreConnection  # noqa: E402

if TYPE_CHECKING:
    from pathlib import Path

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

# Meme fixture inerte que tests/api/test_ingest.py : signature FIT reconnaissable mais
# non decodable.
_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20


def _client(tmp_path: Path, conn: FakeRawStoreConnection | None = None) -> TestClient:
    app = create_app()
    # Surcharge de dependance : le repertoire d'archivage vient de tmp_path, et la
    # connexion base d'une doublure en memoire -- jamais des settings reelles (aucune
    # variable d'environnement Garmin/PostgreSQL requise pour ces tests, et aucun
    # PostgreSQL n'existe de toute facon dans cet environnement).
    app.dependency_overrides[get_upload_dest_dir] = lambda: tmp_path
    app.dependency_overrides[get_upload_db_conn] = lambda: conn or FakeRawStoreConnection()
    return TestClient(app)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_upload_valid_tcx_returns_201_with_ok_status(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/uploads", files={"file": ("activity.tcx", _VALID_TCX, "application/octet-stream")}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ok"
    assert body["source_format"] == "tcx"
    assert body["parsed"]["n_samples"] == 1
    assert body["parse_error"] is None
    assert isinstance(body["fit_file_id"], int)
    # Preuve que l'archivage a reellement eu lieu sur le volume surcharge.
    assert list(tmp_path.iterdir()) != []


def test_upload_zip_wrapped_tcx_is_sniffed_by_content_not_by_extension(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({"whatever.bin": _VALID_TCX})

    # Le nom de fichier annonce ("archive.zip") ET le nom interne ("whatever.bin") sont
    # tous deux trompeurs : seul le contenu doit determiner le format detecte.
    response = client.post("/uploads", files={"file": ("archive.zip", zip_data, "application/zip")})

    assert response.status_code == 201
    assert response.json()["source_format"] == "tcx"


def test_upload_fit_shaped_but_undecodable_returns_422_but_reports_archiving(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/uploads", files={"file": ("activity.fit", _FAKE_FIT, "application/octet-stream")}
    )

    # Contre-epreuve de "pas de succes mensonger" : un 201 ici serait faux (le parsing
    # a bien echoue) -- le statut DOIT etre 422, pas 201, malgre un archivage reussi.
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "archived_but_unparsed"
    assert detail["source_format"] == "fit"
    assert detail["parse_error"] is not None
    # Contre-epreuve de "pas de perte silencieuse" : le fichier brut est bien sur le
    # volume malgre l'echec de parsing.
    assert list(tmp_path.iterdir()) != []


def test_upload_unrecognized_content_returns_422_with_no_side_effect(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/uploads",
        files={"file": ("mystery.bin", b"ni FIT ni TCX ni ZIP", "application/octet-stream")},
    )

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_upload_empty_zip_is_rejected_explicitly(tmp_path: Path) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({})

    response = client.post("/uploads", files={"file": ("empty.zip", zip_data, "application/zip")})

    assert response.status_code == 422
    assert "vide" in response.json()["detail"]


def test_upload_multi_member_zip_is_rejected_explicitly(tmp_path: Path) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({"a.tcx": _VALID_TCX, "b.tcx": _VALID_TCX})

    response = client.post("/uploads", files={"file": ("multi.zip", zip_data, "application/zip")})

    assert response.status_code == 422
    assert "fichiers" in response.json()["detail"]


def test_upload_zip_with_path_escaping_member_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({"../evil.tcx": _VALID_TCX})

    response = client.post("/uploads", files={"file": ("evil.zip", zip_data, "application/zip")})

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_upload_oversized_payload_returns_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Envoyer reellement 200 Mio (borne par defaut) serait couteux pour ce seul test :
    # on resserre la borne lue par la route (nom global regarde a l'appel, pas capture
    # a la definition) plutot que d'agrandir le corps envoye.
    import garminator.api.upload as upload_module

    monkeypatch.setattr(upload_module, "DEFAULT_MAX_UPLOAD_BYTES", 8)
    client = _client(tmp_path)

    response = client.post(
        "/uploads", files={"file": ("activity.tcx", _VALID_TCX, "application/octet-stream")}
    )

    assert response.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_upload_returns_409_when_content_already_belongs_to_another_activity(
    tmp_path: Path,
) -> None:
    conn = FakeRawStoreConnection()
    conn.preload(sha256=hashlib.sha256(_VALID_TCX).digest(), garmin_activity_id=9001)
    client = _client(tmp_path, conn=conn)

    response = client.post(
        "/uploads", files={"file": ("activity.tcx", _VALID_TCX, "application/octet-stream")}
    )

    assert response.status_code == 409
    # Raw-first : le fichier reste archive malgre le conflit d'insertion, aucune perte.
    assert list(tmp_path.iterdir()) != []
