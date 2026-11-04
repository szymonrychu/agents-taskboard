{{- define "agents-taskboard.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-taskboard.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "agents-taskboard.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-taskboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agents-taskboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agents-taskboard.labels" -}}
helm.sh/chart: {{ include "agents-taskboard.chart" . }}
{{ include "agents-taskboard.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Call with (dict "context" . "component" "api"). */}}
{{- define "agents-taskboard.componentLabels" -}}
{{ include "agents-taskboard.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "agents-taskboard.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agents-taskboard.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{/* imagePullSecrets from values plus global, deduplicated by name. */}}
{{- define "agents-taskboard.imagePullSecrets" -}}
{{- $g := ((.Values.global | default dict).imagePullSecrets) | default list }}
{{- $seen := dict }}
{{- $all := list }}
{{- range concat (.Values.imagePullSecrets | default list) $g }}
{{- if not (hasKey $seen .name) }}
{{- $_ := set $seen .name true }}
{{- $all = append $all . }}
{{- end }}
{{- end }}
{{- if $all }}
imagePullSecrets:
  {{- toYaml $all | nindent 2 }}
{{- end }}
{{- end }}

{{/* One secretKeyRef block. Call with (dict "ctx" . "existing" name "key" key "own" ownKey "set" bool "what" label).
     Uses the existing Secret when named, else the chart Secret key `own`; fails when neither is set. */}}
{{- define "agents-taskboard.secretKeyRef" -}}
{{- if and .set .existing -}}
{{- fail (printf "agents-taskboard: set only one of the %s value and its existing Secret" .what) -}}
{{- else if .existing -}}
name: {{ .existing | quote }}
key: {{ .key | default .own | quote }}
{{- else if .set -}}
name: {{ include "agents-taskboard.fullname" .ctx | quote }}
key: {{ .own | quote }}
{{- else -}}
{{- fail (printf "agents-taskboard: the %s is required (set the value or an existing Secret)" .what) -}}
{{- end -}}
{{- end }}

{{- define "agents-taskboard.databaseRef" -}}
{{- $p := .Values.postgres -}}
{{- if and $p.url $p.existingSecret -}}
{{- fail "agents-taskboard: set only one of postgres.url and postgres.existingSecret" -}}
{{- end -}}
{{- if not (or $p.url $p.existingSecret) -}}
{{- fail "agents-taskboard: a Postgres connection is required: set postgres.url or postgres.existingSecret" -}}
{{- end -}}
{{- include "agents-taskboard.secretKeyRef" (dict "ctx" . "existing" $p.existingSecret "key" $p.existingSecretKey "own" "database-uri" "set" (ne ($p.url | default "") "") "what" "database connection") -}}
{{- end }}

{{- define "agents-taskboard.orchestratorRef" -}}
{{- $o := .Values.orchestrator -}}
{{- include "agents-taskboard.secretKeyRef" (dict "ctx" . "existing" $o.existingSecret "key" $o.existingSecretKey "own" "orchestrator-token" "set" (ne ($o.token | default "") "") "what" "orchestrator token") -}}
{{- end }}

{{- define "agents-taskboard.relayRef" -}}
{{- include "agents-taskboard.secretKeyRef" (dict "ctx" . "existing" .Values.relayExistingSecret "key" .Values.relayExistingSecretKey "own" "relay-token" "set" (ne (.Values.relayToken | default "") "") "what" "relay token") -}}
{{- end }}

{{/* "true" when the optional delegate component is wanted (a token value or an existing Secret). */}}
{{- define "agents-taskboard.delegateEnabled" -}}
{{- if or (ne (.Values.delegateTokens | default "") "") .Values.delegateExistingSecret -}}true{{- end -}}
{{- end }}

{{- define "agents-taskboard.delegateRef" -}}
{{- include "agents-taskboard.secretKeyRef" (dict "ctx" . "existing" .Values.delegateExistingSecret "key" .Values.delegateExistingSecretKey "own" "delegate-tokens" "set" (ne (.Values.delegateTokens | default "") "") "what" "delegate tokens") -}}
{{- end }}

{{/* Settings ConfigMap data shared by every component; no secret values. Fails on a bad env key. */}}
{{- define "agents-taskboard.config" -}}
TASKBOARD_PUBLIC_BASE_URL: {{ required "agents-taskboard: publicUrl is required (the link base the notices carry)" .Values.publicUrl | quote }}
TASKBOARD_ORCHESTRATOR_URL: {{ required "agents-taskboard: orchestrator.url is required" .Values.orchestrator.url | quote }}
TASKBOARD_HTTP_PORT: {{ .Values.components.web.port | int | toString | quote }}
TASKBOARD_SERVICE_PORT: {{ .Values.components.service.port | int | toString | quote }}
TASKBOARD_ALLOWED_CIDRS: {{ join "," .Values.allowedCidrs | quote }}
{{- if include "agents-taskboard.delegateEnabled" . }}
TASKBOARD_DELEGATE_PORT: {{ .Values.components.delegate.port | int | toString | quote }}
{{- end }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "TASKBOARD_" $k) }}
{{- fail (printf "agents-taskboard: env key %q must start with TASKBOARD_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/* Container env: the settings ConfigMap and the three secret values. Call with the root context. */}}
{{- define "agents-taskboard.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "agents-taskboard.fullname" . }}-settings
env:
  - name: TASKBOARD_DATABASE_URL
    valueFrom:
      secretKeyRef:
        {{- include "agents-taskboard.databaseRef" . | nindent 8 }}
  - name: TASKBOARD_ORCHESTRATOR_TOKEN
    valueFrom:
      secretKeyRef:
        {{- include "agents-taskboard.orchestratorRef" . | nindent 8 }}
  - name: TASKBOARD_RELAY_TOKEN
    valueFrom:
      secretKeyRef:
        {{- include "agents-taskboard.relayRef" . | nindent 8 }}
  {{- if include "agents-taskboard.delegateEnabled" . }}
  - name: TASKBOARD_DELEGATE_TOKENS
    valueFrom:
      secretKeyRef:
        {{- include "agents-taskboard.delegateRef" . | nindent 8 }}
  {{- end }}
{{- end }}
