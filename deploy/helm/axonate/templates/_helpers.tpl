{{- define "axonate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "axonate.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "axonate.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "axonate.labels" -}}
app.kubernetes.io/name: {{ include "axonate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "axonate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "axonate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "axonate.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else -}}
{{- include "axonate.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "axonate.pgHost" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "%s-postgresql" .Release.Name -}}
{{- else -}}
{{- required "secret.postgres.host is required when postgresql.enabled=false" .Values.secret.postgres.host -}}
{{- end -}}
{{- end -}}

{{- define "axonate.databaseUrl" -}}
{{- $host := include "axonate.pgHost" . -}}
{{- printf "postgresql://%s:%s@%s:5432/%s" .Values.secret.postgres.user .Values.secret.postgres.password $host .Values.secret.postgres.database -}}
{{- end -}}
