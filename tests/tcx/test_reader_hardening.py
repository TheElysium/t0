"""Regression tests for custom XML hardening in :mod:`garminator.tcx.reader`.

Context (L3 batch end review): hardening initially relied on ``ET.XMLParser().parser``,
attribute that NO LONGER EXISTS in installed CPython 3.12 (C accelerator doesn't expose
it). Hardening was thus silently broken, tests meant to cover it passed for the wrong
reason.

This file locks three properties that together make this regression impossible to slip
through unnoticed:

1. Custom parser renders EXACTLY same tree as ``ET.fromstring`` (Clark notation,
   namespaces, character entities);
2. each of four expat anti-XXE handlers REALLY installed;
3. attack corpus rejected, canary included.

No real data: all documents fabricated here, XXE test canary created in test's
``tmp_path``, unrelated to athlete files.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any
from xml.parsers import expat

import pytest

from garminator.fit.errors import TcxParseError
from garminator.tcx.reader import _parse_xml_secure, decode_tcx

if TYPE_CHECKING:
    from pathlib import Path

_HARDENING_HANDLERS = frozenset(
    {
        "StartDoctypeDeclHandler",
        "EntityDeclHandler",
        "UnparsedEntityDeclHandler",
        "ExternalEntityRefHandler",
    }
)

_WELL_FORMED = (
    b'<?xml version="1.0"?>'
    b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
    b"<Activities/></TrainingCenterDatabase>"
)


def _dump(element: ET.Element, depth: int = 0) -> list[str]:
    """Serialize a tree into comparable lines (tag, attributes, text, tail)."""
    out = [
        f"{'  ' * depth}{element.tag!r} {sorted(element.attrib.items())!r} "
        f"{(element.text or '').strip()!r} {(element.tail or '').strip()!r}"
    ]
    for child in element:
        out.extend(_dump(child, depth + 1))
    return out


class _ParserProxy:
    """Expat parser wrapper: records set attributes, can ignore some."""

    def __init__(self, parser: Any, *, muted: frozenset[str], assigned: set[str]) -> None:
        object.__setattr__(self, "_parser", parser)
        object.__setattr__(self, "_muted", muted)
        object.__setattr__(self, "_assigned", assigned)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_parser"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_assigned").add(name)
        if name in object.__getattribute__(self, "_muted"):
            return  # handler deliberately removed (ablation test)
        setattr(object.__getattribute__(self, "_parser"), name, value)


def _patch_parser(
    monkeypatch: pytest.MonkeyPatch, *, muted: frozenset[str] = frozenset()
) -> set[str]:
    """Intercept ``expat.ParserCreate`` and return set of set attributes."""
    assigned: set[str] = set()
    real_create = expat.ParserCreate

    def _create(*args: Any, **kwargs: Any) -> Any:
        return _ParserProxy(real_create(*args, **kwargs), muted=muted, assigned=assigned)

    monkeypatch.setattr(expat, "ParserCreate", _create)
    return assigned


# --------------------------------------------------------------------------
# 1. Fidelity of expat -> TreeBuilder bridge (Clark notation)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "document"),
    [
        ("prefixed attribute", b'<r xmlns="urn:def" xmlns:p="urn:pfx"><c p:a="1" b="2">x</c></r>'),
        ("default ns redefined", b'<r xmlns="urn:A"><c>a</c><s xmlns="urn:B"><c>b</c></s></r>'),
        ("same prefix redefined", b'<r xmlns:p="urn:1"><p:c/><s xmlns:p="urn:2"><p:c/></s></r>'),
        ("default ns cancelled", b'<r xmlns="urn:A"><c xmlns=""><d/></c></r>'),
        ("unprefixed attribute under a default ns", b'<r xmlns="urn:A" id="7"><c id="8"/></r>'),
        ("character entities", b"<r>a&#233;b &amp; &lt;c&gt; &#x41;</r>"),
        ("cdata", b"<r> <a><![CDATA[ <raw> & stuff ]]></a>  </r>"),
        ("comments and PI", b"<!-- c --><?pi d?><r><!--in--><a/><?pi2?></r>"),
        ("reserved xml prefix", b'<r xml:lang="fr" xml:space="preserve"><a/></r>'),
        ("no namespace", b"<r><a x='1'/><b/>tail</r>"),
    ],
)
def test_secure_parser_matches_elementtree_clark_notation(label: str, document: bytes) -> None:
    """Bridge ``_fixname`` must produce same tree as ``ET.XMLParser``.

    Fails immediately if bridge drifts: tag rendered outside Clark notation
    (``{uri}tag``) silently breaks all reader's namespace ``findall``, no other
    test sees it.
    """
    assert _dump(_parse_xml_secure(document)) == _dump(ET.fromstring(document)), label  # noqa: S314


def test_secure_parser_matches_elementtree_on_multi_chunk_text() -> None:
    """Long text: expat delivers content in multiple ``CharacterDataHandler`` calls.

    Without ``buffer_text``, chunks still recombine identically—case deliberately
    removed from parametrization to avoid 200 KB test identifier.
    """
    document = b"<r>" + b"x" * 200_000 + b"&amp;" + b"y" * 10 + b"</r>"
    assert _parse_xml_secure(document).text == ET.fromstring(document).text  # noqa: S314


def test_secure_parser_preserves_latin1_declared_encoding() -> None:
    """Encoding declared in prologue must be respected, as ``ET`` does."""
    document = "<?xml version='1.0' encoding='iso-8859-1'?><r>caf\xe9</r>".encode("iso-8859-1")
    assert _parse_xml_secure(document).text == "caf\xe9"


# --------------------------------------------------------------------------
# 2. All four handlers really installed
# --------------------------------------------------------------------------


def test_secure_parser_installs_all_four_hardening_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing any of four lines in ``_parse_xml_secure`` fails this test.

    This is the structural guard: it depends on no expat behaviour, so it survives a
    CPython version change -- unlike the previous hardening, which relied on a private
    attribute that has since disappeared.
    """
    assigned = _patch_parser(monkeypatch)
    _parse_xml_secure(_WELL_FORMED)
    assert assigned >= _HARDENING_HANDLERS, f"missing handlers: {_HARDENING_HANDLERS - assigned}"


def test_doctype_handler_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ablation: without ``StartDoctypeDeclHandler``, bare DOCTYPE passes.

    Proves rejection comes from this handler, not native expat behavior that
    makes hardening decorative.
    """
    bare = b'<!DOCTYPE r SYSTEM "http://127.0.0.1:1/evil.dtd"><r/>'
    with pytest.raises(TcxParseError):
        _parse_xml_secure(bare)

    _patch_parser(monkeypatch, muted=frozenset({"StartDoctypeDeclHandler"}))
    _parse_xml_secure(bare)  # no longer raises: handler did carry rejection


def test_entity_decl_handler_is_the_second_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense in depth: even without DOCTYPE handler, entity declaration stays
    rejected by ``EntityDeclHandler``. Two layers are distinct."""
    bomb = b'<!DOCTYPE r [<!ENTITY x "boom">]><r>&x;</r>'
    _patch_parser(monkeypatch, muted=frozenset({"StartDoctypeDeclHandler"}))
    with pytest.raises(TcxParseError):
        _parse_xml_secure(bomb)


def test_no_hardening_at_all_would_expand_the_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: without ANY handler, entity bomb expands fine.

    Without this control, rejection tests above could pass on a document expat
    would refuse anyway—exactly the trap in first version.
    """
    bomb = (
        b"<!DOCTYPE b ["
        b'<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        b"]><r>&c;</r>"
    )
    _patch_parser(monkeypatch, muted=_HARDENING_HANDLERS)
    root = _parse_xml_secure(bomb)
    assert len("".join(root.itertext())) == 1_000


# --------------------------------------------------------------------------
# 3. Attack corpus
# --------------------------------------------------------------------------


def test_decode_rejects_external_entity_pointing_at_a_local_file(tmp_path: Path) -> None:
    """Canonical XXE: read local file via external ``SYSTEM`` entity.

    Verify rejection AND absence of canary content in exception message
    (no leak through logs).
    """
    canary = tmp_path / "canary.txt"
    canary.write_text("SECRET-CANARY-12345", encoding="utf-8")
    malicious = (
        f'<!DOCTYPE r [<!ENTITY xxe SYSTEM "{canary.as_uri()}">]>'
        f"<TrainingCenterDatabase>&xxe;</TrainingCenterDatabase>"
    ).encode()

    with pytest.raises(TcxParseError) as excinfo:
        decode_tcx(malicious, activity_id=1)
    assert "SECRET-CANARY" not in str(excinfo.value)


def test_decode_rejects_external_parameter_entity(tmp_path: Path) -> None:
    """Variant via parameter entity (``%p;``), bypasses naive hardening."""
    canary = tmp_path / "canary.dtd"
    canary.write_text('<!ENTITY x "leaked">', encoding="utf-8")
    malicious = (
        f'<!DOCTYPE r [<!ENTITY % p SYSTEM "{canary.as_uri()}">%p;]><TrainingCenterDatabase/>'
    ).encode()

    with pytest.raises(TcxParseError):
        decode_tcx(malicious, activity_id=1)


def test_decode_rejects_external_dtd_reference_without_internal_subset() -> None:
    """DOCTYPE pointing external DTD, no internal subset: potential SSRF."""
    with pytest.raises(TcxParseError):
        decode_tcx(
            b'<!DOCTYPE TrainingCenterDatabase SYSTEM "http://127.0.0.1:1/evil.dtd">'
            b"<TrainingCenterDatabase/>",
            activity_id=1,
        )


def test_decode_rejects_unparsed_entity_declaration() -> None:
    """Unparsed entity (``NDATA``): external entity declaration in another form."""
    with pytest.raises(TcxParseError):
        decode_tcx(
            b'<!DOCTYPE r [<!NOTATION n SYSTEM "n"><!ENTITY e SYSTEM "f.txt" NDATA n>]>'
            b'<TrainingCenterDatabase a="e"/>',
            activity_id=1,
        )


def test_decode_rejects_doctype_hidden_after_comment_and_processing_instruction() -> None:
    """DOCTYPE still rejected even preceded by comment and instruction."""
    with pytest.raises(TcxParseError):
        decode_tcx(
            b'<!-- innocent --><?xml-stylesheet href="x"?>'
            b'<!DOCTYPE r [<!ENTITY x "boom">]>'
            b"<TrainingCenterDatabase>&x;</TrainingCenterDatabase>",
            activity_id=1,
        )


def test_decode_rejects_undeclared_entity_reference() -> None:
    """Undeclared entity does not pass silently: typed error, not raw trace."""
    with pytest.raises(TcxParseError, match="Malformed"):
        decode_tcx(b"<TrainingCenterDatabase>&nope;</TrainingCenterDatabase>", activity_id=1)
