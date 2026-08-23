"""Miroirs des tables ``core.*`` produites par ce lot (migrations 004 et 005).

Convention reprise sans dérogation de ``garminator.fit.archive.ArchivedFile`` et de
``garminator.load.activity_load.ActivityMetricRow`` : un champ, une colonne, même nom,
pour que le passage vers un ``INSERT`` (hors périmètre de ce lot) soit une simple
projection, sans transformation cachée. ``NULL`` est une valeur SIGNIFIANTE dans ce
schéma (« non calculable », jamais confondue avec 0 — cf. le commentaire de
``core.activity_metric.value`` dans migrations/005_core_series.sql) : aucune fonction
de ce paquet ne doit remplacer un ``None`` légitime par 0.

Trois tables du DDL n'ont volontairement PAS de nouvelle dataclasse ici :

- ``core.sample`` / ``core.activity_lap`` / ``core.swim_length`` sont déjà
  représentées, nommément colonne pour colonne, par ``garminator.fit.model.SampleRow``
  / ``LapRow`` / ``SwimLengthRow`` (le docstring de ce module l'affirme explicitement :
  « Les champs collent nommément aux colonnes des tables core.sample /
  core.activity_lap / core.swim_length »). Les redéfinir ici dupliquerait exactement
  ce que le lot précédent a déjà produit dans l'intention explicite d'être réutilisé
  tel quel — le même principe que la consigne de réutiliser ``ActivityMetricRow``
  plutôt que de la recopier.
- ``core.activity_metric`` est représentée par ``garminator.load.activity_load.
  ActivityMetricRow``, réexportée ici pour la même raison (réutilisation explicitement
  demandée).

``core.device`` et ``core.sample_derived`` ONT une dataclasse ci-dessous (le DDL des
deux tables est cité comme source de vérité de ce lot) mais ``build_core_rows``
(``garminator.transform.activity``) ne les peuple pas : voir le rapport de fin de lot
pour la justification (aucun message FIT ``device_info`` n'est décodé par le lot L3
actuel ; peupler ``sample_derived`` correctement exigerait de figer une convention
d'alignement seconde par seconde qui n'est pas requise par les vues ``mart`` actuelles
et dépasse le périmètre de ce lot).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from garminator.load.activity_load import ActivityMetricRow

if TYPE_CHECKING:
    from datetime import date, datetime

    from garminator.fit.model import LapRow, SampleRow, SwimLengthRow

__all__ = [
    "ActivityHrHistogramRow",
    "ActivityMetricRow",
    "ActivityPowerCurveRow",
    "ActivityRow",
    "CoreActivityBundle",
    "DeviceRow",
    "SampleDerivedRow",
]


@dataclass(frozen=True, slots=True)
class DeviceRow:
    """Miroir de ``core.device``. Non peuplée par ce lot (cf. docstring du module)."""

    manufacturer: str | None = None
    product: str | None = None
    serial_number: str | None = None
    device_type: str | None = None
    software_version: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """Miroir de ``core.activity``. Voir migrations/004_core_activity.sql.

    ``ingested_at`` / ``updated_at`` sont absents à dessein : leurs colonnes portent
    ``DEFAULT now()`` en base, et ce lot ne connaît pas l'instant d'écriture (qui
    n'existe qu'au moment de l'``INSERT``, hors périmètre de ce lot).
    """

    activity_id: int
    athlete_id: int
    sport_code: str
    start_time: datetime
    start_time_local: datetime
    local_date: date
    tz_offset_s: int
    duration_s: int
    garmin_type_key: str | None = None
    elapsed_s: int | None = None
    moving_s: int | None = None
    distance_m: float | None = None
    ascent_m: float | None = None
    descent_m: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_power: float | None = None
    max_power: float | None = None
    avg_power_nonzero: float | None = None
    np_watts: float | None = None
    np_reliable: bool | None = None
    avg_speed_mps: float | None = None
    avg_cadence: float | None = None
    calories: int | None = None
    ngp_speed_mps: float | None = None
    gap_applied: bool = False
    swim_time_s: float | None = None
    swim_time_source: str | None = None
    total_samples: int | None = None
    imputed_seconds: int = 0
    excised_seconds: int = 0
    data_quality: str = "unknown"
    parent_activity_id: int | None = None
    is_parent: bool = False
    leg_order: int | None = None
    primary_device_id: int | None = None
    fit_file_id: int | None = None
    summary_sha256: bytes | None = None
    name: str | None = None
    needs_manual_rpe: bool = False


@dataclass(frozen=True, slots=True)
class SampleDerivedRow:
    """Miroir de ``core.sample_derived``. Non peuplée par ce lot (cf. docstring du module)."""

    activity_id: int
    t: datetime
    power_30s: float | None = None
    grade_pct: float | None = None
    grade_factor: float | None = None
    gap_speed_mps: float | None = None
    imputed: bool = False
    excised: bool = False


@dataclass(frozen=True, slots=True)
class ActivityHrHistogramRow:
    """Miroir d'une ligne de ``core.activity_hr_histogram`` (résolution 1 bpm)."""

    activity_id: int
    hr_bpm: int
    seconds: int


@dataclass(frozen=True, slots=True)
class ActivityPowerCurveRow:
    """Miroir d'une ligne de ``core.activity_power_curve``.

    ``kind`` doit être l'une des valeurs de la contrainte ``apc_kind_ck`` :
    ``bike_power`` / ``run_power`` / ``run_speed`` / ``swim_speed`` / ``heart_rate``.
    """

    activity_id: int
    kind: str
    duration_s: int
    value: float
    spans_pause: bool = False


@dataclass(frozen=True, slots=True)
class CoreActivityBundle:
    """Tout ce que produit ``build_core_rows`` pour UNE activité, prêt pour l'écriture.

    ``samples`` / ``laps`` / ``swim_lengths`` sont des ``SampleRow`` / ``LapRow`` /
    ``SwimLengthRow`` de ``garminator.fit.model`` (cf. docstring du module :
    réutilisées telles quelles, jamais redéfinies). ``sample_derived`` est toujours
    vide dans ce lot (voir docstring du module).
    """

    activity: ActivityRow
    samples: tuple[SampleRow, ...]
    laps: tuple[LapRow, ...]
    swim_lengths: tuple[SwimLengthRow, ...]
    hr_histogram: tuple[ActivityHrHistogramRow, ...]
    power_curve: tuple[ActivityPowerCurveRow, ...]
    metrics: tuple[ActivityMetricRow, ...]
    sample_derived: tuple[SampleDerivedRow, ...] = field(default_factory=tuple)
