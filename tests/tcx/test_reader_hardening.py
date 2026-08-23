"""Tests de garde du durcissement XML maison de :mod:`garminator.tcx.reader`.

Contexte (revue de fin de lot L3) : le durcissement reposait initialement sur
``ET.XMLParser().parser``, attribut qui n'existe PLUS dans le CPython 3.12
installe (l'accelerateur C ne l'expose pas). Le durcissement etait donc casse en
silence, et les tests censes le couvrir passaient pour la mauvaise raison.

Ce fichier verrouille les trois proprietes qui, ensemble, rendent cette
regression impossible a repasser inapercue :

1. le parseur maison rend EXACTEMENT le meme arbre que ``ET.fromstring``
   (notation de Clark, espaces de noms, entites de caractere) ;
2. chacun des quatre gestionnaires expat anti-XXE est REELLEMENT installe ;
3. le corpus d'attaques est rejete, canari a l'appui.

Aucune donnee reelle : tous les documents sont fabriques ici, et le canari du
test XXE est cree dans le ``tmp_path`` du test, sans rapport avec les fichiers
de l'athlete.
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
    b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2">'
    b"<Activities/></TrainingCenterDatabase>"
)


def _dump(element: ET.Element, depth: int = 0) -> list[str]:
    """Serialise un arbre en lignes comparables (tag, attributs, texte, queue)."""
    out = [
        f"{'  ' * depth}{element.tag!r} {sorted(element.attrib.items())!r} "
        f"{(element.text or '').strip()!r} {(element.tail or '').strip()!r}"
    ]
    for child in element:
        out.extend(_dump(child, depth + 1))
    return out


class _ParserProxy:
    """Parseur expat enveloppe : enregistre les attributs poses, peut en ignorer certains."""

    def __init__(self, parser: Any, *, muted: frozenset[str], assigned: set[str]) -> None:
        object.__setattr__(self, "_parser", parser)
        object.__setattr__(self, "_muted", muted)
        object.__setattr__(self, "_assigned", assigned)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_parser"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_assigned").add(name)
        if name in object.__getattribute__(self, "_muted"):
            return  # gestionnaire volontairement retire (test d'ablation)
        setattr(object.__getattribute__(self, "_parser"), name, value)


def _patch_parser(
    monkeypatch: pytest.MonkeyPatch, *, muted: frozenset[str] = frozenset()
) -> set[str]:
    """Intercepte ``expat.ParserCreate`` et retourne l'ensemble des attributs poses."""
    assigned: set[str] = set()
    real_create = expat.ParserCreate

    def _create(*args: Any, **kwargs: Any) -> Any:
        return _ParserProxy(real_create(*args, **kwargs), muted=muted, assigned=assigned)

    monkeypatch.setattr(expat, "ParserCreate", _create)
    return assigned


# --------------------------------------------------------------------------
# 1. Fidelite du pont expat -> TreeBuilder (notation de Clark)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "document"),
    [
        ("attribut prefixe", b'<r xmlns="urn:def" xmlns:p="urn:pfx"><c p:a="1" b="2">x</c></r>'),
        ("ns par defaut redefini", b'<r xmlns="urn:A"><c>a</c><s xmlns="urn:B"><c>b</c></s></r>'),
        ("meme prefixe redefini", b'<r xmlns:p="urn:1"><p:c/><s xmlns:p="urn:2"><p:c/></s></r>'),
        ("ns par defaut annule", b'<r xmlns="urn:A"><c xmlns=""><d/></c></r>'),
        ("attribut sans ns sous ns defaut", b'<r xmlns="urn:A" id="7"><c id="8"/></r>'),
        ("entites de caractere", b"<r>a&#233;b &amp; &lt;c&gt; &#x41;</r>"),
        ("cdata", b"<r> <a><![CDATA[ <raw> & stuff ]]></a>  </r>"),
        ("commentaires et PI", b"<!-- c --><?pi d?><r><!--in--><a/><?pi2?></r>"),
        ("prefixe xml reserve", b'<r xml:lang="fr" xml:space="preserve"><a/></r>'),
        ("sans namespace", b"<r><a x='1'/><b/>tail</r>"),
    ],
)
def test_secure_parser_matches_elementtree_clark_notation(label: str, document: bytes) -> None:
    """Le pont ``_fixname`` doit rendre le meme arbre que ``ET.XMLParser``.

    C'est le test qui echoue immediatement si le pont derive : un tag rendu hors
    notation de Clark (``{uri}tag``) casserait silencieusement tous les
    ``findall`` a namespace du lecteur, sans qu'aucun autre test ne le voie.
    """
    assert _dump(_parse_xml_secure(document)) == _dump(ET.fromstring(document)), label  # noqa: S314


def test_secure_parser_matches_elementtree_on_multi_chunk_text() -> None:
    """Texte long : expat livre le contenu en plusieurs appels a ``CharacterDataHandler``.

    Sans ``buffer_text``, les morceaux doivent quand meme se recoller a l'identique --
    cas volontairement sorti du parametrage pour ne pas produire un identifiant de
    test de 200 ko.
    """
    document = b"<r>" + b"x" * 200_000 + b"&amp;" + b"y" * 10 + b"</r>"
    assert _parse_xml_secure(document).text == ET.fromstring(document).text  # noqa: S314


def test_secure_parser_preserves_latin1_declared_encoding() -> None:
    """L'encodage declare dans le prologue doit etre respecte, comme le fait ``ET``."""
    document = "<?xml version='1.0' encoding='iso-8859-1'?><r>caf\xe9</r>".encode("iso-8859-1")
    assert _parse_xml_secure(document).text == "caf\xe9"


# --------------------------------------------------------------------------
# 2. Les quatre gestionnaires sont reellement poses
# --------------------------------------------------------------------------


def test_secure_parser_installs_all_four_hardening_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirer l'une des quatre lignes de ``_parse_xml_secure`` fait echouer ce test.

    C'est la garde structurelle : elle ne depend d'aucun comportement d'expat, donc
    elle survit a un changement de version de CPython -- contrairement au
    durcissement precedent, qui reposait sur un attribut prive disparu.
    """
    assigned = _patch_parser(monkeypatch)
    _parse_xml_secure(_WELL_FORMED)
    assert (
        assigned >= _HARDENING_HANDLERS
    ), f"gestionnaires manquants : {_HARDENING_HANDLERS - assigned}"


def test_doctype_handler_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ablation : sans ``StartDoctypeDeclHandler``, un DOCTYPE nu passerait.

    Prouve que le rejet vient bien de ce gestionnaire, et non d'un comportement
    natif d'expat qui rendrait le durcissement decoratif.
    """
    bare = b'<!DOCTYPE r SYSTEM "http://127.0.0.1:1/evil.dtd"><r/>'
    with pytest.raises(TcxParseError):
        _parse_xml_secure(bare)

    _patch_parser(monkeypatch, muted=frozenset({"StartDoctypeDeclHandler"}))
    _parse_xml_secure(bare)  # ne leve plus : le gestionnaire portait bien le rejet


def test_entity_decl_handler_is_the_second_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense en profondeur : meme sans le gestionnaire de DOCTYPE, une declaration
    d'entite reste refusee par ``EntityDeclHandler``. Les deux couches sont distinctes."""
    bomb = b'<!DOCTYPE r [<!ENTITY x "boom">]><r>&x;</r>'
    _patch_parser(monkeypatch, muted=frozenset({"StartDoctypeDeclHandler"}))
    with pytest.raises(TcxParseError):
        _parse_xml_secure(bomb)


def test_no_hardening_at_all_would_expand_the_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Temoin negatif : sans AUCUN gestionnaire, la bombe d'entites s'expanse bel et bien.

    Sans ce temoin, les tests de rejet ci-dessus pourraient passer sur un document
    qu'expat refuserait de toute facon -- exactement le piege de la premiere version.
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
# 3. Corpus d'attaques
# --------------------------------------------------------------------------


def test_decode_rejects_external_entity_pointing_at_a_local_file(tmp_path: Path) -> None:
    """XXE canonique : lecture d'un fichier local via une entite externe ``SYSTEM``.

    On verifie le rejet ET l'absence du contenu du canari dans le message
    d'exception (pas de fuite par la voie des logs).
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
    """Variante par entite parametre (``%p;``), qui contourne les durcissements naifs."""
    canary = tmp_path / "canary.dtd"
    canary.write_text('<!ENTITY x "leaked">', encoding="utf-8")
    malicious = (
        f'<!DOCTYPE r [<!ENTITY % p SYSTEM "{canary.as_uri()}">%p;]><TrainingCenterDatabase/>'
    ).encode()

    with pytest.raises(TcxParseError):
        decode_tcx(malicious, activity_id=1)


def test_decode_rejects_external_dtd_reference_without_internal_subset() -> None:
    """DOCTYPE pointant une DTD externe, sans sous-ensemble interne : SSRF potentielle."""
    with pytest.raises(TcxParseError):
        decode_tcx(
            b'<!DOCTYPE TrainingCenterDatabase SYSTEM "http://127.0.0.1:1/evil.dtd">'
            b"<TrainingCenterDatabase/>",
            activity_id=1,
        )


def test_decode_rejects_unparsed_entity_declaration() -> None:
    """Entite non analysee (``NDATA``) : declaration d'entite externe sous une autre forme."""
    with pytest.raises(TcxParseError):
        decode_tcx(
            b'<!DOCTYPE r [<!NOTATION n SYSTEM "n"><!ENTITY e SYSTEM "f.txt" NDATA n>]>'
            b'<TrainingCenterDatabase a="e"/>',
            activity_id=1,
        )


def test_decode_rejects_doctype_hidden_after_comment_and_processing_instruction() -> None:
    """Le DOCTYPE reste refuse meme precede d'un commentaire et d'une instruction."""
    with pytest.raises(TcxParseError):
        decode_tcx(
            b'<!-- innocent --><?xml-stylesheet href="x"?>'
            b'<!DOCTYPE r [<!ENTITY x "boom">]>'
            b"<TrainingCenterDatabase>&x;</TrainingCenterDatabase>",
            activity_id=1,
        )


def test_decode_rejects_undeclared_entity_reference() -> None:
    """Une entite non declaree ne passe pas en silence : erreur typee, pas de trace brute."""
    with pytest.raises(TcxParseError, match="malforme"):
        decode_tcx(b"<TrainingCenterDatabase>&nope;</TrainingCenterDatabase>", activity_id=1)
