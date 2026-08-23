# garminator

Pipeline auto-hébergé d'ingestion et de visualisation des données Garmin Connect, pour le suivi
d'entraînement en triathlon.

## Composants

- **`garminator-ingest`** (ce dépôt) : poll de l'API Garmin Connect, parsing des fichiers `.FIT`,
  calculs immuables à l'ingestion, ordonnanceur, endpoint d'upload manuel de `.FIT`.
- **PostgreSQL** : stockage en trois schémas — `raw` (réponses API et fichiers bruts, jamais
  retouchés), `core` (données normalisées), `mart` (vues matérialisées de charge d'entraînement).
- **Grafana** : tableaux de bord (PMC, courbe puissance-durée, natation, wellness, sommeil).
- **Chart Helm** : déploiement sur un cluster Kubernetes personnel (voir la note de sécurité
  ci-dessous).

Le dépôt produit du code et des manifestes. **Aucun agent ni CI n'appelle Garmin Connect et
aucun agent ni CI ne déploie sur le cluster** : c'est l'athlète qui déploie.

## Structure du paquet

```
src/garminator/
├── db/          # migrations et accès PostgreSQL
├── garmin/      # client Garmin Connect (session persistée, backoff, rate limiting)
├── fit/         # parsing des fichiers .FIT (garmin-fit-sdk)
├── compute/     # calculs immuables (NP, GAP, courbe puissance-durée, ...) et charge (TSS, PMC)
├── scheduler/   # ordonnancement (poll quotidien, backfill, rafraîchissement des vues)
└── api/         # endpoint d'upload manuel de fichiers .FIT
```

## Ce qui tourne sur le cluster

Le chart déploie, en plus de PostgreSQL et Grafana, cinq charges périodiques :

| Objet | Valeurs | Rôle |
| --- | --- | --- |
| CronJob `scheduler` | `scheduler.*` — défaut `0 5 * * *` | **Le sondage Garmin.** Rattrape aussi l'historique, passage après passage. Archive les .FIT/.TCX des activités sur le PVC (désactivable via `scheduler.activityFiles: false`, qui passe `--no-activity-files`). |
| CronJob `ops-refresh` | `opsRefresh.*` — défaut `30 5 * * *` | Rafraîchit les six vues matérialisées `mart.*`. |
| CronJob `ops-backfill-status` | `opsBackfill.*` — défaut `0 6 * * *`, activé | Rapport lecture seule sur l'avancement du rattrapage. Ne sonde rien. |
| CronJob `ops-reconcile` | `opsReconcile.*` — défaut `0 7 * * *`, activé | Compare l'inventaire `raw.fit_file` au contenu réel du PVC. Lecture seule des deux côtés, aucune suppression. N'échoue que sur une ligne sans fichier (un fichier orphelin est bénin). |
| CronJob `postgresql-backup` | `backup.*` | `pg_dump --format=custom` sur le PVC. |
| Deployment `ingest` | `ingest.*` | Sert l'endpoint d'upload manuel de `.FIT`. Ne sonde pas Garmin. |

Détail des procédures d'exploitation : [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Développement

Prérequis : Python 3.12 (borne exacte, voir `requires-python`).

```bash
python -m venv .venv
source .venv/bin/activate   # ou .venv\Scripts\activate sous Windows
pip install -e ".[dev]"
```

Vérifications, dans l'ordre où la CI les exécute :

```bash
ruff check .
ruff format --check .
mypy src tests
pytest
```

## Configuration

Les paramètres sont lus depuis l'environnement via `garminator.settings.Settings`
(pydantic-settings). Voir `.env.example` pour la liste des variables attendues — ce fichier ne
contient et ne doit jamais contenir de valeur réelle.

Aucun champ obligatoire n'a de valeur par défaut : le service refuse de démarrer plutôt que de
retomber sur un comportement implicite non désiré.

Les valeurs sensibles (identifiants Garmin **et** DSN PostgreSQL, qui porte le mot de passe de la
base) sont des `SecretStr` : leur `repr`, leur `str` et leur sérialisation JSON sont masqués.
`get_settings()` est le seul point d'entrée supporté — il convertit l'erreur de validation de
pydantic en `SettingsError` qui ne cite que des *noms* de variables. Sans cette conversion,
pydantic recopie le dictionnaire d'entrée complet, mot de passe Garmin en clair, dans le message
d'exception, donc dans les logs du conteneur au premier démarrage raté. Deux tests verrouillent
cette propriété (`tests/test_settings.py`).

### `GARMINTOKENS` : à définir, sous peine de blocage de compte

`garminconnect` 0.3.2 lit la variable **`GARMINTOKENS`** telle quelle (`os.getenv("GARMINTOKENS")`,
sans le préfixe `GARMINATOR_`) et **n'a aucun chemin par défaut**. Si elle n'est pas définie, ou si
elle pointe ailleurs que sur un **volume persistant**, la session n'est jamais réutilisée : chaque
redémarrage du conteneur refait une authentification SSO complète auprès de Garmin. Répétée, cette
séquence est le chemin connu vers le blocage du compte.

L'image Docker la positionne à `/data/garmin-tokens` — ce chemin **doit** être monté sur un volume
persistant (PVC), sinon la persistance est illusoire. Côté code, le champ correspondant est
`Settings.garmin_tokens_path`, déclaré avec `validation_alias="GARMINTOKENS"` précisément pour
partager le nom attendu par la bibliothèque.

## Sécurité — Secret Garmin

> **Le Secret Kubernetes contenant les identifiants Garmin Connect n'est jamais committé dans ce
> dépôt.** Il se crée directement sur le cluster, par l'athlète, hors de tout manifeste versionné
> (le chart Helm ne fait que le *référencer* par son nom).
>
> Le compte Garmin utilisé n'a pas de 2FA active. Ce Secret vaut donc l'accès complet au compte —
> traitez-le avec la même prudence qu'un mot de passe root.

## Image Docker

`Dockerfile` multi-étages : construction du wheel dans une image avec outils de build, exécution
dans une image finale sans compilateur, sous un utilisateur non-root, avec `HEALTHCHECK`.

Le `.dockerignore` restreint le contexte de build : les données personnelles (`Activities.csv`,
`metrics&wellness_1year/`, `*.fit`), le `.env` et les caches n'y entrent jamais. C'est la garantie
qu'aucune donnée personnelle ne peut se retrouver dans une image publiée sur GHCR, y compris si un
`COPY` trop large est introduit plus tard.

```bash
docker build -t garminator-ingest .
```

L'image est publiée sur GHCR par le workflow `.github/workflows/publish-image.yml`, déclenché sur
push d'un tag `vX.Y.Z`.
