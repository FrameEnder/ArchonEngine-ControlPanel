# ArchonEngine ControlPanel — a browser front end for a self-hosted ArchonEngine (Kawari) FFXIV server.
#
# Runs as a non-root user. Mount the Kawari config directory read-write (it holds
# the databases, config.yaml and backups) and, optionally, the rootless Podman
# API socket so the dashboard can start and stop the server container.
FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAWARI_DATA_DIR=/data \
    KAWARI_WEB_DIR=/data/web \
    KAWARI_HOST_DATA_DIR=/data \
    KAWARI_WEB_HOST=0.0.0.0 \
    KAWARI_WEB_PORT=7301

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY kawari_core/ /app/kawari_core/
COPY kawari_web/ /app/kawari_web/

# The image runs as a non-root user whose UID matches the host user at build
# time. Run the container with --userns=keep-id so that UID means the same thing
# on both sides; otherwise rootless Podman maps it to a subuid and the mounted
# data directory is not writable. run.sh and the quadlet unit both do this.
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} kawari 2>/dev/null || true && \
    useradd -u ${UID} -g ${GID} -m -s /usr/sbin/nologin kawari 2>/dev/null || true
USER ${UID}:${GID}

EXPOSE 7301
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7301/connect',timeout=4)" || exit 1

CMD ["python", "-m", "kawari_web"]
