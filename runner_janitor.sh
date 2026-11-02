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

echo "== Orphaned E2E stack cleanup =="
# E2E jobs leave named app/db/Redis containers, a network and an image on the
# shared runner daemon. Group strictly by the run-attempt suffix found on the
# three known container names; never broaden this to a Docker prune.
E2E_STACK_MAX_AGE_MIN="${E2E_STACK_MAX_AGE_MIN:-400}"
RUNNER_PROC_ROOT="${RUNNER_PROC_ROOT:-/proc}"
declare -A e2e_db=() e2e_redis=() e2e_app=() e2e_newest=() e2e_runner=() e2e_bad=() e2e_seen=()

e2e_slot_state() {
  local runner="$1" slot unit proc cmdline cgroup
  if [[ ! "$runner" =~ ^netcup-runner-1-slot([0-9]+)$ ]]; then
    return 2
  fi
  slot="${BASH_REMATCH[1]}"
  unit="gh-runner@${slot}.service"
  if [[ ! -d "$RUNNER_PROC_ROOT" ]]; then
    return 2
  fi
  for proc in "$RUNNER_PROC_ROOT"/[0-9]*; do
    [[ -d "$proc" ]] || continue
    # A process whose command line cannot be read leaves the slot state unknown (a live worker could hide
    # behind it), and unknown keeps the stack.
    [[ -r "$proc/cmdline" ]] || return 2
    cmdline="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
    [[ "$cmdline" == *Runner.Worker* ]] || continue
    [[ -r "$proc/cgroup" ]] || return 2
    cgroup="$(cat "$proc/cgroup" 2>/dev/null || true)"
    [[ -n "$cgroup" ]] || return 2
    if grep -Eq "(^|/)$unit([/[:space:]]|$)" <<< "$cgroup"; then
      return 0
    fi
  done
  return 1
}

now_epoch="$(date +%s)"
while IFS= read -r container; do
  [[ "$container" =~ ^ci-e2e-(db|redis|app)-([0-9]+-[0-9]+)$ ]] || continue
  kind="${BASH_REMATCH[1]}"
  run_attempt="${BASH_REMATCH[2]}"
  e2e_seen["$run_attempt"]=1
  case "$kind" in
    db) e2e_db["$run_attempt"]="$container" ;;
    redis) e2e_redis["$run_attempt"]="$container" ;;
    app) e2e_app["$run_attempt"]="$container" ;;
  esac
  inspect="$(docker inspect --type container --format '{{.Created}}|{{index .Config.Labels "ci.e2e.runner"}}' "$container" 2>/dev/null)" || {
    e2e_bad["$run_attempt"]=1
    continue
  }
  created="${inspect%%|*}"
  runner="${inspect#*|}"
  created_epoch="$(date -d "$created" +%s 2>/dev/null)" || {
    e2e_bad["$run_attempt"]=1
    continue
  }
  if [[ -z "${e2e_newest[$run_attempt]+x}" || "$created_epoch" -gt "${e2e_newest[$run_attempt]}" ]]; then
    e2e_newest["$run_attempt"]="$created_epoch"
  fi
  if [[ -n "$runner" && "$runner" != "<no value>" ]]; then
    if [[ -n "${e2e_runner[$run_attempt]:-}" && "${e2e_runner[$run_attempt]}" != "$runner" ]]; then
      e2e_bad["$run_attempt"]=1
    else
      e2e_runner["$run_attempt"]="$runner"
    fi
  fi
done < <(docker ps -a --filter 'name=ci-e2e-' --format '{{.Names}}' 2>/dev/null || true)

while IFS= read -r network; do
  [[ "$network" =~ ^ci-e2e-net-([0-9]+-[0-9]+)$ ]] || continue
  run_attempt="${BASH_REMATCH[1]}"
  e2e_seen["$run_attempt"]=1
  runner="$(docker network inspect --format '{{index .Labels "ci.e2e.runner"}}' "$network" 2>/dev/null)" || {
    e2e_bad["$run_attempt"]=1
    continue
  }
  if [[ -n "$runner" && "$runner" != "<no value>" ]]; then
    if [[ -n "${e2e_runner[$run_attempt]:-}" && "${e2e_runner[$run_attempt]}" != "$runner" ]]; then
      e2e_bad["$run_attempt"]=1
    else
      e2e_runner["$run_attempt"]="$runner"
    fi
  fi
done < <(docker network ls --filter 'name=ci-e2e-net-' --format '{{.Name}}' 2>/dev/null || true)

while IFS= read -r image; do
  [[ "$image" =~ ^ci-e2e:([0-9]+-[0-9]+)$ ]] || continue
  run_attempt="${BASH_REMATCH[1]}"
  e2e_seen["$run_attempt"]=1
  runner="$(docker image inspect --format '{{index .Config.Labels "ci.e2e.runner"}}' "$image" 2>/dev/null)" || {
    e2e_bad["$run_attempt"]=1
    continue
  }
  if [[ -n "$runner" && "$runner" != "<no value>" ]]; then
    if [[ -n "${e2e_runner[$run_attempt]:-}" && "${e2e_runner[$run_attempt]}" != "$runner" ]]; then
      e2e_bad["$run_attempt"]=1
    else
      e2e_runner["$run_attempt"]="$runner"
    fi
  fi
done < <(docker image ls --filter 'reference=ci-e2e:*' --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true)

for run_attempt in "${!e2e_seen[@]}"; do
  reason=""
  state="unknown"
  if [[ -n "${e2e_bad[$run_attempt]:-}" ]]; then
    echo "kept (active): ci-e2e-$run_attempt (stack metadata unavailable)"
    continue
  fi
  runner="${e2e_runner[$run_attempt]:-}"
  # A job cannot outlive its 360-minute GitHub cap, so a stack older than the bound is orphaned
  # whatever the slot is doing now (a busy slot may be running a LATER job).
  if [[ -n "${e2e_newest[$run_attempt]:-}" ]]; then
    age_minutes="$(( (now_epoch - e2e_newest[$run_attempt]) / 60 ))"
    if (( age_minutes > E2E_STACK_MAX_AGE_MIN )); then
      reason="newest container is ${age_minutes}m old"
    fi
  fi
  if [[ -n "$reason" ]]; then
    :
  elif [[ -n "$runner" ]]; then
    e2e_slot_state "$runner" && state="active" || case "$?" in 1) state="inactive" ;; *) state="unknown" ;; esac
    if [[ "$state" == "active" ]]; then
      echo "kept (active): ci-e2e-$run_attempt ($runner has Runner.Worker)"
      continue
    elif [[ "$state" == "unknown" ]]; then
      echo "kept (active): ci-e2e-$run_attempt (slot state unknown for $runner)"
      continue
    fi
    reason="slot $runner has no Runner.Worker"
  else
    if [[ -z "${e2e_newest[$run_attempt]:-}" ]]; then
      echo "kept (active): ci-e2e-$run_attempt (no container age available)"
      continue
    fi
    age_minutes="$(( (now_epoch - e2e_newest[$run_attempt]) / 60 ))"
    if (( age_minutes <= E2E_STACK_MAX_AGE_MIN )); then
      echo "kept (active): ci-e2e-$run_attempt (unlabelled stack is ${age_minutes}m old)"
      continue
    fi
    reason="newest container is ${age_minutes}m old"
  fi

  echo "orphaned: ci-e2e-$run_attempt ($reason)"
  docker rm -f "${e2e_app[$run_attempt]:-ci-e2e-app-$run_attempt}" "${e2e_redis[$run_attempt]:-ci-e2e-redis-$run_attempt}" "${e2e_db[$run_attempt]:-ci-e2e-db-$run_attempt}" 2>/dev/null || true
  docker network rm "ci-e2e-net-$run_attempt" 2>/dev/null || true
  docker image rm -f "ci-e2e:$run_attempt" 2>/dev/null || true
done

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
# The awk state machine below separates two DIFFERENT things (INF-50: an
# earlier version conflated them, and that conflation is exactly what made
# `docker buildx ls` always drop its own last builder -- see below):
#   - RECORD BOUNDARY: any UNINDENTED line starts a new record (builder or
#     otherwise). `docker buildx ls` prints each builder's node(s) on an
#     INDENTED line below it, so this never mistakes a node line for a new
#     record and the node index never enters a captured name.
#   - OUTPUT CRITERION: a record is only printed if its name matches
#     `^builder-` AND no line in that whole record (header or any indented
#     node line) said "running".
# INF-50: the previous version used `/^builder-/` as BOTH the record
# boundary and part of the output test. `docker buildx ls` always ends with
# the `default` builder, and `default` is always `running` -- `default`'s
# own header line never matches `/^builder-/`, so it never flushed the
# PRIOR (real) builder's record before overwriting `name`, and its node
# line's "running" then got attributed to whatever builder `name` still
# held. Net effect: the actual LAST builder in the list was silently
# dropped on every single run, deterministically -- reproduced against a
# real box (buildx v0.32.2, Docker 29.7.2) that had exactly one leftover
# builder, `docker buildx ls` ending in `default*`, and it was never
# collected. Splitting "what starts a new record" from "what may be
# printed" is what fixes this: `default`'s line still starts a new record
# (so the prior builder gets flushed), it just never OUTPUTS because its
# name doesn't match `^builder-`.
#
# INF-46's own history repeats the same class of error INF-46 itself was
# written to fix: a same-line "inactive" check would again match nothing
# (see INF-46), so status is deliberately checked across the WHOLE record,
# and the check is WHOLE-WORD (word boundaries via [[:space:]]/anchors), not
# a bare substring -- a plain `/running/` would also fire on unrelated
# column content that merely contains the substring (e.g. a socket path
# like ".../overrunning.sock"), wrongly marking that builder as active.
while IFS= read -r name; do
  [ -n "$name" ] || continue
  echo "removing orphaned buildx builder (+ container + state volume): $name"
  docker buildx rm "$name" || true
done < <(docker buildx ls 2>/dev/null | awk '
  function is_running() { return $0 ~ /(^|[[:space:]])running([[:space:]]|$)/ }
  function flush() { if (name ~ /^builder-/ && !running) print name }
  /^[^[:space:]]/ { flush(); name = $1; running = is_running() ? 1 : 0; next }
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
