# syntax=docker/dockerfile:1

# --- Build stage ------------------------------------------------------------
# Static output: build natively on the runner platform (no QEMU for arm64),
# then copy dist/ into the target-platform nginx image.
FROM --platform=$BUILDPLATFORM node:22-alpine AS builder

WORKDIR /app

# Reproducible install from the lockfile (Linux binaries for esbuild/rollup).
# package-lock.json must stay in sync with package.json.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Copy source code (node_modules/dist excluded by .dockerignore)
COPY . .

# Build the application
RUN npm run build


# --- Production stage: nginx running as a non-root user (port 8080) ---------
FROM nginxinc/nginx-unprivileged:1.31-alpine

# Copy built files
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Periodic reload when a TLS certificate is mounted (Helm tls.enabled); no-op
# without certificate (docker-compose)
COPY --chmod=0755 docker-entrypoint.d/90-lfw-tls-reload.sh /docker-entrypoint.d/

# 8443 (TLS) is only served when the Helm chart mounts its server snippet
EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
