{{/*
Nom court du chart.
*/}}
{{- define "garminator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Nom complet (release + chart), tronque a 63 caracteres (limite DNS).
*/}}
{{- define "garminator.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart + version, pour le label helm.sh/chart.
*/}}
{{- define "garminator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels communs.
*/}}
{{- define "garminator.labels" -}}
helm.sh/chart: {{ include "garminator.chart" . }}
{{ include "garminator.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Values.commonLabels }}
{{ toYaml .Values.commonLabels }}
{{- end }}
{{- end -}}

{{/*
Labels de selection (stables entre upgrades — ne jamais y toucher).
*/}}
{{- define "garminator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "garminator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Nom du Secret d'authentification PostgreSQL.
*/}}
{{- define "garminator.postgresql.secretName" -}}
{{- if .Values.postgresql.auth.existingSecret -}}
{{- .Values.postgresql.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-postgresql" (include "garminator.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Nom du service PostgreSQL (headless).
*/}}
{{- define "garminator.postgresql.serviceName" -}}
{{- printf "%s-postgresql" (include "garminator.fullname" .) -}}
{{- end -}}

{{/*
Nom du PVC de l'archive FIT (partage : fichiers FIT, token store Garmin,
dumps PostgreSQL — voir values.yaml fitArchive.subPaths).
*/}}
{{- define "garminator.fitArchive.pvcName" -}}
{{- printf "%s-fit-archive" (include "garminator.fullname" .) -}}
{{- end -}}

{{/*
Nom du Secret admin Grafana.
*/}}
{{- define "garminator.grafana.secretName" -}}
{{- if .Values.grafana.admin.existingSecret -}}
{{- .Values.grafana.admin.existingSecret -}}
{{- else -}}
{{- printf "%s-grafana" (include "garminator.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Nom de la ConfigMap des dashboards Grafana (mecanisme prevu pour L9).
*/}}
{{- define "garminator.grafana.dashboardsConfigMapName" -}}
{{- if .Values.grafana.dashboards.configMapName -}}
{{- .Values.grafana.dashboards.configMapName -}}
{{- else -}}
{{- printf "%s-grafana-dashboards" (include "garminator.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Tag d'image de l'ingestion : celui fourni, sinon AppVersion du chart.
*/}}
{{- define "garminator.ingest.imageTag" -}}
{{- default .Chart.AppVersion .Values.ingest.image.tag -}}
{{- end -}}

{{/*
Mot de passe PostgreSQL applicatif.

DELIBEREMENT SANS GENERATION ALEATOIRE. Le mot de passe reel de PostgreSQL est
fige par `initdb` a la creation du volume de donnees et n'est plus jamais relu
depuis POSTGRES_PASSWORD ensuite. Toute valeur regeneree a un `helm upgrade`
produirait un Secret desynchronise du volume, et donc un refus
d'authentification sur une base qui fonctionnait la veille.

`lookup` ne peut pas servir de garde-fou : il rend une valeur vide pendant
`helm template`, pendant `--dry-run`, et dans tout flux `helm template | kubectl
apply`. Le mot de passe doit donc etre fourni explicitement, ou provenir d'un
Secret preexistant (`postgresql.auth.existingSecret`).
*/}}
{{- define "garminator.postgresql.password" -}}
{{- if .Values.postgresql.auth.password -}}
{{- .Values.postgresql.auth.password -}}
{{- else -}}
{{- fail "\n\nERREUR : postgresql.auth.password est obligatoire.\n\nAucun mot de passe PostgreSQL n'est genere par ce chart : le mot de passe reel est\nfige par initdb a la creation du volume, et un mot de passe regenere a chaque\n`helm upgrade` casserait l'authentification de facon silencieuse et difficile a\ndiagnostiquer.\n\nDeux solutions :\n\n  1. Fournir le mot de passe dans les valeurs (le generer une fois, puis le\n     conserver dans le fichier de valeurs local, hors depot git) :\n\n       openssl rand -base64 32 | tr -d '/+=' | cut -c1-32\n       helm install ... --set postgresql.auth.password='<le mot de passe>'\n\n  2. Referencer un Secret Kubernetes preexistant contenant les cles `username`\n     et `password` :\n\n       kubectl create secret generic garminator-postgresql \\\n         --namespace garminator \\\n         --from-literal=username=garminator \\\n         --from-literal=password='<le mot de passe>'\n       helm install ... --set postgresql.auth.existingSecret=garminator-postgresql\n" -}}
{{- end -}}
{{- end -}}

{{/*
Mot de passe admin Grafana. Meme regle que ci-dessus : aucune generation
aleatoire, pour qu'un `helm upgrade` ne fasse jamais deriver le contenu du
Secret par rapport a ce que l'athlete connait.
*/}}
{{- define "garminator.grafana.adminPassword" -}}
{{- if .Values.grafana.admin.password -}}
{{- .Values.grafana.admin.password -}}
{{- else -}}
{{- fail "\n\nERREUR : grafana.admin.password est obligatoire.\n\nAucun mot de passe n'est genere par ce chart (un mot de passe regenere a chaque\n`helm upgrade` derive silencieusement de ce que l'athlete a note).\n\n  helm install ... --set grafana.admin.password='<le mot de passe>'\n\nou referencer un Secret preexistant avec les cles `admin-user` et\n`admin-password` via grafana.admin.existingSecret.\n" -}}
{{- end -}}
{{- end -}}

{{/*
Garde-fou d'exposition : l'exigence de l'athlete est un acces STRICTEMENT LOCAL
(aucun Ingress, aucun TLS, aucune exposition hors cluster). On refuse donc les
types de Service qui ouvriraient un acces externe.
*/}}
{{- define "garminator.assertLocalOnlyService" -}}
{{- $type := .type -}}
{{- if not (eq $type "ClusterIP") -}}
{{- fail (printf "\n\nERREUR : %s = %q.\n\nCe chart est concu pour un acces strictement local (aucun Ingress, aucun TLS).\nSeul le type ClusterIP est autorise ; l'acces se fait par `kubectl port-forward`.\nExposer ces services en NodePort ou LoadBalancer publierait Grafana et l'endpoint\nd'upload FIT sans TLS sur le reseau.\n" .key $type) -}}
{{- end -}}
{{- end -}}
