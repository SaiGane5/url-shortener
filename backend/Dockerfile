# syntax=docker/dockerfile:1.7
###############################################################################
# Stage 1 -- build the virtualenv.
#
# Dependencies are installed from the pinned lock file in a stage that keeps the
# compilers, then only the resulting venv is copied forward. The runtime image
# therefore contains no build toolchain, which is both smaller and a smaller
# attack surface.
###############################################################################
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Only the lock file is copied first, so `docker build` reuses the (slow)
# dependency layer whenever application code changes but dependencies do not.
COPY requirements.lock.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock.txt

###############################################################################
# Stage 2 -- runtime.
###############################################################################
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app

# Unprivileged, fixed uid/gid. A fixed uid keeps bind-mounted volumes
# predictable across hosts, and nothing in the image is owned by the app user,
# so the running container cannot modify its own code.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR ${APP_HOME}

COPY --from=builder /opt/venv /opt/venv
COPY --chown=root:root alembic.ini pyproject.toml ./
COPY --chown=root:root alembic ./alembic
COPY --chown=root:root app ./app

USER 10001:10001

EXPOSE 8000

# Uses the Python already in the image rather than adding curl/wget to the
# runtime layer just for a probe.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", \
         "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"]

# Exec form => uvicorn is PID 1 and receives SIGTERM directly (no shell to
# swallow it), so `--timeout-graceful-shutdown` actually governs shutdown:
# stop accepting, drain in-flight requests, then run the lifespan shutdown
# (which flushes buffered clicks) before exiting.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log", \
     "--timeout-graceful-shutdown", "10", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
