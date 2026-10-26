{{/*
The env value: the misp-env ConfigMap, as JSON. With databaseCA, DB_SSLROOTCERT names the
certificate's mount unless env sets it.
*/}}
{{- define "misp.envValues" -}}
{{- $env := dict }}
{{- if .Values.databaseCA }}
{{- $_ := set $env "DB_SSLROOTCERT" "/etc/misp-docker/db-ca/ca.crt" }}
{{- end }}
{{- range $key, $value := .Values.env }}
{{- if not (kindIs "invalid" $value) }}
{{- $_ := set $env $key (toString $value) }}
{{- end }}
{{- end }}
{{- toJson $env }}
{{- end }}

{{/*
The env var of each Secret key a setting can take, as {var: {name, key}}: the keys of
misp-db and misp-app, a connection Secret's where connectionSecrets maps one.
*/}}
{{- define "misp.secretVars" -}}
{{- $root := . }}
{{- $refs := include "misp.connectionRefs" . | fromJson }}
{{- $out := dict }}
{{- range $name, $keys := dict "misp-db" .Values.secrets.db "misp-app" .Values.secrets.app }}
{{- range $var, $value := $keys }}
{{- if not (kindIs "invalid" $value) }}
{{- $_ := set $out $var (get $refs $var | default (dict "name" $name "key" $var)) }}
{{- end }}
{{- end }}
{{- end }}
{{- toJson $out }}
{{- end }}

{{/* A setting's value as the string of its env var. Takes (list value default). */}}
{{- define "misp.settingValue" -}}
{{- $value := index . 0 }}
{{- $default := index . 1 }}
{{- if and (kindIs "string" $value) (kindIs "slice" $default) (not (hasPrefix "[" (trim $value))) }}
{{- /* A list setting takes a comma-separated list as well as JSON */}}
{{- $items := list }}
{{- range splitList "," $value }}
{{- with trim . }}
{{- $items = append $items . }}
{{- end }}
{{- end }}
{{- toJson $items }}
{{- else if kindIs "string" $value }}
{{- $value }}
{{- else if or (kindIs "slice" $value) (kindIs "map" $value) }}
{{- toJson $value }}
{{- else if kindIs "bool" $value }}
{{- ternary "true" "false" $value }}
{{- else if kindIs "float64" $value }}
{{- if eq $value (floor $value) }}{{ int64 $value }}{{ else }}{{ $value }}{{ end }}
{{- else }}
{{- toString $value }}
{{- end }}
{{- end }}

{{/*
The settings the repository brings: files/settings.yaml, with the env value
on top, as the env vars MISP and init.py read. JSON {var: {"value": string}} or
{var: {"secretKeyRef": {name, key}}}, plus MISP_REPOSITORY_SETTINGS, the comma-separated
names of the settings that MISP takes from its environment layer.
MISP_SETTING_<Category>__<key> is that layer, above config.php and the database: MISP
refuses a change to such a setting. MISP_CONFIG_<Category>__<key> (MISP_CONFIG_<name>
without a category) is what init.py renders into config.php, which MISP reads before the
layer: minimum_config, db_enable, s3 while PLUGIN_S3_BUCKET_NAME is set, and the auth
plugin groups while their switch is true.
*/}}
{{- define "misp.settings" -}}
{{- $file := .Files.Get "files/settings.yaml" | fromYaml }}
{{- $env := include "misp.envValues" . | fromJson }}
{{- $secret := include "misp.secretVars" . | fromJson }}
{{- /* MISP keeps attachments in S3 only while attachments_dir starts with s3 */}}
{{- if get $env "PLUGIN_S3_BUCKET_NAME" }}
{{- $_ := set $env "MISP_ATTACHMENTS_DIR" "s3://" }}
{{- end }}
{{- range $file.derived }}
{{- $from := .from }}
{{- range .to }}
{{- if not (or (get $env .) (hasKey $secret .)) }}
{{- if hasKey $secret $from }}
{{- $_ := set $secret . (get $secret $from) }}
{{- else if get $env $from }}
{{- $_ := set $env . (get $env $from) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- range $alias, $target := $file.aliases }}
{{- if not (or (get $env $target) (hasKey $secret $target)) }}
{{- if hasKey $secret $alias }}
{{- $_ := set $secret $target (get $secret $alias) }}
{{- else if get $env $alias }}
{{- $_ := set $env $target (get $env $alias) }}
{{- end }}
{{- end }}
{{- end }}
{{- $plugins := dict "oidc" "OIDC_ENABLE" "ldap" "LDAPAUTH_ENABLE" }}
{{- $configPhp := list "minimum_config" "db_enable" "s3" }}
{{- $out := dict }}
{{- $repository := list }}
{{- $auth := dict }}
{{- range $group, $settings := $file.settings }}
{{- $plugin := hasKey $plugins $group }}
{{- $on := true }}
{{- if eq $group "s3" }}
{{- $on = not (empty (get $env "PLUGIN_S3_BUCKET_NAME")) }}
{{- else if $plugin }}
{{- $on = eq (lower (toString (get $env (get $plugins $group)))) "true" }}
{{- end }}
{{- if $on }}
{{- range $name, $spec := $settings }}
{{- $var := $name | replace "." "_" | upper }}
{{- $entry := dict }}
{{- if hasKey $secret $var }}
{{- $entry = dict "secretKeyRef" (get $secret $var) }}
{{- else if ne (toString (get $env $var)) "" }}
{{- $entry = dict "value" (include "misp.settingValue" (list (get $env $var) $spec.value)) }}
{{- else if not (and $spec.blank_protection (empty $spec.value)) }}
{{- $entry = dict "value" (include "misp.settingValue" (list $spec.value $spec.value)) }}
{{- end }}
{{- if eq $name "Security.auth" }}
{{- /* Each enabled auth plugin adds its own; the list is rendered once below */}}
{{- $_ := set $auth $group ($entry.value | fromJsonArray) }}
{{- else if $entry }}
{{- $suffix := ternary (replace "." "__" $name) $name (contains "." $name) }}
{{- if or $plugin (has $group $configPhp) (not (contains "." $name)) }}
{{- $_ := set $out (printf "MISP_CONFIG_%s" $suffix) $entry }}
{{- end }}
{{- if and (not $plugin) (contains "." $name) }}
{{- $_ := set $out (printf "MISP_SETTING_%s" $suffix) $entry }}
{{- $repository = append $repository $name }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- /* MISP tries the auth plugins in this order */}}
{{- $plugins := list }}
{{- range list "oidc" "ldap" }}
{{- $plugins = concat $plugins (get $auth . | default list) }}
{{- end }}
{{- with $plugins }}
{{- $_ := set $out "MISP_CONFIG_Security__auth" (dict "value" (uniq . | toJson)) }}
{{- end }}
{{- $_ := set $out "MISP_REPOSITORY_SETTINGS" (dict "value" (sortAlpha $repository | join ",")) }}
{{- toJson $out }}
{{- end }}
