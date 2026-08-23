"""Tests de garminator.tcx.reader : decodage XML durci, robustesse.

Fixtures utilisees ici sont soit construites en ligne (chaines XML minimales et
entierement fictives), soit lues depuis tests/fixtures/tcx/synthetic_bike.tcx --
un fichier synthetique fabrique pour ce lot, jamais derive de bike.tcx (l'original
de l'athlete, exclu du depot).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garminator.fit.errors import TcxParseError
from garminator.tcx.reader import decode_tcx, decode_tcx_file

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tcx" / "synthetic_bike.tcx"

_MINIMAL_TCX = b"""<?xml version="1.0"?>
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


def test_decode_synthetic_fixture_produces_samples_with_only_cadence_and_watts() -> None:
    result = decode_tcx_file(_FIXTURE, activity_id=42)

    assert result.source_format == "tcx"
    assert result.session is None  # pas de message session en TCX (addendum SS2.2)
    assert result.laps == []
    assert result.swim_lengths == []
    assert len(result.samples) == 3
    assert result.samples[0].cadence == 80
    assert result.samples[0].power == 150
    assert result.samples[0].heart_rate is None
    assert result.samples[-1].elapsed_s == 2


def test_decode_minimal_tcx() -> None:
    result = decode_tcx(_MINIMAL_TCX, activity_id=1)
    assert len(result.samples) == 1
    assert result.samples[0].power == 100
    assert result.samples[0].cadence == 75


def test_decode_malformed_xml_raises_with_no_side_effect() -> None:
    with pytest.raises(TcxParseError, match="malforme"):
        decode_tcx(b"<TrainingCenterDatabase><Unclosed>", activity_id=1)


def test_decode_empty_bytes_raises() -> None:
    with pytest.raises(TcxParseError):
        decode_tcx(b"", activity_id=1)


def test_decode_rejects_doctype_declaration() -> None:
    malicious = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE TrainingCenterDatabase [<!ENTITY xxe "pwned">]>'
        b"<TrainingCenterDatabase>&xxe;</TrainingCenterDatabase>"
    )
    with pytest.raises(TcxParseError):
        decode_tcx(malicious, activity_id=1)


def test_decode_rejects_entity_expansion_bomb() -> None:
    # Bombe d'entites classique ("billion laughs"), tronquee a quelques niveaux :
    # doit etre refusee avant toute expansion, pas seulement "lente".
    bomb = (
        b'<?xml version="1.0"?>'
        b"<!DOCTYPE lolz [\n"
        b'<!ENTITY lol "lol">\n'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        b"]>\n"
        b"<TrainingCenterDatabase>&lol2;</TrainingCenterDatabase>"
    )
    with pytest.raises(TcxParseError):
        decode_tcx(bomb, activity_id=1)


def test_decode_tcx_without_any_trackpoint_yields_empty_samples() -> None:
    empty = (
        b'<?xml version="1.0"?>'
        b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2">'
        b"<Activities/></TrainingCenterDatabase>"
    )
    result = decode_tcx(empty, activity_id=1)
    assert result.samples == []
    assert result.start_time is None
