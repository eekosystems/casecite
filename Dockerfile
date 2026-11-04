# Multi-stage Dockerfile for Coolify deployment
# Stage 1: Build frontend with Vite
# Stage 2: Serve both backend API and frontend from one container

# ---- Stage 1: Frontend Build ----
# Base images float on the minor line on purpose (node:24, python:3.11): a
# rebuild picks up OS security fixes, and Trivy gates the image in CI, so a
# digest pin that nobody bumps means a permanently red build job. For a fully
# reproducible build, replace the tags with `image@sha256:<digest>` (resolve
# with `docker buildx imagetools inspect <image>`); Dependabot's docker
# ecosystem (.github/dependabot.yml) will then propose digest bumps.
FROM node:24-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build && \
    test -f dist/index.html && \
    echo "✓ Vite build successful" || \
    (echo "✗ FATAL: Vite build failed - dist/index.html not found" && exit 1)

# ---- Stage 1b: Word add-in build ----
# The task pane is plain static files; built here and served at /addin/ on the
# app's own origin, so the add-in deploys with the stack. The manifest for a
# deployment is rendered by the backend (GET /api/v1/addin/manifest.xml).
FROM node:24-slim AS addin-build

WORKDIR /addin
COPY word-addin/package.json word-addin/package-lock.json* ./
RUN npm ci
COPY word-addin/ .
RUN ADDIN_BASE=/addin/ npm run build && \
    test -f dist/index.html && \
    echo "✓ Word add-in build successful" || \
    (echo "✗ FATAL: Word add-in build failed - dist/index.html not found" && exit 1)

# ---- Stage 2: Python Runtime ----
# Floating patch tag (see note above on the frontend stage).
FROM python:3.11-slim AS python-base
ENV PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Install nginx, system dependencies, and security updates (distro versions,
# not pinned — see the base-image note above)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    nginx \
    curl \
    gosu \
    libmagic1 \
    libffi8 \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for running the application
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Set working directory
WORKDIR /app

# Copy backend requirements and install (prefer lock file for reproducible builds)
COPY backend/requirements.txt backend/requirements.lock* ./

# Base-image tooling is not in the lock file but ships in the image, so Trivy
# scans it. Pin it to current releases (pip/setuptools/wheel/jaraco.context
# CVEs in 2026) instead of whatever python:3.11-slim happens to bundle.
RUN pip install --no-cache-dir --upgrade "pip==26.2.1" "setuptools==84.0.0" "wheel==0.48.0" "jaraco.context==6.1.2"

# Install Python dependencies (prefer lock file for reproducible builds)
RUN if [ -f requirements.lock ]; then \
      pip install --no-cache-dir -r requirements.lock; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

# pip and wheel are build-time tools: strip them from the runtime filesystem.
# Nothing installs packages at runtime, and pip's vendored copies of its own
# dependencies (pip/_vendor/vendor.txt) otherwise surface in image scans with
# no fix available short of a new pip release. setuptools stays: greenlet, pillow
# and uvloop declare it as a runtime requirement.
RUN python -m pip uninstall -y pip wheel

# tiktoken downloads its encoding table on first use. Fetch it at build time so
# the app starts and chunks documents with no outbound network access.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chmod -R a+rX /opt/tiktoken

# Copy backend code
COPY --chown=appuser:appuser backend/app ./app

# Copy Alembic config + migrations so `alembic upgrade head` can run at startup.
# Without these, schema changes never apply in production (create_all only adds
# whole tables, never columns).
COPY --chown=appuser:appuser backend/alembic.ini ./alembic.ini
COPY --chown=appuser:appuser backend/migrations ./migrations

# Copy built frontend from stage 1
COPY --chown=appuser:appuser --from=frontend-build /frontend/dist /var/www/html
# The Word add-in pane, served at /addin/ (nginx here, Caddy in the bundled
# stack), and the manifest template the backend renders per deployment.
COPY --chown=appuser:appuser --from=addin-build /addin/dist /var/www/html/addin
COPY --chown=appuser:appuser word-addin/manifest.xml /app/addin-manifest.xml

# Verify the built index.html references assets (not /src/main.jsx)
RUN grep -q '/assets/' /var/www/html/index.html && \
    echo "✓ Frontend dist verified" || \
    (echo "✗ FATAL: /var/www/html/index.html missing asset references" && cat /var/www/html/index.html && exit 1)

# Copy logo asset for favicon/branding
COPY --chown=appuser:appuser frontend/logo.webp /var/www/html/logo.webp

# Copy nginx config. Root-owned and read-only for everyone else: the root
# master process reads it, and appuser (the workers and the application) must
# not be able to rewrite what nginx serves or proxies to.
COPY nginx.unified.conf /etc/nginx/sites-enabled/default
RUN chown root:root /etc/nginx/sites-enabled/default \
    && chmod 0644 /etc/nginx/sites-enabled/default

# Create data directories with broad permissions (Coolify may run as non-root)
RUN mkdir -p /app/data/chroma /app/data/uploads /app/data/audit_logs /app/data/user_settings \
    && chmod -R 755 /app/data

# nginx workers run as appuser (the master is started by start.sh, see below)
RUN chown -R appuser:appuser /var/log/nginx /var/lib/nginx /run \
    && sed -i 's/user www-data;/user appuser;/' /etc/nginx/nginx.conf \
    && grep -q '^user appuser;' /etc/nginx/nginx.conf

# License terms and third-party notices travel with the image
COPY LICENSE NOTICE.md /app/

# Declare data volume
VOLUME ["/app/data"]

# Expose port 80 (Coolify expects this)
EXPOSE 80

# Health check — through nginx (:80), so a dead nginx OR a dead uvicorn marks
# the container unhealthy. The start period covers migrations and first boot.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost/health || exit 1

# Start script - runs both nginx and uvicorn
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Bake the git sha into the runtime env so /health can report which build is
# serving. CI and Coolify pass SOURCE_COMMIT as a build arg. Declared last so a
# new commit does not invalidate the dependency layers above.
ARG SOURCE_COMMIT=unknown
ENV SOURCE_COMMIT=$SOURCE_COMMIT

# No USER directive on purpose: start.sh must begin as root to chown the mounted
# /app/data volume (Docker/Coolify create named volumes root-owned) and to let
# nginx bind :80. It then drops privileges: nginx workers run as appuser (see
# the nginx.conf edit above) and uvicorn — the only process that touches client
# data — is started as appuser via gosu. start.sh refuses to start if the drop
# does not work. Alembic migrations run as appuser too: importing the models
# touches files under /app/data, which must stay appuser-owned.
CMD ["/start.sh"]
