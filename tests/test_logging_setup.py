"""Tests de :mod:`garminator.logging_setup`.

Ces tests reconfigurent le journal racine, ce qui est global au processus. Chacun
restaure donc l'état d'origine dans un ``finally`` : sans cela, un test qui laisse le
niveau à ``ERROR`` rendrait muet le ``caplog`` d'un test ultérieur, et le coupable serait
introuvable puisque la panne apparaîtrait ailleurs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from garminator.logging_setup import LOG_LEVEL_ENV_VAR, configure_logging, resolve_log_level

if TYPE_CHECKING:
    import pytest


def _snapshot() -> tuple[int, list[logging.Handler]]:
    root = logging.getLogger()
    return root.level, list(root.handlers)


def _restore(state: tuple[int, list[logging.Handler]]) -> None:
    root = logging.getLogger()
    root.setLevel(state[0])
    root.handlers[:] = state[1]


def test_niveau_par_defaut_laisse_passer_info() -> None:
    """Le point du correctif : ``logger.info`` doit désormais produire quelque chose.

    Sans configuration, le journal racine est à ``WARNING`` et les 23 appels du code
    disparaissaient sans trace.
    """
    state = _snapshot()
    try:
        configure_logging(None)
        assert logging.getLogger("garminator.garmin.retry").isEnabledFor(logging.INFO)
    finally:
        _restore(state)


def test_le_format_porte_horodatage_niveau_et_module(capsys: pytest.CaptureFixture[str]) -> None:
    """Sans ces trois champs, une ligne de journal ne se corrèle à rien après coup."""
    state = _snapshot()
    try:
        configure_logging("INFO")
        logging.getLogger("garminator.garmin.retry").info("temporisation %ss", 30)
        sortie = capsys.readouterr()
        # stdout et non stderr : les CronJobs y écrivent déjà leur rapport, et
        # Kubernetes ne garantit pas l'ordre relatif de deux flux distincts.
        assert sortie.err == ""
        assert "INFO" in sortie.out
        assert "garminator.garmin.retry" in sortie.out
        assert "temporisation 30s" in sortie.out
        assert sortie.out.startswith("20")  # horodatage ISO en tête de ligne
    finally:
        _restore(state)


def test_valeur_inconnue_ne_fait_pas_echouer_le_processus() -> None:
    """Une faute de frappe dans un réglage de confort ne doit pas coûter le sondage du jour.

    Un ``ValueError`` ici ferait échouer le CronJob avant même la première requête, et
    l'athlète perdrait ses données de la journée pour un réglage sans rapport.
    """
    state = _snapshot()
    try:
        configure_logging("VERBEUX")
        assert logging.getLogger().level == logging.INFO
    finally:
        _restore(state)


def test_appels_repetes_ne_dupliquent_pas_les_lignes(capsys: pytest.CaptureFixture[str]) -> None:
    """``force=True`` remplace le gestionnaire au lieu d'en empiler un second."""
    state = _snapshot()
    try:
        configure_logging("INFO")
        configure_logging("INFO")
        logging.getLogger("garminator.test").info("une seule fois")
        assert capsys.readouterr().out.count("une seule fois") == 1
    finally:
        _restore(state)


def test_variable_d_environnement_est_bien_celle_du_chart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le nom lu ici est celui que la ConfigMap écrit -- l'accord que le chart suppose."""
    state = _snapshot()
    try:
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "WARNING")
        configure_logging()
        assert logging.getLogger().level == logging.WARNING
    finally:
        _restore(state)


def test_resolve_accepte_la_casse_et_les_espaces() -> None:
    """Une valeur YAML recopiée à la main arrive souvent avec une casse ou un espace."""
    assert resolve_log_level(" debug ") == logging.DEBUG
    assert resolve_log_level("Error") == logging.ERROR
    assert resolve_log_level("") == logging.INFO
    assert resolve_log_level(None) == logging.INFO
