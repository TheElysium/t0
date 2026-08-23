# RUNBOOK — Exploitation de garminator

Ce document couvre les procédures d'exploitation réellement disponibles à ce jour. Il
ne décrit que ce qui existe : aucune commande, aucun endpoint, aucune table mentionnés
ici n'est hypothétique. Là où une pièce manque encore, c'est dit explicitement, avec la
raison.

Toutes les commandes ci-dessous supposent `GARMINATOR_POSTGRES_DSN` défini dans
l'environnement (jamais `--dsn` en ligne de commande dans un contexte partagé : voir
« Pourquoi pas `--dsn` » plus bas).

## Sommaire

1. [Premier démarrage](#1-premier-démarrage)
2. [Reprendre après un échec](#2-reprendre-après-un-échec)
3. [Une fenêtre bloquée en `in_progress`](#3-une-fenêtre-bloquée-en-in_progress)
4. [Restaurer depuis une sauvegarde](#4-restaurer-depuis-une-sauvegarde)
5. [Rafraîchir les vues `mart` manuellement](#5-rafraîchir-les-vues-mart-manuellement)
6. [Garmin renvoie 401](#6-garmin-renvoie-401)
7. [Pourquoi pas `--dsn`](#7-pourquoi-pas---dsn)
8. [Le sondage quotidien (CronJob `scheduler`)](#8-le-sondage-quotidien-cronjob-scheduler)
9. [Réconcilier le PVC et `raw.fit_file`](#9-réconcilier-le-pvc-et-rawfit_file)
10. [La transformation quotidienne (CronJob `transform`)](#10-la-transformation-quotidienne-cronjob-transform)
11. [Envoyer un fichier manuellement](#11-envoyer-un-fichier-manuellement)

---

## 1. Premier démarrage

1. Appliquer les migrations :

   ```
   python -m garminator.db migrate
   ```

   Vérifier l'absence de dérive : `python -m garminator.db status` doit afficher
   « Dérives détectées : » sans aucune ligne `!` en dessous.

2. Les six vues `mart.*` sont peuplées automatiquement à leur création par les
   migrations 007/008 (`CREATE MATERIALIZED VIEW` sans `WITH NO DATA`) : aucune action
   supplémentaire n'est nécessaire à ce stade pour qu'elles existent, mais elles seront
   VIDES tant qu'aucune activité n'a été ingérée dans `core.activity` — c'est normal, pas
   une panne.

3. **Amorcer l'athlète et ses seuils.** Étape OBLIGATOIRE, et la seule qui ne soit pas
   automatisable : copier `deploy/seed/athlete.sql.example` **hors du dépôt**, le
   remplir, puis l'exécuter :

   ```
   cp deploy/seed/athlete.sql.example ~/garminator-seed.sql
   $EDITOR ~/garminator-seed.sql
   psql "$GARMINATOR_POSTGRES_DSN" -f ~/garminator-seed.sql
   ```

   Ce n'est pas une migration parce que ces valeurs sont des données personnelles
   (poids, fréquences cardiaques, seuils) : le dépôt porte la procédure, jamais la
   donnée — même raisonnement que pour les traces GPS des fichiers .FIT. Le prix à payer
   est que **cette étape est à refaire sur chaque base neuve**.

   Sans elle, `python -m garminator.transform` s'arrête en code 2 (« core.athlete
   contient 0 ligne(s) ») : il refuse de deviner l'athlète, parce qu'un mauvais athlète
   donnerait de mauvais seuils, donc des TSS faux — sans violer la moindre contrainte ni
   produire le moindre symptôme visible avant des mois.

4. Constater l'état de départ :

   ```
   python -m garminator.ops health
   ```

   Sur une base neuve, attendez-vous à : aucun endpoint dans « Dernier succès par
   endpoint » (raw.poll_window est vide), zéro fenêtre en échec, zéro fenêtre bloquée,
   et les six vues `mart` à `0` ligne (peuplées mais vides).

5. Déclencher le premier sondage. Il n'y a rien de spécial à faire : le CronJob
   `<release>-scheduler` (`scheduler-cronjob.yaml`) passe chaque jour et rattrape
   lui-même tout l'historique manquant depuis `GARMINATOR_HISTORY_START`, à raison de
   `scheduler.maxWindowsPerEndpoint` fenêtres par endpoint et par passage — voir la
   section 8. Pour ne pas attendre le prochain passage planifié :

   ```
   kubectl create job --from=cronjob/<release>-scheduler premier-sondage
   ```

   Sur un tout premier backfill, ce premier passage télécharge aussi les fichiers
   originaux (.FIT/.TCX) de chaque activité rencontrée. Pour étaler cette salve, un
   premier run purement JSON est possible via `scheduler.activityFiles: false` (drapeau
   `--no-activity-files`) : les fichiers seront récupérés aux passages suivants, une fois
   le drapeau remis à `true`.

6. Suivre l'avancement du rattrapage :

   ```
   python -m garminator.ops backfill-status
   ```

   Cette commande est un RAPPORT lecture seule sur `raw.poll_window` : elle ne déclenche
   aucun sondage. C'est le CronJob ci-dessus qui progresse, passage après passage.

## 2. Reprendre après un échec

Le sondage (`garminator.scheduler.service.run_scheduled_poll`, utilisé par le service
d'ingestion) est conçu pour être rejouable sans risque : une fenêtre déjà `done` n'est
retéléchargée que si elle retombe dans la fenêtre glissante de rafraîchissement récente
(`refresh_lookback_days`, 3 jours par défaut). Reprendre après un échec (crash du pod,
coupure réseau, redémarrage du cluster) ne demande donc **aucune action manuelle** :
relancer le service d'ingestion suffit, il reprendra exactement où il s'était arrêté.

Pour vérifier ce qu'il reste à faire avant de relancer :

```
python -m garminator.ops backfill-status --endpoint <endpoint> [--endpoint <endpoint> ...]
```

Remplacer `<endpoint>` par les noms d'endpoints réellement sondés par le service
d'ingestion (ce runbook ne les invente pas : consulter la configuration du service
d'ingestion pour la liste en vigueur).

## 3. Une fenêtre bloquée en `in_progress`

Une fenêtre reste `in_progress` quand le processus a été interrompu (kill, OOM, panne
du nœud) entre le moment où elle a été marquée `in_progress` et celui où elle aurait dû
être marquée `done` ou `failed`.

**Aucune intervention manuelle en base n'est nécessaire ni recommandée.** Le calcul des
fenêtres dues (`garminator.scheduler.planning.due_windows`) considère automatiquement
comme due toute fenêtre `in_progress` dont `last_attempted_at` dépasse
`DEFAULT_STALE_IN_PROGRESS_AFTER` (2 heures) : au prochain cycle de sondage, elle sera
retentée normalement, sans duplication (la fenêtre est simplement retraitée comme si
elle était `pending`).

Pour l'observer :

```
python -m garminator.ops health
```

section « Fenêtres bloquées in_progress ». Si une fenêtre y reste listée sur PLUSIEURS
exécutions consécutives séparées de plus de 2 heures, ce n'est plus un cas normal
d'attente : cela signifie que le service de sondage lui-même ne tourne plus (sinon il
l'aurait reprise). Diagnostiquer d'abord l'état du pod d'ingestion (`kubectl get pods`,
`kubectl logs`) avant toute manipulation de `raw.poll_window`.

Ne JAMAIS `UPDATE raw.poll_window SET status = 'failed'` à la main pour « débloquer »
une fenêtre : c'est inutile (le mécanisme ci-dessus s'en charge) et risque de masquer un
job de sondage réellement arrêté (le symptôme visible disparaîtrait sans que la cause
soit traitée).

## 4. Restaurer depuis une sauvegarde

Les sauvegardes sont produites par le CronJob existant
(`deploy/helm/garminator/templates/backup-cronjob.yaml`), au format `pg_dump --format=custom`,
dans `fitArchive.subPaths.postgresBackups` du PVC partagé, nommées
`garminator-<horodatage-UTC>.dump`.

1. Identifier le dump à restaurer (le plus récent, sauf besoin explicite d'un point
   dans le passé) : lister le contenu du sous-chemin de sauvegarde sur le PVC.
2. Restaurer avec `pg_restore` contre une base PostgreSQL joignable :

   ```
   pg_restore --clean --if-exists --no-owner --dbname="$GARMINATOR_POSTGRES_DSN" garminator-<horodatage>.dump
   ```

   `--clean --if-exists` supprime les objets existants avant de les recréer : la
   restauration remplace intégralement le contenu de la base cible — à n'exécuter que
   contre une base explicitement destinée à recevoir cette restauration, jamais contre
   une base de production sans confirmation préalable.

3. Après restauration, vérifier l'absence de dérive de schéma et l'état des vues :

   ```
   python -m garminator.db status
   python -m garminator.ops health
   ```

   Si les vues `mart` apparaissent « JAMAIS PEUPLÉE » après restauration (cas possible
   si le dump a été produit par un outil qui exclut les données des vues matérialisées),
   voir la section suivante : `python -m garminator.ops refresh` bascule automatiquement
   sur un rafraîchissement non concurrent pour ce cas précis.

## 5. Rafraîchir les vues `mart` manuellement

```
python -m garminator.ops refresh
```

Rafraîchit les six vues `mart.*` dans l'ordre imposé par leurs dépendances réelles
(`mart.daily_load` avant `mart.pmc`, qui la lit — les quatre autres vues n'ont pas
d'ordre imposé entre elles). Chaque vue est rafraîchie indépendamment : l'échec d'une
vue n'empêche pas les suivantes d'être tentées, et le code de sortie de la commande
(`1`) signale un échec partiel sans qu'il faille relire les logs pour le savoir.

Ce rafraîchissement manuel est un COMPLÉMENT au `CronJob` de rafraîchissement
périodique (`ops-refresh-cronjob.yaml`, cadence dans `opsRefresh.schedule`), utile
immédiatement après une restauration ou un rattrapage massif, sans attendre le prochain
passage planifié.

## 6. Garmin renvoie 401

Un 401 signifie que la session persistée dans `GARMINTOKENS` n'est plus valide (mot de
passe changé, session expirée côté Garmin, ou compte bloqué après trop de tentatives).

1. **Ne pas relancer le service en boucle** : chaque tentative de reconnexion échouée
   avec de mauvaises informations d'identification rapproche le compte (sans 2FA — voir
   le profil athlète) d'un blocage total.
2. Vérifier que `GARMINATOR_GARMIN_USERNAME`/`GARMINATOR_GARMIN_PASSWORD` (variables
   d'environnement du conteneur, alimentées depuis les clés `username`/`password` du
   Secret Kubernetes référencé par `ingest-deployment.yaml` et le CronJob du scheduler)
   sont toujours corrects. Le préfixe `GARMINATOR_` est obligatoire : `Settings` est un
   `BaseSettings` avec `env_prefix="GARMINATOR_"` et `extra="ignore"`, donc une variable
   non préfixée est ignorée en silence et le pod échoue au démarrage sur un champ requis
   manquant — symptôme qui ne ressemble PAS à un problème d'identifiants.
3. Si les identifiants sont corrects mais la session a simplement expiré, purger le
   contenu de `GARMINTOKENS` (sous-chemin `fitArchive.subPaths.garminTokens` du PVC)
   pour forcer une nouvelle authentification complète au prochain démarrage du pod
   d'ingestion — à faire consciemment, pas systématiquement : chaque authentification
   complète est elle-même plus visible pour Garmin qu'une reprise de session.
4. Si le blocage persiste après une authentification manuelle réussie hors du cluster
   (navigateur), il ne s'agit plus d'un problème que ce dépôt peut résoudre par du code :
   c'est un incident côté compte Garmin, à traiter directement sur leur interface.

Ce lot n'ajoute aucune détection automatique du 401 : `python -m garminator.ops health`
ne verra un tel incident qu'indirectement, via l'accumulation de fenêtres `failed` (le
message d'erreur persisté dans `raw.poll_window.last_error` par le sondage, si le 401
s'y répercute, donnera le détail exact — la CLI `health` l'affiche déjà par fenêtre).

## 7. Pourquoi pas `--dsn`

`python -m garminator.ops` accepte `--dsn` pour un usage interactif ponctuel, mais la
variable d'environnement `GARMINATOR_POSTGRES_DSN` reste le choix par défaut recommandé
et celui utilisé par tous les manifests Helm de ce lot : un DSN passé en argument de
ligne de commande porte le mot de passe PostgreSQL en clair et reste visible dans la
liste des processus du conteneur (`ps`, `/proc/<pid>/cmdline`, et
`spec.containers[].command` via `kubectl describe pod`) pendant toute la durée de vie
du processus — la variable d'environnement n'est, elle, exposée que via
`/proc/<pid>/environ`, déjà le canal utilisé par le reste du dépôt (identifiants Garmin
inclus) et jugé acceptable pour ce niveau de risque.

---

## 8. Le sondage quotidien (CronJob `scheduler`)

C'est la pièce qui interroge réellement Garmin. Manifeste :
`deploy/helm/garminator/templates/scheduler-cronjob.yaml`, valeurs sous `scheduler.*`.

| Clé | Défaut | Effet |
| --- | --- | --- |
| `scheduler.enabled` | `true` | Désactive complètement le sondage si `false`. |
| `scheduler.schedule` | `"0 5 * * *"` | Cadence. Un passage par jour suffit : Garmin consolide sommeil et HRV plusieurs heures après le réveil. |
| `scheduler.maxWindowsPerEndpoint` | `60` | Fenêtres traitées par endpoint et par passage. Borne la durée d'un run et le nombre de requêtes vers une API non publique. |
| `scheduler.activityFiles` | `true` | À `false`, ajoute `--no-activity-files` : sondage JSON seul, sans télécharger les .FIT/.TCX. |
| `scheduler.endpoints` | les 10 de `DEFAULT_ENDPOINTS` | Doit rester aligné sur le catalogue du code ; `tests/test_config_coherence.py` le vérifie. |

Ce que fait un passage, dans l'ordre : il calcule les fenêtres dues (tout jour non
encore `done` depuis `GARMINATOR_HISTORY_START`, plus les
`GARMINATOR_REFRESH_LOOKBACK_DAYS` derniers jours rejoués systématiquement, plus
toujours le jour même), sonde chaque endpoint, puis archive sur le PVC les fichiers
originaux des activités que le sondage vient de lister — sans requête supplémentaire
pour redemander cette liste.

**`concurrencyPolicy: Forbid` n'est pas du confort.** Deux passages simultanés
authentifieraient deux sessions Garmin en parallèle sur le même compte, et une salve
d'authentifications est précisément ce qui fait bloquer un compte Garmin Connect. Pour
la même raison `backoffLimit: 0` : une fenêtre en échec est déjà persistée `'failed'` en
base et sera rejouée au prochain passage, alors que réessayer immédiatement un échec
d'authentification ne fait qu'accélérer le blocage. Ne pas relancer le Job en boucle
après un échec — voir la section 6.

Le rattrapage historique n'a pas de commande dédiée : c'est ce même CronJob qui, passage
après passage, consomme les fenêtres restantes. `python -m garminator.ops
backfill-status` en donne l'avancement.

---

## 9. Réconcilier le PVC et `raw.fit_file`

`migrations/002_raw.sql` pose la règle d'atomicité — **le fichier est écrit et fsync sur
le PVC AVANT l'INSERT de sa ligne, jamais l'inverse** — et qualifie les deux désaccords
possibles. Le CronJob `ops-reconcile` (tous les jours à 07h00) les cherche. Il est
**strictement en lecture seule** : le PVC lui est monté `readOnly`, il ne supprime aucun
fichier et ne modifie aucune ligne.

| Anomalie | Gravité | Ce que ça veut dire |
|---|---|---|
| **Fichier orphelin** (sur le PVC, aucune ligne) | bénin | Archivage réussi, INSERT en échec. C'est le sens que la règle d'atomicité rend délibérément possible. Le fichier est simplement inutilisé. |
| **Ligne orpheline** (en base, aucun fichier) | **grave** | La ligne affirme l'existence d'un fichier source. Toute reconstruction de `core` depuis `raw` s'appuiera dessus et échouera, ou sautera l'activité en silence. |
| **Taille (ou sha256) divergente** | **grave** | Troncature ou écrasement : la ligne ne décrit plus son fichier. |

Le Job ne sort en échec que sur une anomalie **grave**. Un fichier orphelin le laisse en
succès, délibérément : un CronJob qui échoue pour un fichier inutilisé finit par ne plus
être lu du tout, et c'est l'alerte « ligne orpheline » qu'on ne peut pas se permettre
d'ignorer.

### Lancer un scan à la demande

```bash
kubectl create job -n garminator --from=cronjob/garminator-ops-reconcile scan-manuel
```

Puis lire le rapport :

```bash
kubectl logs -n garminator job/scan-manuel
```

### Scan profond

Par défaut le scan compare les **tailles** : sur ~1 090 fichiers de plusieurs Mo, relire
tout le volume pour un contrôle quotidien coûte cher, et la taille attrape la quasi-totalité
des cas. Sur soupçon de corruption silencieuse (même taille, contenu différent), passer
`opsReconcile.deep=true` — ou lancer ponctuellement le module à la main depuis un pod qui
monte le PVC :

```bash
python -m garminator.ops reconcile --archive-root /data/fit-archive --deep
```

### Que faire des anomalies

- **Fichiers orphelins** — rien d'urgent. Ils n'occupent que de l'espace. Avant d'en
  supprimer un, vérifier qu'aucune ligne ne le référence (le rapport l'a déjà fait, mais
  la suppression est irréversible et le rapport peut dater). Une accumulation soudaine est
  en revanche un symptôme à comprendre, pas à balayer.
- **Lignes orphelines** — c'est le cas grave. Le fichier source est perdu : la ligne
  `raw.fit_file` ne peut plus être rejouée. Si l'activité existe encore chez Garmin, la
  voie de réparation est de **supprimer la ligne** puis de laisser le sondage la
  retélécharger (une fenêtre repassée en `failed` sera reprise — voir la section 2). Si
  elle vient d'un upload manuel, le fichier d'origine est la seule source.
- **Toutes les lignes rapportées absentes d'un coup** — ce n'est pas une perte de données,
  c'est presque toujours un **PVC monté ailleurs** qu'à l'écriture. Comparer
  `--archive-root` à `ingest.config.fitArchivePath` et au `subPath` du montage. Le scan est
  volontairement bruyant dans ce cas plutôt que silencieux.

Un contexte historique utile : jusqu'à la correction du hachage des téléchargements ZIP
dans `garminator.scheduler.activity_files`, chaque passage sur la fenêtre du jour courant
réarchivait les activités du jour. Un déploiement antérieur à cette correction a donc très
probablement accumulé des fichiers orphelins — bénins, mais nombreux.

## 10. La transformation quotidienne (CronJob `transform`)

```
python -m garminator.transform [--limit N] [--athlete-id N] [--retry-failed]
```

C'est le maillon entre l'archive et les tableaux de bord : il consomme les fichiers de
`raw.fit_file` dont `parsed_at` est NULL, les décode, et écrit les lignes `core.*` dont
les vues `mart.*` dérivent. Le CronJob `<release>-transform` le lance chaque jour, entre
le sondage et le rafraîchissement des vues (`transform.schedule` dans `values.yaml`).

**Cet ordre n'est pas une préférence.** Rafraîchir les vues avant la transformation
RÉUSSIT : le rafraîchissement recopie simplement du vide. Les tableaux de bord restent
blancs une journée entière sans qu'aucun voyant ne s'allume. Un test
(`tests/test_config_coherence.py`) vérifie que les trois `schedule` du chart restent dans
l'ordre sondage → transformation → refresh ; ne pas le contourner.

### Lire le code de sortie

- **`0`** — passage nominal, **y compris avec des fichiers reportés**. Un fichier dont le
  résumé Garmin n'est pas encore ingéré est un état transitoire attendu : ni `parsed_at`
  ni `parse_error` ne sont posés, le fichier revient de lui-même dans la file au passage
  suivant. Le compter comme un échec ferait échouer le CronJob presque chaque nuit, et un
  CronJob qui échoue toutes les nuits est un CronJob qu'on cesse de regarder.
- **`1`** — au moins un fichier en échec. L'incident est déjà consigné dans
  `raw.fit_file.parse_error` ; le code de sortie sert seulement à le rendre visible sans
  lire les logs.
- **`2`** — le passage n'a pas pu commencer : DSN absent, ou `core.athlete` vide ou
  ambiguë (voir l'étape 3 du premier démarrage). Distinct de `1` parce que **rien n'a été
  tenté** : c'est une erreur de configuration, pas une donnée fautive.

### Un fichier en échec

```
SELECT fit_file_id, garmin_activity_id, storage_path, parse_error
FROM raw.fit_file WHERE parse_error IS NOT NULL ORDER BY downloaded_at DESC;
```

Un fichier portant un `parse_error` **sort de la file** et n'est plus retenté : sans
cela, un fichier indigeste ferait échouer le passage toutes les nuits. Une fois la cause
corrigée (correspondance de sport manquante, décodeur à réparer) :

```
python -m garminator.transform --retry-failed
```

Un succès efface le `parse_error` précédent. Rejouer **avant** d'avoir corrigé la cause
ne fait que reproduire le même échec.

### Rattraper un retard

Chaque activité est commitée individuellement : un passage interrompu (éviction de pod,
redémarrage de nœud) ne perd que le fichier en cours, jamais le lot. Le backfill initial
se résorbe donc sur plusieurs nuits, à `transform.batchLimit` fichiers par passage. Pour
l'accélérer sans attendre :

```
kubectl create job --from=cronjob/<release>-transform rattrapage-transform
```

`concurrencyPolicy: Forbid` empêche ce Job manuel de croiser le passage planifié.

### Ce que la transformation n'écrit jamais avant d'avoir fini

L'ordre d'écriture est le même principe que celui de l'archivage (fichier fsyncé sur le
PVC AVANT l'insertion de sa ligne) : le bundle `core.*` est **commité avant** que
`parsed_at` ne soit posé. Une coupure entre les deux laisse donc le fichier dans la file,
et il sera retraité — l'écriture étant idempotente, cela ne produit pas de doublon. Poser
`parsed_at` d'abord aurait fait disparaître silencieusement l'activité de la file sans
qu'elle soit écrite nulle part.

## 11. Envoyer un fichier manuellement

```bash
curl -F "file=@/chemin/vers/activite.fit" http://localhost:8000/uploads
```

L'endpoint `POST /uploads` (`garminator.api.upload`) est **local uniquement** pour
l'instant — pas d'Ingress, pas de TLS, pas d'authentification (décision assumée, voir le
rapport du lot qui l'a introduit). Il sert pour une activité que le sondage Garmin n'a
pas ou ne peut pas récupérer : import depuis un autre outil, activité effacée côté
Garmin avant le passage du CronJob `scheduler`, etc.

Le format (FIT, TCX, ou ZIP contenant l'un des deux) est détecté **par le contenu**,
jamais par l'extension du nom de fichier. Le fichier est archivé sur le PVC — même
règle d'atomicité que le sondage : écrit et fsyncé AVANT que sa ligne `raw.fit_file` ne
soit insérée — avec `garmin_activity_id = NULL`. C'est la seule marque de provenance :
`raw.fit_file` ne porte aucune colonne dédiée à distinguer un envoi manuel d'un
téléchargement Garmin, la valeur NULL suffit et c'est la convention posée par
`migrations/012_raw_fit_file_manual_upload.sql`.

Réponses possibles :

| Code | Sens |
|---|---|
| `201` | Archivé et parsé avec succès. Le corps renvoie `fit_file_id`. |
| `409` | Le contenu (même `sha256`) est déjà rattaché en base à une AUTRE provenance — activité Garmin réelle, ou envoi manuel distinct. Rien n'est perdu : le fichier reste archivé, aucune ligne n'est créée ni modifiée. |
| `413` | Corps de requête au-dessus de la borne de taille. |
| `422` | Contenu non reconnu (ni FIT ni TCX, ZIP vide/à plusieurs membres/dangereux), ou archivage réussi mais parsing local échoué — le corps de la réponse le précise dans les deux cas, et l'archive n'est jamais perdue même en cas de parsing raté. |

### La limite connue : un envoi manuel reste « skipped »

**Un envoi manuel est archivé dans `raw.*` mais n'atteint jamais `core.*`.** Le motif
n'est pas que le fichier serait muet — il ne l'est pas : la durée se déduit des deux
formats, et le sport figure dans le message `session` d'un FIT comme dans l'attribut
`Sport` d'un TCX, même si aucun lecteur ne les extrait aujourd'hui. Le fuseau est le
seul élément réellement absent, et `core.athlete.home_timezone` en tient lieu.

Les deux obstacles réels sont structurels :

1. `garminator.transform` ne sait construire son `ActivityContext` qu'à partir du
   résumé Garmin (`get_activities_by_date`), que le sondage associe à chaque fichier
   téléchargé et qu'un envoi manuel n'a pas, puisqu'il n'est jamais passé par le
   sondage. Il n'existe pas de second chemin partant du fichier décodé.
2. `core.activity.activity_id` **est** l'identifiant Garmin : clé primaire, sans
   séquence ni valeur par défaut. Un envoi manuel n'a aucune clé à fournir.

En attendant, `transform` laisse le fichier dans la file au statut « skipped » — ni
`parsed_at` ni `parse_error` ne sont posés, exactement comme un fichier Garmin dont le
résumé n'est pas encore arrivé (voir « Lire le code de sortie » ci-dessus). Le jour où
les deux manques ci-dessus seront comblés, les fichiers déjà envoyés seront repris sans
intervention.

C'est un manque assumé et documenté, pas un bug : aucune valeur n'est inventée ni
devinée pour combler le trou.
