# syntax=docker/dockerfile:1.7
###############################################################################
# Stage 1 -- build the static bundle.
#
# Vite inlines VITE_* variables at BUILD time, so the API URL has to be a build
# arg rather than a runtime env var. It defaults to empty, which means
# "same origin" -- the browser calls whatever host served the page, so this
# image is portable across environments with no baked-in hostnames.
###############################################################################
FROM node:20-alpine AS builder

WORKDIR /build

ENV CI=true

# Lockfile first, so the dependency layer is cached independently of source.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig*.json vite.config.ts index.html ./
COPY src ./src

ARG VITE_API_BASE_URL=""
ARG VITE_SHORT_LINK_BASE_URL=""
ARG VITE_API_DOCS_URL="/docs"
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_SHORT_LINK_BASE_URL=${VITE_SHORT_LINK_BASE_URL} \
    VITE_API_DOCS_URL=${VITE_API_DOCS_URL}

# `tsc -b && vite build` -- a type error fails the image build rather than
# shipping a bundle that only breaks at runtime.
RUN npm run build

###############################################################################
# Stage 2 -- serve.
#
# nginx-unprivileged rather than the standard nginx image: it runs as uid 101
# with no root master process and listens on 8080, so no capability is needed
# to bind the port. The final image contains only static assets and nginx --
# no Node.js, no node_modules, no source.
###############################################################################
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

USER 101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
