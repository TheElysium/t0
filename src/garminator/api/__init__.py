"""API HTTP de garminator : endpoint d'envoi manuel de fichiers FIT/TCX/ZIP (lot L7).

Ce paquet n'importe RIEN au niveau package (pas de réexport ici, contrairement à
``garminator.garmin``) : ``garminator.api.upload`` (et donc ``garminator.api.app``)
déclare une route utilisant ``File(...)``, ce qui fait échouer l'IMPORT du module dès
sa définition si ``python-multipart`` n'est pas installé (vérifié sur ce dépôt — voir
le rapport de fin de lot). Réexporter ces symboles ici casserait l'import de
``garminator.api.ingest`` (logique pure, sans dépendance à FastAPI/multipart) dans tout
environnement sans ``python-multipart``. Importez donc le sous-module voulu directement :
``garminator.api.ingest`` pour la logique métier, ``garminator.api.app``/``.upload``
pour la couche HTTP.
"""

from __future__ import annotations
