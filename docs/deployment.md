# Deployment notes

## GHCR package

The published package is:

```text
ghcr.io/technoidlover/opsvibeclone:120c99e
```

The tag is pinned to the source commit used for the initial publication. Use
an explicit tag in production instead of `latest` so an update is intentional.

## Install or update

Run the installer as root:

```sh
curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo sh
```

The script:

1. Verifies Linux and a supported CPU architecture.
2. Installs and starts Docker when Docker is missing.
3. Optionally logs in to GHCR using `GHCR_USERNAME` and `GHCR_TOKEN`.
4. Pulls the configured image.
5. Creates the `opsvibes` Docker network and `/etc/opsvibes`.
6. Recreates the `opsvibes` container while preserving the data directory.

For a private GHCR package, the token must have `read:packages`:

```sh
GHCR_USERNAME=technoidlover \
GHCR_TOKEN='YOUR_READ_PACKAGES_TOKEN' \
  curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo -E sh
```

## Container configuration

The container exposes port 3000 and mounts:

- `/etc/opsvibes` for persistent application data.
- `/var/run/docker.sock` for Docker management features.
- `/proc` as read-only at `/host/proc` for host metrics.

The default host binding is `0.0.0.0:3000`. To put the panel behind a reverse
proxy, bind it locally:

```sh
OPSVIBES_BIND=127.0.0.1:3000 \
  curl -fsSL https://raw.githubusercontent.com/technoidlover/OpsVibeClone/main/install.sh | sudo -E sh
```

## Validation

```sh
docker ps --filter name=opsvibes
docker inspect --format '{{.State.Health.Status}}' opsvibes
curl http://127.0.0.1:3000/api/health
docker logs --tail 100 opsvibes
```

The health endpoint should return JSON containing `"status":"ok"`.

## Backup

Stop writes before copying the SQLite files:

```sh
docker stop opsvibes
tar -C /etc -czf opsvibes-data-backup.tgz opsvibes
docker start opsvibes
```

Protect this archive because it contains the database and encryption key.

## Harbor

Harbor was not included in the installer because the previously supplied
`harbor.nodesign.vn` endpoint was unreachable on ports 80, 443, 5000, and 8443.
Once Harbor is serving a Docker Registry endpoint, mirror the image with:

```sh
docker tag ghcr.io/technoidlover/opsvibeclone:120c99e \
  harbor.nodesign.vn/opsvibeclone:120c99e
docker push harbor.nodesign.vn/opsvibeclone:120c99e
```
