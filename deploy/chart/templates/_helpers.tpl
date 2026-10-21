{{/* The tag of the three images: image.tag, else the chart's appVersion */}}
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

{{/* Scheduling and pull secrets of every pod */}}
{{- define "misp.podScheduling" -}}
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
worker and the console tasks. Each pod renders its own app/Config.
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
{{- end }}

{{/* LOG_FORMAT and TZ from misp-env, for a pod that does not read the whole ConfigMap */}}
{{- define "misp.logEnv" -}}
- name: LOG_FORMAT
  valueFrom:
    configMapKeyRef:
      name: misp-env
      key: LOG_FORMAT
      optional: true
- name: TZ
  valueFrom:
    configMapKeyRef:
      name: misp-env
      key: TZ
      optional: true
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

{{/* Checksums of misp-env, misp-db and misp-app, so a change rolls the pods that read them */}}
{{- define "misp.checksums" -}}
checksum/env: {{ include (print .Template.BasePath "/configmap-env.yaml") . | sha256sum }}
{{- if .Values.secrets.create }}
checksum/secrets: {{ printf "%s%s%s%s" (.Files.Get "files/secrets-db.env") (toJson .Values.secrets.db) (.Files.Get "files/secrets-app.env") (toJson .Values.secrets.app) | sha256sum }}
{{- end }}
{{- end }}

{{/*
DB_USER and DB_PASSWORD from the Secrets that database.user and database.password name, as
env entries of a container that reads misp-db through envFrom: env wins over envFrom.
*/}}
{{- define "misp.dbEnv" -}}
{{- range $var, $ref := dict "DB_USER" .Values.database.user "DB_PASSWORD" .Values.database.password }}
{{- if $ref }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef:
      name: {{ $ref.name }}
      key: {{ $ref.key }}
{{- end }}
{{- end }}
{{- end }}

{{/*
The secretKeyRef of the database user or password: database.user or database.password when
set, else the key of misp-db. Takes (list root "user"|"password").
*/}}
{{- define "misp.dbSecretKeyRef" -}}
{{- $root := index . 0 }}
{{- $ref := index $root.Values.database (index . 1) }}
{{- if $ref }}
name: {{ $ref.name }}
key: {{ $ref.key }}
{{- else }}
name: misp-db
key: {{ ternary "DB_USER" "DB_PASSWORD" (eq (index . 1) "user") }}
{{- end }}
{{- end }}

{{/*
Entries of an env file in files/: KEY=VALUE lines, comments and blank lines
skipped, as a dict. Takes (list root path).
*/}}
{{- define "misp.envFile" -}}
{{- $root := index . 0 }}
{{- $out := dict }}
{{- range $line := $root.Files.Lines (index . 1) }}
{{- $line = trim $line }}
{{- if and $line (not (hasPrefix "#" $line)) (contains "=" $line) }}
{{- $kv := regexSplit "=" $line 2 }}
{{- $_ := set $out (index $kv 0) (index $kv 1) }}
{{- end }}
{{- end }}
{{- toJson $out }}
{{- end }}

{{/*
The console tasks: they run MISP's console in the pod instead of calling the
API. tests/test_chart.py checks this list against misp_container.task.CAKE_TASKS.
*/}}
{{- define "misp.consoleTasks" -}}
{{- list "periodic-summary" "check-user-validity" "block-invalid-users" | toJson }}
{{- end }}

{{/*
A CronJob of the task runner. Takes (dict root name task args schedule).
API tasks (app.kubernetes.io/component: misp-task) call the API with ADMIN_KEY;
console tasks (misp-console-task) render app/Config and run MISP's console.
The network policies select on that label.
*/}}
{{- define "misp.taskCronJob" -}}
{{- $root := .root }}
{{- $console := has .task (include "misp.consoleTasks" $root | fromJsonArray) }}
{{- $component := ternary "misp-console-task" "misp-task" $console }}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ .name }}
  labels:
    {{- include "misp.labels" (dict "root" $root "name" .name) | nindent 4 }}
    app.kubernetes.io/component: {{ $component }}
spec:
  schedule: {{ .schedule | quote }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: {{ ternary 0 2 (eq .task "periodic-summary") }}
      activeDeadlineSeconds: {{ ternary 3600 1200 $console }}
      template:
        metadata:
          labels:
            app.kubernetes.io/name: {{ .name }}
            app.kubernetes.io/component: {{ $component }}
        spec:
          restartPolicy: {{ ternary "Never" "OnFailure" $console }}
          {{- include "misp.podSecurityContext" $root | nindent 10 }}
          {{- include "misp.podScheduling" $root | nindent 10 }}
          containers:
            - name: task
              {{- include "misp.image" $root | nindent 14 }}
              command: ["/usr/bin/tini", "--", "python3", "-m", "misp_container.task", {{ .task | quote }}{{ range .args }}, {{ . | quote }}{{ end }}]
              {{- if $console }}
              {{- with include "misp.dbEnv" $root }}
              env:
                {{- . | nindent 16 }}
              {{- end }}
              envFrom:
                - configMapRef:
                    name: misp-env
                - secretRef:
                    name: misp-db
                - secretRef:
                    name: misp-app
              volumeMounts:
                {{- include "misp.appVolumeMounts" $root | nindent 16 }}
              resources:
                {{- toYaml $root.Values.cronjobs.resources.console | nindent 16 }}
              {{- else }}
              env:
                # No misp-env for an API task: only what the runner reads, plus the
                # log format and time zone every other pod takes from it
                {{- include "misp.logEnv" $root | nindent 16 }}
                - name: SYNC_BASE_URL
                  value: http://web:8080
                - name: TASK_METRICS_URL
                  value: http://metrics:9191/metrics
                - name: TASK_MAX_QUEUED
                  value: {{ $root.Values.cronjobs.maxQueued | quote }}
                - name: ADMIN_KEY
                  valueFrom:
                    secretKeyRef:
                      name: misp-admin
                      key: ADMIN_KEY
                      optional: true
              resources:
                {{- toYaml $root.Values.cronjobs.resources.api | nindent 16 }}
              {{- end }}
              {{- include "misp.containerSecurityContext" $root | nindent 14 }}
          {{- if $console }}
          volumes:
            {{- include "misp.appVolumes" $root | nindent 12 }}
            {{- include "misp.attachmentsVolume" $root | nindent 12 }}
          {{- end }}
{{- end }}
