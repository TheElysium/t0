"""Contrat structurel du transport Garmin, pour injection.

``garminconnect.Garmin`` (le vrai client réseau) satisfait ce protocole sans en hériter
explicitement (typage structurel). Dans les tests, un faux transport qui l'implémente
suffit à exercer tout le lot L2 **sans aucun appel réseau**.

Volontairement minimal : seules ``login`` et ``download_activity`` sont typées
explicitement, parce que ce sont les deux seules méthodes dont :class:`~garminator.
garmin.client.GarminClient` a besoin de connaître la signature précise. Les ~130 autres
méthodes de endpoint (``get_sleep_data``, ``get_activities_by_date``, ...) sont invoquées
dynamiquement par :meth:`~garminator.garmin.client.GarminClient.fetch` via ``getattr`` :
leur nom est une donnée d'appel, pas une dépendance de typage — épingler les 132
signatures ici serait une duplication du fichier source de ``garminconnect`` que rien ne
garde synchronisée.
"""

from __future__ import annotations

from typing import Any, Protocol


class GarminTransport(Protocol):
    """Sous-ensemble de l'API de ``garminconnect.Garmin`` utilisé par ce client."""

    def login(self, tokenstore: str) -> tuple[str | None, str | None]:
        """Authentifie (ou restaure une session depuis le tokenstore) et persiste les jetons.

        Reproduit la signature de ``garminconnect.Garmin.login`` : retourne
        ``(None, None)`` en cas de succès complet, ou lève une des exceptions de
        ``garminconnect`` (jamais de retour silencieux en cas d'échec définitif).
        """
        ...

    def download_activity(self, activity_id: str, dl_fmt: Any = ...) -> bytes:
        """Télécharge une activité dans le format demandé et retourne les octets bruts.

        Pour ``ActivityDownloadFormat.ORIGINAL``, les octets sont ceux d'une archive ZIP
        dont le contenu (un seul fichier, ``.fit`` ou ``.tcx``) n'est jamais présumé par
        ce module — voir :mod:`garminator.garmin.archive`.
        """
        ...
