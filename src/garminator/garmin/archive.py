"""Lecture du nom de fichier réel dans l'archive ZIP du téléchargement « original ».

Confirmé par l'addendum athlète (§2) : le téléchargement « original » d'une activité
Garmin est **toujours** une archive ZIP, dont le contenu peut être ``.fit`` **ou**
``.tcx`` selon que la séance a été enregistrée par une montre ou importée manuellement
(home-trainer). Ce module ne présume jamais du format d'après l'URL ou l'extension
attendue : il lit le nom réel depuis le répertoire central du ZIP.

Volontairement, ce module ne décode **pas** le contenu du fichier interne — seulement
ses métadonnées ZIP (nom, présence). Le décodage FIT/TCX est le rôle de L3 ; ce lot
s'arrête à livrer les octets bruts et le nom de fichier réel, comme demandé.
"""

from __future__ import annotations

import io
import zipfile


class ArchiveError(RuntimeError):
    """L'archive téléchargée n'a pas la forme attendue (vide, multiple, ou pas un ZIP)."""


def extract_original_filename(zip_bytes: bytes) -> str:
    """Nom du fichier unique contenu dans l'archive ZIP « original » d'une activité.

    Raises:
        ArchiveError: si les octets ne forment pas un ZIP valide, si l'archive est vide,
            ou si elle contient plus d'un fichier (cas non prévu par l'addendum athlète,
            qui documente une archive à un seul fichier ``.fit`` ou ``.tcx``).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
    except zipfile.BadZipFile as exc:
        raise ArchiveError("Le contenu téléchargé n'est pas une archive ZIP valide.") from exc

    if not names:
        raise ArchiveError("Archive ZIP vide : aucun fichier à l'intérieur.")
    if len(names) > 1:
        raise ArchiveError(f"Archive ZIP contient plusieurs fichiers, cas non prévu : {names!r}")
    return names[0]
