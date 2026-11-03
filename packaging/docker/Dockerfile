# Callido Restore for a NAS (Synology Container Manager, or any Docker host): the server in network mode, opened from a browser.
# Build from the repository root:  docker build -f packaging/docker/Dockerfile -t callido-restore .
# Run: see docs/NAS.md (a password is required; /config keeps the settings, the NAS volumes are mounted for restores).

# --- rclone: the same pinned release as the desktop app, its SHA256SUMS checked against rclone's signing key in the repo
FROM debian:bookworm-slim AS rclone
ARG RCLONE_VERSION=v1.75.2
ARG RCLONE_SIGNING_FPR=FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA
ARG TARGETARCH
ARG TARGETVARIANT
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg unzip && rm -rf /var/lib/apt/lists/*
COPY packaging/rclone-release-signing-key.asc /tmp/rclone.asc
RUN set -eu; \
    case "${TARGETARCH}${TARGETVARIANT}" in amd64) a=amd64 ;; arm64) a=arm64 ;; armv7) a=arm-v7 ;; *) echo "unsupported ${TARGETARCH}${TARGETVARIANT}"; exit 1 ;; esac; \
    z="rclone-${RCLONE_VERSION}-linux-${a}.zip"; cd /tmp; \
    curl -fsSLO "https://downloads.rclone.org/${RCLONE_VERSION}/${z}"; curl -fsSLO "https://downloads.rclone.org/${RCLONE_VERSION}/SHA256SUMS"; \
    export GNUPGHOME=/tmp/gpg; mkdir -m 700 "$GNUPGHOME"; gpg --batch --import /tmp/rclone.asc; \
    gpg --batch --status-fd 1 --verify SHA256SUMS > status.txt; grep -q "VALIDSIG ${RCLONE_SIGNING_FPR}" status.txt; \
    gpg --batch --output sums.txt --decrypt SHA256SUMS; grep " ${z}\$" sums.txt | sha256sum -c -; \
    unzip -q "$z"; install -m 755 "rclone-${RCLONE_VERSION}-linux-${a}/rclone" /usr/local/bin/rclone; \
    install -D -m 644 "rclone-${RCLONE_VERSION}-linux-${a}/README.txt" /usr/local/share/rclone/README.txt; \
    /usr/local/bin/rclone version | head -1

# --- the app: Python's standard library plus lz4 (no window on a NAS, so no pywebview)
FROM python:3.12-slim-bookworm
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
RUN pip install --no-cache-dir "lz4>=4.0"
WORKDIR /app
COPY callido_restore /app/callido_restore
COPY static /app/static
COPY LICENSE /app/LICENSE
# /config holds settings, connections (rclone.conf), saved restores and the download cache; HOME points there so both the app
# (~/.hbk-explorer) and rclone (~/.config/rclone) keep everything in that one volume.
ENV HOME=/config CALLIDO_LISTEN=0.0.0.0 CALLIDO_PORT=8765 PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 3)"]
ENTRYPOINT ["python", "-m", "callido_restore"]
