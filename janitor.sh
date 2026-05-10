#!/usr/bin/env bash
# Purpose: Light-weight maintenance: docker prune, disk usage, unattended-upgrades dry-run.

set -euo pipefail

echo "== Disk usage =="
df -h /

echo "== Docker system prune (non-interactive) =="
docker system prune -f || true

echo "== Docker images =="
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | head -n 50

echo "== Unattended-upgrades dry-run =="
if command -v unattended-upgrades >/dev/null; then
  unattended-upgrades --dry-run --debug | head -n 80 || true
else
  echo "unattended-upgrades not installed"
fi

echo "== Done =="
