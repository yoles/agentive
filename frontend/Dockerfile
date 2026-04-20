# Frontend Dockerfile — multi-stage (dev + prod)
# Base : node:24-alpine3.21 (Node 24 LTS pinned)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 1 — Base (dependencies)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM node:24-alpine3.21 AS base

WORKDIR /app

# Copy package files first (for Docker layer caching)
COPY package.json package-lock.json* ./

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 2 — Dev (Vite dev server, HMR, all deps)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM base AS dev

# --legacy-peer-deps : openapi-typescript pins TS ^5, Vite 8 ships TS 6.
# Acceptable runtime compatibility, peer-dep warning tolerated.
RUN npm install --legacy-peer-deps

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 3 — Build
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM base AS build

RUN npm ci --legacy-peer-deps

COPY . .

# Production build — explicit env for Vite (client sees no token, same-origin API).
ENV NODE_ENV=production
RUN npm run build

# Drop sourcemaps from the artefact — they leak original source if accidentally
# served. Story 1.9 can wire them into Sentry via `sourcemap: 'hidden'` instead.
RUN find dist -name "*.map" -type f -delete

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 4 — Prod (Caddy serves static files with security headers)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM caddy:2-alpine AS prod

COPY --from=build /app/dist /usr/share/caddy

# Hardened Caddy config — security headers match the edge Caddyfile but also
# apply if this image is ever served directly (defense-in-depth).
RUN printf ':80 {\n\
    root * /usr/share/caddy\n\
    try_files {path} /index.html\n\
    file_server\n\
    header {\n\
        Strict-Transport-Security "max-age=31536000; includeSubDomains"\n\
        X-Content-Type-Options "nosniff"\n\
        X-Frame-Options "DENY"\n\
        Referrer-Policy "strict-origin-when-cross-origin"\n\
        Permissions-Policy "geolocation=(), microphone=(), camera=()"\n\
        -Server\n\
    }\n\
    @hashed path *.[0-9a-f]*.js *.[0-9a-f]*.css\n\
    header @hashed Cache-Control "public, max-age=31536000, immutable"\n\
    @html path /\n\
    header @html Cache-Control "no-store"\n\
}\n' > /etc/caddy/Caddyfile

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://localhost/ > /dev/null || exit 1
