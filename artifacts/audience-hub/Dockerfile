FROM node:20.20.2-bookworm-slim AS web
WORKDIR /web
# Match the standalone installation check; don't inherit npm from a cached image.
RUN npm install --global npm@10.9.9 --no-audit --no-fund \
  && node --version && npm --version
COPY package.json package-lock.json ./
# The builder needs devDependencies even when npm is configured for production.
RUN npm ci --include=dev --bin-links=true --no-audit --no-fund --cache=/tmp/npm-cache \
  || { cat /tmp/npm-cache/_logs/*-debug-0.log; exit 1; }
RUN test "$(npm --version)" = "10.9.9" \
  && test -x node_modules/.bin/vite
COPY index.html vite.config.ts tsconfig.json ./
COPY src/ ./src/
COPY public/ ./public/
RUN PORT=5000 BASE_PATH=/ npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 tini curl \
  && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY backend/pyproject.toml ./backend/
COPY backend/app/__init__.py ./backend/app/
# Install pinned dependencies in a cacheable layer.
RUN pip install --upgrade pip && pip install ./backend
COPY backend/ ./backend/
# Replace the minimal package with the complete app and console entry point.
RUN pip install --no-deps ./backend
COPY --from=web /web/dist/public ./static
COPY scripts/ ./scripts/
RUN chmod +x scripts/*.sh && mkdir -p /data/uploads /data/exports \
  && chown -R app:app /data /app
WORKDIR /app/backend
USER app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/scripts/start-api.sh"]