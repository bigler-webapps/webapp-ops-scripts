#!/usr/bin/env bash
# Purpose: Docker GC for netcup-runner-1 (INF-42) -- the self-hosted CI
# runner, which builds a fresh image on every push and therefore needs
# daily, short-window cleanup. janitor.sh's app-server profile
# (--keep-storage=20GB / until=48h, weekly-ish cadence) reclaimed ~0B here:
# measured 2026-08-19, 406 images/85.94GB (33.53GB reclaimable), 289
# build-cache entries/21.49GB (16.62GB reclaimable) -- on a box that
# rebuilds per push, ~99% of that is younger than 48h, let alone 7 days.
#
# This is a SEPARATE script from janitor.sh, deliberately: it runs on the
# runner it cleans (no SSH/Tailscale needed), and it prunes volumes, which
# janitor.sh must never do (test_janitor.py::test_janitor_never_prunes_volumes).
#
# WARNING: the volume prune below is safe ONLY because this host's volumes
# are throwaway per-job Postgres containers from CI `services:`, never
# persistent application data. Do NOT lift this line into janitor.sh or run
# this script against any app server (main-prod/staging/etc.) -- there it
# would delete production data.

set -euo pipefail

echo "== Disk usage (before) =="
df -h /

echo "== Docker container prune (stopped containers) =="
docker container prune -f || true

echo "== Docker orphaned buildx builder cleanup =="
# CI-6 dropped setup-buildx-action in favour of host-daemon BuildKit, so
# app-ci.yml no longer creates buildx builder containers. Any
# buildx_buildkit_builder-* container still around is leftover from a
# different/earlier path, sits idle rather than "stopped", and is
# therefore untouched by the container prune above.
while IFS= read -r name; do
  [ -n "$name" ] || continue
  echo "removing orphaned buildx builder container: $name"
  docker rm -f "$name" || true
done < <(docker ps -a --filter "name=buildx_buildkit_builder-" --format '{{.Names}}' 2>/dev/null || true)

echo "== Docker image prune (tagged-unused images, untouched >6h) =="
# INF-42: runner-maintenance.yml's until=168h filtered on CREATION time, not
# unused-ness, and reclaimed 0B on a manual run -- almost every image here is
# younger than a week. 6h protects images a just-finished or still-queued
# job might still need (median job runtime 42s, p90 queue wait 13.5min,
# observed max 34min) without being a no-op like the 168h window was.
docker image prune -af --filter "until=6h" || true

echo "== Docker builder prune (build cache, keep-storage floor) =="
# Floor well under janitor.sh's 20GB: this host's cache regrows fast (per
# push) and daily cleanup, not weekly, is what keeps it in check, so a
# small floor is enough to keep some warm-cache benefit between builds.
docker builder prune -af --keep-storage=5GB || true

echo "== Docker volume prune =="
# Deliberate inversion of janitor.sh (see WARNING above). Measured
# 2026-08-19: 62 volumes / 21.40GB, 3.44GB reclaimable -- ephemeral
# `services:` Postgres containers from CI jobs, not app data.
docker volume prune -f || true

echo "== Docker storage diagnostic (verbose) =="
docker system df -v || true

echo "== Largest Docker/containerd top-level directories =="
du -sh /var/lib/docker/*/ /var/lib/containerd/*/ 2>/dev/null | sort -rh | head -n 15 || true

echo "== Disk usage (after) =="
df -h /

echo "== Done =="
