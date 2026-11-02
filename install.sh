#!/bin/sh
set -eu

# Install or update OpsVibeClone from GitHub Container Registry.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo sh
#
# Optional environment variables:
#   OPSVIBES_IMAGE       Image reference (default is the published immutable tag).
#   OPSVIBES_BIND        Host bind, for example 127.0.0.1:3000.
#   OPSVIBES_PUBLIC_IP   Public IP used by the panel.
#   GHCR_USERNAME        GHCR username when the package is private.
#   GHCR_TOKEN           GHCR token with read:packages when the package is private.

IMAGE="${OPSVIBES_IMAGE:-ghcr.io/technoidlover/opsvibeclone:120c99e}"
NAME=opsvibes
NETWORK=opsvibes
DATA_DIR=/etc/opsvibes
BIND="${OPSVIBES_BIND:-0.0.0.0:3000}"

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

has() {
    command -v "$1" >/dev/null 2>&1
}

[ "$(id -u)" = 0 ] || die "Run this installer as root or with sudo."
[ "$(uname -s)" = Linux ] || die "OpsVibeClone requires Linux."

case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) ;;
    *) die "Unsupported architecture: $(uname -m)" ;;
esac

if ! has docker; then
    has curl || die "curl is required to install Docker."
    curl -fsSL https://get.docker.com | sh
fi

if has systemctl; then
    systemctl enable --now docker >/dev/null 2>&1 || true
fi
docker info >/dev/null 2>&1 || die "Docker is not running."

# Login is only needed when the GHCR package is private. Credentials are read
# from environment variables and are not written to this repository.
if [ -n "${GHCR_TOKEN:-}" ]; then
    [ -n "${GHCR_USERNAME:-}" ] || die "GHCR_USERNAME is required with GHCR_TOKEN."
    printf '%s' "$GHCR_TOKEN" | docker login ghcr.io \
        --username "$GHCR_USERNAME" --password-stdin >/dev/null
fi

PUBLIC_IP="${OPSVIBES_PUBLIC_IP:-}"
if [ -z "$PUBLIC_IP" ] && has curl; then
    PUBLIC_IP=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
fi
[ -n "$PUBLIC_IP" ] || PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

docker pull "$IMAGE"
docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
mkdir -p "$DATA_DIR"

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
    --name "$NAME" \
    --network "$NETWORK" \
    --restart unless-stopped \
    -p "$BIND:3000" \
    -v "$DATA_DIR:/etc/opsvibes" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /proc:/host/proc:ro \
    -e OPSVIBES_HOST_PROC=/host/proc \
    -e OPSVIBES_PUBLIC_IP="$PUBLIC_IP" \
    --log-opt max-size=10m \
    --log-opt max-file=3 \
    "$IMAGE" >/dev/null

printf 'OpsVibeClone is running at http://%s\n' "$BIND"
printf 'Data directory: %s\n' "$DATA_DIR"
