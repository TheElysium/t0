"""Erreurs du lot L7 (endpoint d'upload).

Séparées de :mod:`garminator.fit.errors` : celles-ci couvrent des refus qui ne
relèvent ni du décodage ni de l'archivage (taille de requête, archive hostile), mais
de la frontière HTTP elle-même. Le module ``garminator.api.upload`` les mappe vers des
codes de statut explicites — voir sa docstring pour la table de correspondance.
"""

from __future__ import annotations


class UploadError(RuntimeError):
    """Racine des erreurs de ce lot. Jamais levée directement."""


class PayloadTooLargeError(UploadError):
    """Le corps de la requête (ou un membre d'archive décompressé) dépasse la taille
    maximale acceptée. Levée avant toute écriture sur le volume : aucun octet du
    dépassement n'est conservé en mémoire au-delà du seuil."""


class HostileArchiveError(UploadError):
    """L'archive ZIP porte un motif dangereux : chemin d'échappement (``../``, chemin
    absolu) ou ratio de compression caractéristique d'une bombe de décompression.

    Levée en pré-inspection, avant toute décompression réelle d'un membre : seuls les
    métadonnées ``ZipInfo`` (nom, taille annoncée, taille compressée) sont lues.
    """
