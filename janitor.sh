#!/usr/bin/env bash
# Purpose: Light-weight maintenance: docker prune, disk usage, unattended-upgrades dry-run.
#
# INF-1 (2026-08-01): `docker system prune -f` alone only removes DANGLING
# resources (untagged images, stopped containers) -- it never touches tagged-
# but-unused images or the build cache, which is where disk actually fills up
# (staging measured: 59.5 GB build cache, 51.83 GB reclaimable, 0% of it
# "active"). Each prune below targets one of those buckets explicitly, with a
# daily keep-storage FLOOR on the build cache so it doesn't grow unbounded
# again but still keeps 20 GB of warm-cache benefit across repeat builds.
# Volumes are deliberately EXCLUDED -- a detached-but-still-needed data volume
# on a prod box is not worth the few GB reclaimed.

set -euo pipefail

echo "== Disk usage (before) =="
df -h /

echo "== Docker container prune (stopped containers) =="
docker container prune -f || true

echo "== Docker image prune (tagged-unused images, untouched >48h) =="
docker image prune -af --filter "until=48h" || true

echo "== Docker builder prune (build cache, keep-storage floor) =="
docker builder prune -af --keep-storage=20GB || true

echo "== Disk usage (after) =="
df -h /

echo "== Docker images =="
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | head -n 50

echo "== Unattended-upgrades dry-run =="
if command -v unattended-upgrades >/dev/null; then
  unattended-upgrades --dry-run --debug | head -n 80 || true
else
  echo "unattended-upgrades not installed"
fi

echo "== Done =="
