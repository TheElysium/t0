"""Chaînage ``décodé (L3) + seuils (L5) -> lignes core.*``, puis écriture en base.

Le paquet se lit en deux moitiés, et cette séparation est la seule chose à retenir
avant d'y toucher :

- **Le noyau de calcul est PUR.** ``activity`` et ``rows`` ne touchent ni la base ni le
  réseau : ils consomment ce que ``garminator.fit.decode_fit`` /
  ``garminator.tcx.decode_tcx`` renvoient (``ParsedActivitySource``, cf.
  ``garminator.fit.model``) et ce que ``garminator.load`` sait calculer (TSS, TRIMP,
  hrTSS, seuils), pour produire des dataclasses immuables. Aucun SQL, aucune connexion,
  donc testables sans PostgreSQL — ce qui compte ici : il n'y a pas d'instance
  disponible dans l'environnement de développement, et cette pureté est ce qui permet
  de couvrir la totalité du calcul de charge malgré cela.
- **La frontière SQL est isolée dans trois modules.** ``summary`` (lecture des seuils et
  construction de l'``ActivityContext`` depuis le résumé Garmin), ``store`` (écriture
  idempotente d'une activité et de ses séries), ``runner`` (consommation de la file
  ``raw.fit_file``). Tous trois passent par un ``Protocol`` structurel
  ``DBConnection`` — jamais par un type psycopg concret — de sorte qu'une doublure en
  mémoire suffise à les tester pendant que mypy vérifie l'usage réel.

Cette organisation remplace celle du lot d'origine, où l'écrivain SQL était annoncé
comme « un lot ultérieur » : il est ici, mais la pureté du noyau n'a pas été sacrifiée
pour autant.
"""

from __future__ import annotations
