# Multi-stage Dockerfile for petrosa-dashboard (SPA)
# Stage 1: build static assets with Node
FROM node:20-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY tsconfig.json tsconfig.node.json vite.config.ts ./
COPY tailwind.config.js postcss.config.js .eslintrc.cjs ./
COPY index.html ./
COPY src ./src
COPY public ./public

RUN npm run build

# Stage 2: serve via nginx on 8080
FROM nginx:1.27-alpine

RUN apk add --no-cache curl \
  && rm -rf /usr/share/nginx/html/*

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Non-root: nginx:1.27-alpine's default user is `nginx`. Bind to 8080
# rather than 80 so we don't need CAP_NET_BIND_SERVICE.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
