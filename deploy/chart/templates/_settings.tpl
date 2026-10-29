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
{{- if kindIs "float64" $value }}
{{- /* toString prints 1000000 as 1e+06, which PHP refuses as a limit */}}
{{- $_ := set $env $key (include "misp.settingValue" (list $value $value)) }}
{{- else if not (kindIs "invalid" $value) }}
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
A config.php value as JSON, typed by the setting's default in settings.yaml: a string
default keeps the value a string, so a client secret 0071234 stays one. Takes (list value
default).
*/}}
{{- define "misp.configValue" -}}
{{- if kindIs "string" (index . 1) }}
{{- toJson (toString (index . 0)) }}
{{- else }}
{{- include "misp.settingValue" . }}
{{- end }}
{{- end }}

{{/*
The settings the repository brings: files/settings.yaml, with the env value
on top, as the env vars MISP and init.py read. JSON {var: {"value": string}} or
{var: {"secretKeyRef": {name, key}}}, plus MISP_REPOSITORY_SETTINGS, the comma-separated
names of the settings that MISP takes from its environment layer.
MISP_SETTING_<Category>__<key> (MISP_SETTING_<name> without a category) is that layer,
above config.php and the database: MISP refuses a change to such a setting.
MISP_CONFIG_<Category>__<key> is what init.py renders into config.php, which MISP reads
before the layer: the settings marked config_php, and the auth plugin groups while their
switch is true. Its values are JSON, except those MISP_PLAIN_CONFIG names: a Secret holds
them, as plain strings.
*/}}
{{- define "misp.settings" -}}
{{- $file := .Files.Get "files/settings.yaml" | fromYaml }}
{{- $env := include "misp.envValues" . | fromJson }}
{{- $secret := include "misp.secretVars" . | fromJson }}
{{- $refs := include "misp.connectionRefs" . | fromJson }}
{{- /* The key each env var takes its value from: its alias, or the var it derives from */}}
{{- $source := dict }}
{{- range $file.derived }}
{{- $from := .from }}
{{- range .to }}{{ $_ := set $source . $from }}{{ end }}
{{- end }}
{{- range $alias, $target := $file.aliases }}{{ $_ := set $source $target $alias }}{{ end }}
{{- $listed := list }}
{{- range $group, $settings := $file.settings }}
{{- range $name, $spec := $settings }}
{{- $var := $name | replace "." "_" | upper }}
{{- $listed = append $listed $name }}
{{- if $spec.secret }}
{{- range list $var (get $source $var) }}
{{- if and . (ne (toString (get $env .)) "") }}
{{- fail (printf "env.%s holds the secret %s: set it in secrets.app, which keeps it out of the ConfigMaps" . $name) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- range $var, $_ := $env }}
{{- if hasPrefix "MISP_SETTING_" $var }}
{{- $name := trimPrefix "MISP_SETTING_" $var | replace "__" "." }}
{{- if has $name $listed }}
{{- fail (printf "env.%s sets %s, which settings.yaml lists: use env.%s" $var $name ($name | replace "." "_" | upper)) }}
{{- end }}
{{- end }}
{{- end }}
{{- /* A trailing slash makes MISP rewrite its baseurl row on every request */}}
{{- with get $env "MISP_BASEURL" }}
{{- $_ := set $env "MISP_BASEURL" (trimSuffix "/" .) }}
{{- end }}
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
{{- /* PHP sees plain HTTP from caddy: behind TLS, MISP marks the session cookie Secure only with force_https */}}
{{- $baseurl := get $env "MISP_BASEURL" }}
{{- range $group, $settings := $file.settings }}
{{- with get $settings "MISP.baseurl" }}{{ $baseurl = $baseurl | default .value }}{{ end }}
{{- end }}
{{- if and (hasPrefix "https://" (toString $baseurl)) (not (hasKey $env "SECURITY_FORCE_HTTPS")) }}
{{- $_ := set $env "SECURITY_FORCE_HTTPS" "true" }}
{{- end }}
{{- $plugins := dict "oidc" "OIDC_ENABLE" "ldap" "LDAPAUTH_ENABLE" }}
{{- $out := dict }}
{{- $repository := list }}
{{- $plain := list }}
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
{{- $carried := true }}
{{- if hasKey $secret $var }}
{{- $entry = dict "secretKeyRef" (get $secret $var) }}
{{- else if $spec.secret }}
{{- /* A Secret the release supplies may hold the key though the secrets value does not name it */}}
{{- $key := get $source $var | default $var }}
{{- $entry = dict "secretKeyRef" (get $refs $key | default (dict "name" "misp-app" "key" $key)) }}
{{- $carried = false }}
{{- else if hasKey $env $var }}
{{- if not (and $spec.blank_protection (eq (toString (get $env $var)) "")) }}
{{- $entry = dict "value" (get $env $var) }}
{{- end }}
{{- else if not (and $spec.blank_protection (empty $spec.value)) }}
{{- $entry = dict "value" $spec.value }}
{{- end }}
{{- if eq $name "Security.auth" }}
{{- /* Each enabled auth plugin adds its own; the list is rendered once below */}}
{{- $_ := set $auth $group (include "misp.settingValue" (list $entry.value $spec.value) | fromJsonArray) }}
{{- else if $entry }}
{{- $suffix := replace "." "__" $name }}
{{- if or $plugin $spec.config_php }}
{{- $config := printf "MISP_CONFIG_%s" $suffix }}
{{- if hasKey $entry "value" }}
{{- $_ := set $out $config (dict "value" (include "misp.configValue" (list $entry.value $spec.value))) }}
{{- else }}
{{- $_ := set $out $config $entry }}
{{- $plain = append $plain $config }}
{{- end }}
{{- end }}
{{- if not $plugin }}
{{- $setting := printf "MISP_SETTING_%s" $suffix }}
{{- if hasKey $entry "value" }}
{{- $_ := set $out $setting (dict "value" (include "misp.settingValue" (list $entry.value $spec.value))) }}
{{- else }}
{{- $_ := set $out $setting $entry }}
{{- end }}
{{- if $carried }}
{{- $repository = append $repository $name }}
{{- end }}
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
{{- $_ := set $out "MISP_PLAIN_CONFIG" (dict "value" (sortAlpha $plain | join ",")) }}
{{- toJson $out }}
{{- end }}
