"""Tests of t0.tcx.reader: hardened XML decoding, robustness.

Fixtures used here are either built inline (minimal, entirely fictional XML strings)
or read from tests/fixtures/tcx/synthetic_bike.tcx—synthetic file built for this
batch, never derived from bike.tcx (athlete original, excluded from repo).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from t0.fit.errors import TcxParseError
from t0.tcx.mapping import TCD_NAMESPACE, TPX_NAMESPACE
from t0.tcx.reader import decode_tcx, decode_tcx_file


def test_tcd_namespace_matches_the_official_uri() -> None:
    """The namespace constants are compared to literals, never to a fixture.

    A wrong namespace does not raise: ElementTree simply matches nothing, so the
    decoder returns an activity with zero samples and no start time. The repository
    shipped ``TrainingCenterDatabasev2`` (missing the slash) and every TCX fixture was
    written to match it, so the whole suite agreed with the reader while both were
    wrong on real Garmin files. Hence a literal here: it is the only assertion a
    fixture cannot follow into the error.
    """
    assert TCD_NAMESPACE == "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
    assert TPX_NAMESPACE == "http://www.garmin.com/xmlschemas/ActivityExtension/v2"


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tcx" / "synthetic_bike.tcx"

_MINIMAL_TCX = b"""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
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
    assert result.session is None  # No session message in TCX (addendum SS2.2)
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
    with pytest.raises(TcxParseError, match="Malformed"):
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
    # Classic entity bomb ("billion laughs"), truncated to a few levels: must be
    # refused before any expansion, not merely "slow".
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


_TCX_WITH_DUPLICATE_TS = (
    b'<?xml version="1.0"?>'
    b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
    b'<Activities><Activity Sport="Biking"><Lap>'
    b"<Track>"
    b'<Trackpoint><Time>2026-08-01T06:00:00Z</Time><Extensions><TPX xmlns="'
    b"http://www.garmin.com/xmlschemas/ActivityExtension/v2"
    b'"><Watts>100</Watts></TPX></Extensions></Trackpoint>'
    b'<Trackpoint><Time>2026-08-01T06:00:00.400Z</Time><Extensions><TPX xmlns="'
    b"http://www.garmin.com/xmlschemas/ActivityExtension/v2"
    b'"><Watts>101</Watts></TPX></Extensions></Trackpoint>'
    b'<Trackpoint><Time>2026-08-01T06:00:01Z</Time><Extensions><TPX xmlns="'
    b"http://www.garmin.com/xmlschemas/ActivityExtension/v2"
    b'"><Watts>102</Watts></TPX></Extensions></Trackpoint>'
    b"</Track>"
    b"</Lap></Activity></Activities></TrainingCenterDatabase>"
)


def test_decode_tcx_drops_trackpoint_with_duplicate_elapsed_s() -> None:
    # Trackpoints at t=0.0s and t=0.4s both round to elapsed_s=0 (map_trackpoint
    # rounds to whole seconds; real Garmin TCX files carry sub-second precision,
    # cf. activity 24313351581). Downstream regularization treats a duplicate as
    # caller error, not a gap: the second trackpoint must be dropped, not raise.
    result = decode_tcx(_TCX_WITH_DUPLICATE_TS, activity_id=1)

    assert [s.elapsed_s for s in result.samples] == [0, 1]
    # The kept sample at elapsed_s=0 is the first one seen, not a merge.
    assert result.samples[0].power == 100


_TCX_WITH_BACKWARD_TS = (
    b'<?xml version="1.0"?>'
    b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
    b'<Activities><Activity Sport="Biking"><Lap>'
    b"<Track>"
    b"<Trackpoint><Time>2026-08-01T06:00:00Z</Time></Trackpoint>"
    b"<Trackpoint><Time>2026-08-01T06:00:02Z</Time></Trackpoint>"
    b"<Trackpoint><Time>2026-08-01T06:00:01.400Z</Time></Trackpoint>"
    b"<Trackpoint><Time>2026-08-01T06:00:03Z</Time></Trackpoint>"
    b"</Track>"
    b"</Lap></Activity></Activities></TrainingCenterDatabase>"
)


def test_decode_tcx_drops_trackpoint_with_backward_elapsed_s() -> None:
    # A trackpoint whose rounded elapsed_s goes backward relative to the previous
    # kept sample must also be dropped.
    result = decode_tcx(_TCX_WITH_BACKWARD_TS, activity_id=1)

    assert [s.elapsed_s for s in result.samples] == [0, 2, 3]


def test_decode_tcx_keeps_all_trackpoints_when_strictly_increasing() -> None:
    result = decode_tcx(_MINIMAL_TCX, activity_id=1)

    assert [s.elapsed_s for s in result.samples] == [0]


def test_decode_tcx_without_any_trackpoint_yields_empty_samples() -> None:
    empty = (
        b'<?xml version="1.0"?>'
        b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
        b"<Activities/></TrainingCenterDatabase>"
    )
    result = decode_tcx(empty, activity_id=1)
    assert result.samples == []
    assert result.start_time is None
