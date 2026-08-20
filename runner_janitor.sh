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

echo "== Docker orphaned buildx builder cleanup (builder + container + state volume) =="
# CI-6 dropped setup-buildx-action in favour of host-daemon BuildKit, so
# app-ci.yml no longer creates buildx builder containers; any builder-<uuid>
# entry still around predates that cutover and should never reappear
# (INF-46: if one does, that is a new, active path and a separate finding --
# not something to keep silently sweeping).
#
# INF-46: `docker rm` on the container alone (the old approach here) leaves
# the builder registered and its state volume behind -- `docker buildx rm`
# removes builder + container + volume together. Enumerate names from
# `docker buildx ls` itself, never derive them from a container/volume name:
# those carry a DIFFERENT prefix (buildx_buildkit_builder-) PLUS a trailing
# node index (.../builder-<uuid>0_state) that a prefix/suffix-stripping
# derivation would silently get wrong, leaving the script deleting nothing.
#
# The awk state machine below matches only the UNINDENTED builder-level row
# for the NAME (`docker buildx ls` prints each builder's node(s) on an
# INDENTED line below it, so `^builder-`, anchored with no leading
# whitespace, never matches those -- the node index never enters the
# captured name), while checking EVERY line belonging to that builder
# (header line and any indented node lines below it) for the word
# "running" before deciding to print it. This is deliberately NOT a
# same-line "inactive" check: `docker buildx ls` puts a builder's status on
# its OWN indented node line in the common multi-line form, not on the
# header line the name comes from, and requiring both on one line would
# silently match nothing at all -- a builder is only skipped when a
# "running" node is actually seen, never assumed idle by the shape of the
# output. The match is WHOLE-WORD (word boundaries via [[:space:]]/anchors),
# not a bare substring: a plain `/running/` would also fire on unrelated
# column content that merely contains the substring (e.g. a socket path
# like ".../overrunning.sock"), wrongly marking that builder as active.
while IFS= read -r name; do
  [ -n "$name" ] || continue
  echo "removing orphaned buildx builder (+ container + state volume): $name"
  docker buildx rm "$name" || true
done < <(docker buildx ls 2>/dev/null | awk '
  function is_running() { return $0 ~ /(^|[[:space:]])running([[:space:]]|$)/ }
  function flush() { if (name != "" && !running) print name }
  /^builder-/ { flush(); name = $1; running = is_running() ? 1 : 0; next }
  is_running() { running = 1 }
  END { flush() }
' || true)

echo "== Docker orphaned buildx state-volume cleanup (fallback) =="
# INF-46: covers the case docker buildx rm above cannot -- a state volume
# whose builder entry is already gone from buildx's own registry. Scoped
# ONLY to the exact naming convention (never a blanket `volume prune -af`,
# which would widen this script's blast radius right where the INF-42
# review already had to narrow it -- see the WARNING at the top of this
# file). `docker volume rm` on a volume still mounted by a container fails
# harmlessly (caught by || true); an active builder's volume is never at risk.
while IFS= read -r vol; do
  [ -n "$vol" ] || continue
  echo "removing orphaned buildx state volume: $vol"
  docker volume rm "$vol" || true
done < <(docker volume ls --filter "name=buildx_buildkit_builder-" --format '{{.Name}}' 2>/dev/null || true)

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
