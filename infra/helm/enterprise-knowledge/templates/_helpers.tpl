{{- define "ekb.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ekb.fullname" -}}
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

{{- define "ekb.labels" -}}
app.kubernetes.io/name: {{ include "ekb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ekb.postgresUrl" -}}
{{- if .Values.postgres.externalUrl -}}
{{- .Values.postgres.externalUrl -}}
{{- else -}}
postgresql+asyncpg://ekb:{{ .Values.secrets.POSTGRES_PASSWORD }}@{{ include "ekb.fullname" . }}-postgres:5432/ekb
{{- end -}}
{{- end -}}

{{- define "ekb.brokerUrl" -}}
amqp://ekb:{{ .Values.secrets.RABBITMQ_PASSWORD }}@{{ include "ekb.fullname" . }}-rabbitmq:5672//
{{- end -}}
