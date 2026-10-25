# syntax=docker/dockerfile:1

# --- Build stage ------------------------------------------------------------
FROM node:22-alpine AS builder

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

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
