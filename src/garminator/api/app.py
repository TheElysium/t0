"""Fabrique de l'application FastAPI du lot L7.

Fonction plutôt qu'instance de module au niveau global, même convention que
:func:`garminator.settings.get_settings` : chaque appelant (dont les tests) contrôle
explicitement le moment de construction, sans import-time side effect qui lirait déjà
l'environnement au chargement du module.
"""

from __future__ import annotations

from fastapi import FastAPI

from garminator.api.upload import router as upload_router


def create_app() -> FastAPI:
    """Construit l'application FastAPI, montée uniquement du routeur d'envoi manuel.

    Accès prévu LOCAL uniquement (pas d'Ingress, pas de TLS, pas d'authentification) —
    voir le rapport de fin de lot pour ce qu'il faudrait ajouter avant toute exposition.
    """
    app = FastAPI(
        title="garminator — envoi manuel",
        description=(
            "Endpoint local d'envoi manuel d'un fichier FIT, TCX, ou archive ZIP "
            "Garmin « originale »."
        ),
    )
    app.include_router(upload_router)
    return app
