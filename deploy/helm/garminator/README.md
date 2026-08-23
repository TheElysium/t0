# Chart Helm garminator

Déploiement homelab de la chaîne garminator : ingestion Garmin (`garminator-ingest`),
PostgreSQL, Grafana. **Accès strictement local** : aucun Ingress, aucun TLS. Les
Services sont en `ClusterIP` ; l'accès se fait par `kubectl port-forward`.

Ce chart ne déploie rien tout seul — c'est l'athlète qui exécute `helm install`
sur son cluster homelab.

## Avant d'installer : créer le Secret Garmin

**Ce chart ne crée jamais le Secret contenant les identifiants Garmin.** Le compte
Garmin de l'athlète n'a pas de double authentification (2FA) : ce Secret vaut donc
l'accès complet au compte. Il doit exister dans le namespace cible avant
`helm install`, sous le nom donné par `garminSecret.name` (par défaut
`garminator-garmin-credentials`), avec les clés `username` et `password`.

```bash
kubectl create namespace garminator   # si besoin

kubectl create secret generic garminator-garmin-credentials \
  --namespace garminator \
  --from-literal=username='REMPLACER_PAR_EMAIL_GARMIN' \
  --from-literal=password='REMPLACER_PAR_MOT_DE_PASSE_GARMIN'
```

**Un Secret Kubernetes n'est pas chiffré.** Ses valeurs sont seulement encodées en
base64 (`kubectl get secret garminator-garmin-credentials -o jsonpath='{.data.password}' | base64 -d`
les révèle instantanément). Toute personne ayant accès à l'API server, à `etcd` non
chiffré au repos, ou aux sauvegardes d'`etcd`, peut lire ces identifiants en clair.
Si le cluster ne chiffre pas les Secrets au repos (`EncryptionConfiguration`), c'est
le seul niveau de protection réel. Ne partagez jamais ce Secret, ne le committez
jamais, et effacez l'historique shell après la commande ci-dessus si nécessaire.

Si les clés ou le nom du Secret diffèrent, adaptez `garminSecret.name`,
`garminSecret.usernameKey` et `garminSecret.passwordKey` dans `values.yaml`.

## Avant d'installer : choisir les mots de passe (obligatoire)

**Ce chart ne genere aucun mot de passe aleatoire, volontairement.**

Le mot de passe reel de PostgreSQL est fige par `initdb` a la **creation du volume
de donnees** et n'est plus jamais relu depuis `POSTGRES_PASSWORD` ensuite. Un chart
qui regenere le contenu du Secret a chaque `helm upgrade` laisserait donc une base
intacte avec l'ancien mot de passe et une application configuree avec le nouveau :
« authentification refusee » sur une base qui fonctionnait la veille, sans aucune
modification apparente. Les mecanismes a base de `lookup` ne protegent pas de ce
scenario (`lookup` rend une valeur vide pendant `helm template`, pendant
`--dry-run`, et dans tout flux `helm template | kubectl apply`), et l'annotation
`helm.sh/resource-policy: keep` ne protege que contre la *suppression*, jamais
contre la *mise a jour* du contenu (elle laisse en plus le Secret orphelin apres
`helm uninstall`, ce qui fait echouer la reinstallation suivante).

Generer les mots de passe **une fois**, puis les conserver dans un fichier de
valeurs local **exclu du depot git** :

```bash
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32   # PostgreSQL
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32   # admin Grafana
```

```yaml
# mes-valeurs.yaml - NE JAMAIS COMMITTER
postgresql:
  auth:
    password: "..."
grafana:
  admin:
    password: "..."
```

Variante sans mot de passe dans un fichier de valeurs : creer les Secrets a la
main et les referencer via `postgresql.auth.existingSecret` (cles `username` et
`password`) et `grafana.admin.existingSecret` (cles `admin-user` et
`admin-password`).

Si rien n'est fourni, `helm template` et `helm install` echouent avec un message
explicite. C'est le comportement voulu : mieux vaut un refus au premier jour
qu'une panne d'authentification au premier `helm upgrade`.

**Changer `postgresql.auth.password` apres le premier deploiement ne change pas
le mot de passe dans la base.** Il faut d'abord `ALTER USER garminator WITH
PASSWORD '...';` sur l'instance, puis mettre a jour la valeur.

## Installation

```bash
helm lint deploy/helm/garminator -f mes-valeurs.yaml

helm install garminator deploy/helm/garminator \
  --namespace garminator \
  --create-namespace \
  -f mes-valeurs.yaml \
  --set ingest.image.repository=ghcr.io/VOTRE_ORG/garminator \
  --set ingest.image.tag=0.1.0
```

Paramètres à revoir avant un premier déploiement réel (voir `values.yaml`, commenté) :

- `ingest.image.repository` / `ingest.image.tag` — image publiée par la CI (lot L0) sur GHCR.
- `postgresql.persistence.storageClassName`, `fitArchive.persistence.storageClassName`,
  `grafana.persistence.storageClassName` — StorageClass du cluster homelab (vide = StorageClass
  par défaut du cluster).
- Les tailles de volumes (`*.persistence.size`), à ajuster à l'historique réel de l'athlète
  (backfill depuis 2025-04-03, lot L10).
- `backup.schedule` et `backup.retention` si le rythme hebdomadaire par défaut ne convient pas.

## Le sondage Garmin (CronJob `scheduler`)

C'est le seul objet du chart qui appelle Garmin Connect. Il remplace la boucle de
sondage qui n'a jamais existé dans le Deployment `ingest` — celui-ci ne sert que
l'endpoint d'upload manuel.

```bash
# Déclencher un passage immédiat, sans attendre la cadence planifiée
kubectl create job -n garminator --from=cronjob/garminator-scheduler sondage-manuel

# Suivre le run en cours
kubectl logs -n garminator -l app.kubernetes.io/component=scheduler --tail=100 -f
```

Clés `values.yaml` (`scheduler.*`) : `enabled`, `schedule` (défaut `0 5 * * *`),
`maxWindowsPerEndpoint` (défaut `60`, borne le nombre de fenêtres — donc de requêtes —
par passage), `activityFiles` (à `false`, ajoute `--no-activity-files` : sondage JSON
seul, sans télécharger les .FIT/.TCX), et `endpoints`, qui doit rester aligné sur
`DEFAULT_ENDPOINTS` côté code (`tests/test_config_coherence.py` le vérifie).

Le rattrapage historique n'a pas de Job dédié : ce même CronJob consomme les fenêtres
restantes passage après passage, depuis `ingest.config.backfillStartDate`. Le CronJob
`ops-backfill-status` en publie l'avancement, en lecture seule.

`concurrencyPolicy: Forbid` et `backoffLimit: 0` sont délibérés : deux passages
simultanés, ou un réessai immédiat après échec, multiplieraient les authentifications
SSO sur le même compte — c'est exactement ce qui fait bloquer un compte Garmin Connect.

### Transformation `raw` → `core` (`transform`)

Le CronJob `transform` (clés `transform.*`, défaut `15 5 * * *`) décode les fichiers
archivés et écrit les lignes `core.*` dont les vues `mart.*` dérivent. Il est planifié
**entre** le sondage (`0 5`) et le rafraîchissement des vues (`30 5`), et cet ordre est
une exigence : rafraîchir avant la transformation réussit — en recopiant du vide — et
laisse les tableaux de bord blancs sans le moindre signal d'erreur.
`tests/test_config_coherence.py` refuse toute valeur qui romprait cet ordre.

Le PVC d'archivage y est monté au **même `mountPath`** que dans le CronJob de sondage
(`storage_path` est un chemin absolu enregistré en base) et en `readOnly: true` : la
transformation lit l'archive, elle ne la modifie jamais.

`transform.batchLimit` (défaut `200`, aligné sur le défaut du code) borne le nombre de
fichiers par passage. Chaque activité étant commitée individuellement, un passage
interrompu ne perd que le fichier en cours. Voir la section 10 du `RUNBOOK` pour la
lecture des codes de sortie — notamment le fait qu'un fichier **reporté** (résumé Garmin
pas encore ingéré) n'est pas une erreur.

**Prérequis** : `core.athlete` et `core.threshold` doivent être amorcés à la main
(`deploy/seed/athlete.sql.example`), sinon ce Job s'arrête en code 2 sans rien écrire.

### Réconciliation PVC ↔ base (`ops-reconcile`)

Le CronJob `ops-reconcile` (clés `opsReconcile.*`, défaut `0 7 * * *`, soit après le
sondage et le rafraîchissement) compare l'inventaire de `raw.fit_file` au contenu réel
du volume d'archivage. Il **ne supprime rien et n'écrit rien** : le PVC y est monté en
`readOnly: true`, ce qui n'est pas une convention mais une garantie du kubelet.

Les deux anomalies ne sont pas symétriques, et le code de sortie s'en tient à celle qui
compte : une **ligne sans fichier** (interdite par la règle d'atomicité de
`migrations/002_raw.sql`) fait échouer le Job ; un **fichier orphelin** le laisse en
succès, délibérément — un Job qui échoue pour un fichier inutilisé finit par n'être plus
lu, et c'est l'autre alerte qu'on ne peut pas se permettre de manquer.

`opsReconcile.deep: true` recalcule le sha256 de chaque fichier au lieu de comparer sa
taille : cela attrape une corruption à taille constante, au prix d'une relecture
intégrale du volume. À réserver au diagnostic. Procédure et remédiation :
[`docs/RUNBOOK.md`](../../../docs/RUNBOOK.md) §9.

## Accès aux services (port-forward)

Aucun des services n'est exposé hors du cluster. Depuis un poste avec accès au
cluster :

```bash
# Grafana (interface web)
kubectl port-forward -n garminator svc/garminator-grafana 3000:3000
# puis http://localhost:3000 - identifiant admin : celui que vous avez fourni
# dans `grafana.admin.password`, relisible depuis le Secret si besoin :
kubectl get secret -n garminator garminator-grafana \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo

# Endpoint d'upload manuel de fichiers .FIT (lot L7)
kubectl port-forward -n garminator svc/garminator-ingest 8080:8080

# PostgreSQL, pour une requête ponctuelle (psql, DBeaver, ...)
kubectl port-forward -n garminator svc/garminator-postgresql 5432:5432
```

Les Services sont contraints a `ClusterIP` : le chart **refuse** de rendre
`NodePort` ou `LoadBalancer` (message d'erreur explicite), puisqu'il n'y a pas de
TLS et que l'exigence est un acces strictement local.

Le mot de passe PostgreSQL applicatif (compte `garminator`, distinct du compte
Garmin) se récupère de la même façon :

```bash
kubectl get secret -n garminator garminator-postgresql \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

## Volumes persistants

Trois PVC :

| PVC | Contenu | Notes |
|---|---|---|
| `<release>-postgresql` (via `volumeClaimTemplates` du StatefulSet) | données PostgreSQL | `PGDATA` dans le sous-répertoire `pgdata`, jamais à la racine du volume |
| `<release>-fit-archive` | fichiers `.FIT` archivés (`fit-files/`), **token store Garmin** (`garmin-tokens/`), dumps PostgreSQL (`postgres-backups/`) | volume unique à trois sous-répertoires (`subPath`), pour rester à trois PVC |
| `<release>-grafana` | données Grafana (dashboards édités en base, plugins, etc.) | optionnel, désactivable via `grafana.persistence.enabled: false` |

**Attention `ReadWriteOnce`** : le PVC `fit-archive` est monte a la fois par le pod
d'ingestion et par le pod du `CronJob` de sauvegarde. Avec le mode d'acces
`ReadWriteOnce` par defaut, les deux pods doivent tourner **sur le meme noeud** ;
sur un cluster multi-noeuds, le Job de sauvegarde resterait indefiniment `Pending`.
Sur un homelab mono-noeud, c'est sans consequence. Sinon, passer
`fitArchive.persistence.accessModes` a `ReadWriteMany` (StorageClass compatible
requise) ou separer les dumps sur leur propre PVC.

Les pods qui partagent ce PVC utilisent **le meme `fsGroup`** (celui de
`ingest.securityContext.fsGroup`, 10001 par defaut). C'est indispensable : le
kubelet applique `fsGroup` recursivement au volume a chaque montage, et deux
valeurs differentes s'ecraseraient mutuellement - l'ingestion perdrait le droit
d'ecrire son archive FIT et son token store apres le premier passage de la
sauvegarde.

Le pod d'ingestion tourne avec `readOnlyRootFilesystem: true`. Les seuls chemins
inscriptibles sont donc le PVC `fit-archive` (archive FIT + token store) et un
`emptyDir` monte sur `/tmp` (borne par `ingest.tmpDirSizeLimit`). Les variables
`HOME`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` et `PYTHONPYCACHEPREFIX`
sont redirigees vers `/tmp` pour cette raison, et `PYTHONDONTWRITEBYTECODE=1`
evite toute tentative d'ecriture de `.pyc` dans `site-packages` (lecture seule).
Toute nouvelle dependance qui ecrirait ailleurs doit etre ajoutee a cette liste.

Le token store Garmin (`GARMINTOKENS`, variable d'environnement lue par
`garminator-ingest`) est **persistant** : sans cela, chaque redémarrage du pod
d'ingestion referait une authentification SSO complète auprès de Garmin, ce qui
expose un compte sans 2FA au blocage (voir le principe n°1 du plan macro et
issue #233).

## Restauration depuis une sauvegarde

Le `CronJob` `<release>-postgresql-backup` exécute `pg_dump --format=custom`
chaque semaine (par défaut dimanche 03h00, `backup.schedule`) vers
`<pvc fit-archive>/postgres-backups/`, avec rotation simple (`backup.retention`
dumps conservés).

Trois garanties du script de sauvegarde :

- `pg_dump` n'est **pas** place dans un tube : son code de retour remonte
  directement et `set -e` fait echouer le Job. Un dump vide echoue aussi
  explicitement. Une sauvegarde qui echoue en silence est pire que pas de
  sauvegarde - surveiller `kubectl get jobs -n garminator`.
- Le dump est ecrit sous `.dump.part` puis renomme : un dump interrompu ne porte
  jamais le nom definitif, n'est jamais compte par la rotation et ne peut jamais
  etre choisi pour une restauration.
- La rotation n'utilise ni `ls -1t`, ni `tail`, ni `xargs -r` : les noms portent
  un horodatage UTC de largeur fixe, donc l'ordre lexicographique du glob est
  l'ordre chronologique. Aucune dependance a une option GNU dont la presence
  n'est pas garantie dans le busybox d'une image alpine.

Pour restaurer manuellement un dump (à exécuter en dehors des heures d'ingestion,
idéalement après avoir mis `ingest.replicaCount: 0` le temps de l'opération) :

```bash
# 1. Lister les dumps disponibles, via un pod temporaire monté sur le PVC.
kubectl run -n garminator restore-shell --rm -it --restart=Never \
  --image=postgres:16-alpine \
  --overrides='{"spec":{"containers":[{"name":"restore-shell","image":"postgres:16-alpine","command":["sh"],"stdin":true,"tty":true,"volumeMounts":[{"name":"fit-archive","mountPath":"/backups","subPath":"postgres-backups"}]}],"volumes":[{"name":"fit-archive","persistentVolumeClaim":{"claimName":"garminator-fit-archive"}}]}}' \
  -- ls -la /backups

# 2. Copier le dump choisi hors du cluster (ou l'utiliser directement depuis
#    un pod avec accès réseau à PostgreSQL), puis restaurer :
kubectl port-forward -n garminator svc/garminator-postgresql 5432:5432 &
pg_restore --host=localhost --port=5432 \
  --username=garminator --dbname=garminator \
  --clean --if-exists \
  chemin/vers/garminator-YYYYMMDDTHHMMSSZ.dump
```

**Cohérence dump / PVC (lot L10) :** un `pg_dump` seul ne restaure que l'état de
PostgreSQL, pas les fichiers `.FIT` archivés ni le token store Garmin. Pour une
restauration complète cohérente à un même instant, prenez un instantané (snapshot)
du PVC `fit-archive` **au même moment** que le `pg_dump` — sinon la base peut
référencer des fichiers FIT absents de l'archive restaurée, ou l'inverse. La
procédure d'exploitation détaillée est du ressort du lot L10.

## NetworkPolicy (optionnelle)

Désactivée par défaut (`networkPolicy.enabled: false`) : dans un homelab, le
réseau du cluster est généralement déjà de confiance, et une NetworkPolicy mal
réglée peut bloquer silencieusement le poll Garmin (sortie HTTPS) sans qu'aucun
Service ne le signale. Si activée, elle limite PostgreSQL aux pods du Release,
Grafana à une sortie unique vers PostgreSQL, et l'ingestion à PostgreSQL + DNS +
HTTPS sortant (nécessaire pour joindre l'API Garmin).

## Tableaux de bord Grafana (lot L9)

La ConfigMap `<release>-grafana-dashboards` est créée **vide** par ce chart et
montée dans `/var/lib/grafana/dashboards`, avec le provider de provisioning déjà
en place (`/etc/grafana/provisioning/dashboards`). Le lot L9 n'a qu'à y ajouter
des clés `*.json` (ou fournir sa propre ConfigMap via
`grafana.dashboards.configMapName`) — aucune modification de ce chart n'est
nécessaire pour publier de nouveaux dashboards.

## Validation hors ligne

Ce chart est conçu pour être entièrement validé sans toucher à un cluster :

Trois jeux de valeurs de reference sont fournis dans `ci/` (mots de passe de test,
jamais deployes) :

```bash
for v in deploy/helm/garminator/ci/*.yaml; do
  helm lint     deploy/helm/garminator -f "$v"
  helm template garminator deploy/helm/garminator -f "$v" > /dev/null
done
```

- `ci/01-defaut-values.yaml` - configuration par defaut, secrets fournis.
- `ci/02-secrets-externes-values.yaml` - tous les Secrets preexistants, NetworkPolicy activee.
- `ci/03-minimal-values.yaml` - sans Grafana, sans sauvegarde, sans persistance.

Sans fichier de valeurs, `helm template` **echoue volontairement** (mots de passe
non fournis, voir plus haut).

Validation de schema Kubernetes (`kubeconform`) : a executer cote athlete, l'outil
n'est pas installe dans l'environnement des agents et son installation demanderait
un acces reseau.

```bash
helm template garminator deploy/helm/garminator -f mes-valeurs.yaml \
  | kubeconform -strict -summary
```

Aucune commande `helm install`, `helm upgrade` ou `kubectl apply` n'est exécutée
par les agents qui développent ce dépôt (principe n°5 du plan macro).
