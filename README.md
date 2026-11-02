# OpsVibeClone

Self-hosted VPS control panel extracted from the OpsVibes application image and
published as `ghcr.io/technoidlover/opsvibeclone`.

## Published image

The installer currently uses the immutable image tag:

```text
ghcr.io/technoidlover/opsvibeclone:120c99e
```

To pull it directly:

```sh
docker pull ghcr.io/technoidlover/opsvibeclone:120c99e
```

If GitHub Container Registry requires authentication, use a GitHub token with
`read:packages`:

```sh
echo "$GHCR_TOKEN" | docker login ghcr.io -u technoidlover --password-stdin
```

## Install

On a clean Ubuntu or Debian server:

```sh
curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo sh
```

The installer installs Docker when needed, creates the `opsvibes` network,
preserves `/etc/opsvibes`, and starts the `opsvibes` container on port 3000.

For a private package:

```sh
export GHCR_USERNAME=technoidlover
export GHCR_TOKEN='YOUR_READ_PACKAGES_TOKEN'
curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo -E sh
```

Useful overrides:

```sh
OPSVIBES_BIND=127.0.0.1:3000 \
OPSVIBES_PUBLIC_IP=203.0.113.10 \
  curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo -E sh
```

## Source layout

- `backend/vibepanel`: Python/FastAPI backend.
- `web`: compiled frontend assets.
- `Dockerfile`: reproducible application image.
- `install.sh`: Docker-based install/update script.
- `docs/deployment.md`: deployment and troubleshooting notes.

## Data and security

Runtime data is kept in `/etc/opsvibes`. It includes the SQLite database and
the instance encryption key, so do not commit or publish that directory.
The installer does not include setup tokens, passwords, databases, or secrets.

## Development

Build locally with Docker:

```sh
docker build -t opsvibeclone:local .
```

Run a health check:

```sh
docker run --rm -p 3000:3000 opsvibeclone:local
curl http://127.0.0.1:3000/api/health
```
