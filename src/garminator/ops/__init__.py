"""Exploitation en production du pipeline garminator (lot L10).

Ce paquet regroupe trois besoins d'exploitation distincts, chacun dans son propre module
pour rester testable indépendamment :

- :mod:`garminator.ops.refresh` -- rafraîchissement des vues matérialisées ``mart.*``,
  dans l'ordre de dépendance imposé par les migrations 007/008.
- :mod:`garminator.ops.backfill` -- suivi et pilotage du rattrapage historique, posé
  SUR l'ordonnanceur du lot L6 (:mod:`garminator.scheduler`) sans en dupliquer la
  logique de planification.
- :mod:`garminator.ops.health` -- questions d'exploitation réelles (dernière ingestion
  réussie, fenêtres en échec ou bloquées, fraîcheur des vues, trous de calendrier),
  répondues depuis l'état réellement présent en base.

Comme :mod:`garminator.db.runner`, ces modules sont volontairement autonomes vis-à-vis
de :mod:`garminator.settings` : ils lisent leur DSN indépendamment (variable
d'environnement dédiée), sans exiger les identifiants Garmin.
"""

from __future__ import annotations
