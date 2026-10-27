{{/*
Common labels. Call with (dict "ctx" $ "component" "backend").
*/}}
{{- define "lfw.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
app.kubernetes.io/part-of: lidoire-flood-watch
{{ include "lfw.selectorLabels" . }}
{{- end }}

{{/*
Selector labels (immutable on Deployments/StatefulSets: keep them stable).
*/}}
{{- define "lfw.selectorLabels" -}}
app.kubernetes.io/name: lidoire-flood-watch
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Application image (backend/frontend). Call with (dict "ctx" $ "repository" "...").
*/}}
{{- define "lfw.appImage" -}}
{{- $tag := .ctx.Values.image.tag | default .ctx.Chart.AppVersion -}}
{{- printf "%s:%s" .repository $tag -}}
{{- end }}

{{- define "lfw.appImagePullPolicy" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.pullPolicy -}}
{{- .Values.image.pullPolicy -}}
{{- else if eq $tag "main" -}}
Always
{{- else -}}
IfNotPresent
{{- end -}}
{{- end }}

{{/*
Pod security context (PSS restricted). Call with a component securityContext
dict ({runAsUser, runAsGroup, fsGroup}).
*/}}
{{- define "lfw.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .runAsUser }}
runAsGroup: {{ .runAsGroup }}
{{- if .fsGroup }}
fsGroup: {{ .fsGroup }}
fsGroupChangePolicy: OnRootMismatch
{{- end }}
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
Container security context (PSS restricted, read-only root filesystem).
*/}}
{{- define "lfw.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
Scheduling and pull secrets shared by every pod.
*/}}
{{- define "lfw.podScheduling" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
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

{{/* Names of the Secrets synced by External Secrets Operator */}}
{{- define "lfw.generatedSecret" -}}lidoire-generated{{- end }}
{{- define "lfw.backendSecret" -}}lidoire-backend{{- end }}
{{- define "lfw.externalApisSecret" -}}lidoire-external-apis{{- end }}
{{- define "lfw.backupSecret" -}}lidoire-backup-s3{{- end }}

{{/*
nginx server-level snippet of the frontend when tls.enabled (ConfigMap
frontend-nginx-tls, mounted in /etc/nginx/server.d/). The single server block
of frontend/nginx.conf then answers on 8080 (plain HTTP, 301 to https) and on
8443 (TLS: dashboard and /api/ proxy, same headers and locations).
*/}}
{{- define "lfw.frontendTlsSnippet" -}}
listen 8443 ssl;
http2 on;

ssl_certificate     /etc/nginx/tls/tls.crt;
ssl_certificate_key /etc/nginx/tls/tls.key;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:lfw_tls:1m;
ssl_session_timeout 1d;
ssl_session_tickets off;

# Plain HTTP (8080) -> https, every path: the probes use 8443 (scheme HTTPS).
if ($scheme = http) {
    return 301 https://$host$request_uri;
}
{{- end }}
