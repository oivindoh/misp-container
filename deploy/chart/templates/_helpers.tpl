{{/* The tag of the two images: image.tag, else the chart's appVersion */}}
{{- define "misp.tag" -}}
{{- .Values.image.tag | default .Chart.AppVersion -}}
{{- end }}

{{/* Labels of a resource. Selectors use app.kubernetes.io/name only. */}}
{{- define "misp.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: misp
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ include "misp.tag" .root | quote }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Labels of a pod template. The network policies select a release's pods by
app.kubernetes.io/instance and part-of; the Services and Deployments by name.
*/}}
{{- define "misp.podLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: misp
{{- end }}

{{/* Scheduling and pull secrets of every pod. No pod talks to the Kubernetes API. */}}
{{- define "misp.podScheduling" -}}
automountServiceAccountToken: false
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- range . }}
  - name: {{ . }}
  {{- end }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Pod security context of a pod that runs the main image, as UID 1000 */}}
{{- define "misp.podSecurityContext" -}}
securityContext:
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  # Without it the kubelet changes the owner of every file on the attachments claim
  # at each pod start, on a CSI driver with fsGroupPolicy File such as CephFS
  fsGroupChangePolicy: OnRootMismatch
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{/* Container security context: read-only root, no privileges */}}
{{- define "misp.containerSecurityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
{{- end }}

{{/* The main image */}}
{{- define "misp.image" -}}
image: {{ .Values.image.misp }}:{{ include "misp.tag" . }}
imagePullPolicy: {{ .Values.image.pullPolicy }}
{{- end }}

{{/*
The mounts of a pod that renders app/Config and runs MISP: configure, web,
worker and the task CronJobs. Each pod renders its own app/Config.
*/}}
{{- define "misp.appVolumeMounts" -}}
- name: files-scripts-tmp
  mountPath: /var/www/MISP/app/files/scripts/tmp
- name: files-certs
  mountPath: /var/www/MISP/app/files/certs
- name: files-terms
  mountPath: /var/www/MISP/app/files/terms
# MISP uploads org logos and custom images here and inlines them into its pages
# (Server::getFileRules, OrgImgHelper; the image-paths check of
# scripts/check_upstream.py), so every pod sees the same files
- name: attachments
  mountPath: /var/www/MISP/app/files/img/orgs
  subPath: img/orgs
- name: attachments
  mountPath: /var/www/MISP/app/files/img/custom
  subPath: img/custom
- name: misp-config
  mountPath: /var/www/MISP/app/Config
- name: misp-tmp
  mountPath: /var/www/MISP/app/tmp
- name: misp-gnupg
  mountPath: /var/www/MISP/.gnupg
- name: gnupg-key
  mountPath: /etc/misp-docker/gnupg
  readOnly: true
- name: certs
  mountPath: /etc/misp-docker/certs
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- with include "misp.dbCAVolumeMount" . }}
{{ . }}
{{- end }}
{{- end }}

{{- define "misp.appVolumes" -}}
- name: files-scripts-tmp
  emptyDir:
    sizeLimit: 500Mi
- name: files-certs
  emptyDir:
    sizeLimit: 10Mi
- name: files-terms
  emptyDir:
    sizeLimit: 10Mi
- name: misp-config
  emptyDir:
    sizeLimit: 10Mi
- name: misp-tmp
  emptyDir:
    sizeLimit: 1Gi
- name: misp-gnupg
  emptyDir:
    sizeLimit: 10Mi
- name: tmp
  emptyDir:
    sizeLimit: 100Mi
# The instance GPG key and the sync server certificates, copied in at start
- name: gnupg-key
  secret:
    secretName: misp-gnupg
    optional: true
- name: certs
  secret:
    secretName: misp-certs
    optional: true
{{- with include "misp.dbCAVolume" . }}
{{ . }}
{{- end }}
{{- end }}

{{/* The databaseCA certificate at DB_SSLROOTCERT, in every pod that connects to the database */}}
{{- define "misp.dbCAVolumeMount" -}}
{{- if .Values.databaseCA -}}
- name: db-ca
  mountPath: /etc/misp-docker/db-ca
  readOnly: true
{{- end }}
{{- end }}

{{- define "misp.dbCAVolume" -}}
{{- with .Values.databaseCA -}}
- name: db-ca
  secret:
    secretName: {{ .name }}
    items:
      - key: {{ .key }}
        path: ca.crt
{{- end }}
{{- end }}

{{/* The attachments volume: the shared claim, or an emptyDir per pod when attachments are in S3 */}}
{{- define "misp.attachmentsVolume" -}}
- name: attachments
  {{- if .Values.attachments.claim }}
  persistentVolumeClaim:
    claimName: attachments
  {{- else }}
  emptyDir:
    sizeLimit: 1Gi
  {{- end }}
{{- end }}

{{/*
The checksum of the configuration a pod runs with: misp-env, and misp-db and misp-app when
the chart makes them. A change rolls the pods, and each pod waits for a configure run of the
same checksum (MISP_CHECKSUM, misp.checksumEnv), so a revision whose configure Job
refuses its configuration never serves.
*/}}
{{- define "misp.checksums" -}}
{{- $secrets := "" }}
{{- if .Values.secrets.create }}
{{- $secrets = printf "%s%s" (toJson .Values.secrets.db) (toJson .Values.secrets.app) }}
{{- end }}
checksum/config: {{ printf "%s%s%s" (include (print .Template.BasePath "/configmap-env.yaml") .) (include (print .Template.BasePath "/configmap-settings.yaml") .) $secrets | sha256sum }}
{{- end }}

{{/* The pod's checksum/config annotation as MISP_CHECKSUM; empty in a pod without it */}}
{{- define "misp.checksumEnv" -}}
- name: MISP_CHECKSUM
  valueFrom:
    fieldRef:
      fieldPath: metadata.annotations['checksum/config']
{{- end }}

{{/*
The env of a container of the misp image: the connection Secrets (misp.connectionEnv), the
misp-env ConfigMap, and the Secrets named. A container that gets misp-app runs MISP: it also
gets the repository's settings (misp-settings, and the settings a Secret holds). Call it
with (list $root "misp-db" ...).
*/}}
{{- define "misp.appEnv" -}}
{{- $root := first . }}
{{- $misp := has "misp-app" (rest .) }}
env:
  {{- include "misp.checksumEnv" $root | nindent 2 }}
  {{- with include "misp.connectionEnv" (concat (list $root "misp-env") (rest .)) }}
  {{- . | nindent 2 }}
  {{- end }}
  {{- if $misp }}
  {{- range $var, $entry := include "misp.settings" $root | fromJson }}
  {{- with $entry.secretKeyRef }}
  - name: {{ $var }}
    valueFrom:
      secretKeyRef:
        name: {{ .name }}
        key: {{ .key }}
        optional: true
  {{- end }}
  {{- end }}
  {{- end }}
envFrom:
  - configMapRef:
      name: misp-env
  {{- if $misp }}
  - configMapRef:
      name: misp-settings
  {{- end }}
{{- range rest . }}
  - secretRef:
      name: {{ . }}
{{- end }}
{{- end }}

{{/*
The env vars a connection Secret may fill, each with the source it replaces.
*/}}
{{- define "misp.connectionVars" -}}
{{- toJson (dict
      "DB_HOST" "misp-env" "DB_PORT" "misp-env" "DB_NAME" "misp-env"
      "DB_USER" "misp-db" "DB_PASSWORD" "misp-db"
      "MISP_REDIS_HOST" "misp-env" "MISP_REDIS_PORT" "misp-env" "MISP_REDIS_PASSWORD" "misp-app") }}
{{- end }}

{{/*
The secretKeyRef of each env var that connectionSecrets fills, as JSON. A later entry wins.
*/}}
{{- define "misp.connectionRefs" -}}
{{- $refs := dict }}
{{- range $secret := .Values.connectionSecrets }}
{{- range $var, $key := $secret.keys }}
{{- $_ := set $refs $var (dict "name" $secret.name "key" $key) }}
{{- end }}
{{- end }}
{{- toJson $refs }}
{{- end }}

{{/*
env entries from connectionSecrets for a container that reads these sources through envFrom:
an entry replaces the var of a source the container reads, and env wins over envFrom. Takes
(list root source ...).
*/}}
{{- define "misp.connectionEnv" -}}
{{- $vars := include "misp.connectionVars" . | fromJson }}
{{- $env := list }}
{{- range $var, $ref := include "misp.connectionRefs" (first .) | fromJson }}
{{- if has (get $vars $var) (rest $) }}
{{- $env = append $env (dict "name" $var "valueFrom" (dict "secretKeyRef" $ref)) }}
{{- end }}
{{- end }}
{{- with $env }}{{ toYaml . }}{{ end }}
{{- end }}

{{/*
The secretKeyRef of one secret env var: its connectionSecrets entry, else the key of the same
name in misp-db (DB_USER, DB_PASSWORD) or misp-app (MISP_REDIS_PASSWORD). Takes (list root var).
*/}}
{{- define "misp.secretKeyRef" -}}
{{- $var := index . 1 }}
{{- $ref := get (include "misp.connectionRefs" (index . 0) | fromJson) $var }}
{{- toYaml (default (dict "name" (get (include "misp.connectionVars" . | fromJson) $var) "key" $var) $ref) }}
{{- end }}

{{/*
A CronJob of periodic work. Takes (dict root name task args schedule), deadline, and
module "housekeeping" for a housekeeping run. A task (app.kubernetes.io/component:
misp-task) renders app/Config and runs MISP's console in the pod; housekeeping
(misp-housekeeping) deletes old rows straight in the database. A network policy of
the release's owner can select on that label.
*/}}
{{- define "misp.taskCronJob" -}}
{{- $root := .root }}
{{- $module := default "task" .module }}
{{- $task := eq $module "task" }}
{{- $component := ternary "misp-task" "misp-housekeeping" $task }}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ .name }}
  labels:
    {{- include "misp.labels" (dict "root" $root "name" .name) | nindent 4 }}
    app.kubernetes.io/component: {{ $component }}
spec:
  schedule: {{ .schedule | quote }}
  {{- with $root.Values.env.TZ }}
  # The schedules run in the zone that PHP runs in
  timeZone: {{ . | quote }}
  {{- end }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      # A retry would mail the summaries twice, or repeat every server of a run
      # because one partner did not answer
      backoffLimit: {{ ternary 0 2 (has .task (list "periodic-summary" "pull-servers" "push-servers" "cache-servers")) }}
      activeDeadlineSeconds: {{ default 3600 .deadline }}
      template:
        metadata:
          labels:
            {{- include "misp.podLabels" (dict "root" $root "name" .name) | nindent 12 }}
            app.kubernetes.io/component: {{ $component }}
          {{- if $task }}
          annotations:
            {{- include "misp.checksums" $root | nindent 12 }}
          {{- end }}
        spec:
          restartPolicy: {{ ternary "Never" "OnFailure" $task }}
          {{- include "misp.podSecurityContext" $root | nindent 10 }}
          {{- include "misp.podScheduling" $root | nindent 10 }}
          containers:
            - name: {{ $module }}
              {{- include "misp.image" $root | nindent 14 }}
              args: ["python3", "-m", "misp_container.{{ $module }}", {{ .task | quote }}{{ range .args }}, {{ . | quote }}{{ end }}]
              {{- if $task }}
              {{- include "misp.appEnv" (list $root "misp-db" "misp-app") | nindent 14 }}
              volumeMounts:
                {{- include "misp.appVolumeMounts" $root | nindent 16 }}
                - name: attachments
                  mountPath: /var/www/MISP/app/attachments
              resources:
                {{- toYaml $root.Values.cronjobs.resources | nindent 16 }}
              {{- else }}
              {{- include "misp.appEnv" (list $root "misp-db") | nindent 14 }}
              {{- with include "misp.dbCAVolumeMount" $root }}
              volumeMounts:
                {{- . | nindent 16 }}
              {{- end }}
              resources:
                {{- toYaml $root.Values.housekeeping.resources | nindent 16 }}
              {{- end }}
              {{- include "misp.containerSecurityContext" $root | nindent 14 }}
          {{- if $task }}
          volumes:
            {{- include "misp.appVolumes" $root | nindent 12 }}
            {{- include "misp.attachmentsVolume" $root | nindent 12 }}
          {{- else if $root.Values.databaseCA }}
          volumes:
            {{- include "misp.dbCAVolume" $root | nindent 12 }}
          {{- end }}
{{- end }}
