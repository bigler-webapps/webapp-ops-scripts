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
#
# INF-51 (2026-08-20): staging hit 100% disk, 0 bytes free; three containers
# entered restart loops on "No space left on device" and the janitor's own
# rsync failed for the same reason -- the cleanup mechanism blocked by the
# condition it exists to fix. Two changes here, one in the deploy path.
#
#   * Image retention is now BY COUNT PER REPOSITORY, not by age. The old
#     `--filter "until=48h"` could reach 26 of 262 images on that box (the
#     other 236 were younger than the window), because a 48h rollback buffer
#     is sized for a prod cadence of a few deploys a week and hram deployed
#     15 times in one hour. A time window scales with deploy frequency; a
#     count does not. Trade-off, stated plainly: the rollback window is now
#     the last N images rather than 48 hours -- SMALLER in wall-clock terms
#     on a busy app, LARGER on one that deploys monthly.
#
#   * The script now FAILS when it finishes with the disk still above the
#     threshold. Measured AFTER the prune, deliberately: a run that arrives
#     at 95% and leaves at 40% is a janitor doing its job and stays silent.
#     One that leaves the disk above the threshold is a janitor that can no
#     longer keep up, and that is the signal. Before this, the 10:08 run on
#     2026-08-20 printed "Disk usage (after): 96%" and exited success --
#     nothing compared that number to anything.
#
# `|| true` now appears ONLY on diagnostic lines, never on a prune. INF-19
# added it for a real SIGPIPE abort on `docker images | head -50` and it must
# stay there; on a prune it made a genuinely failed cleanup indistinguishable
# from a successful one.
#
# NOT fixed here, and worth knowing: the build-cache floor below withheld
# essentially the whole cache in the INF-51 incident (cache stood at 20.9 GB
# against a 20 GB floor), which is why that step produced no output at all.
# INF-51's scope deliberately excludes changing it.

set -euo pipefail

# Both tunable per host: `janitor.sh` is synced to every janitor-role box,
# including the three prod servers, whose disks and churn differ from
# staging's. Defaults are the operator's 2026-08-20 decision.
IMAGE_RETENTION="${JANITOR_IMAGE_RETENTION:-3}"
DISK_THRESHOLD="${JANITOR_DISK_THRESHOLD:-90}"

# Validate both BEFORE anything is deleted, and fail loudly.
#
# `${VAR:-default}` only rescues an UNSET or empty override -- a non-empty
# nonsense value passes straight through. For the threshold that is survivable:
# a non-numeric value makes `[ -gt ]` itself error and `set -e` aborts. For the
# retention count it is not, and this is the dangerous half: awk coerces a
# non-numeric `keep` to 0, so `n[$1] > keep` is true for the FIRST image of
# every repository and the pass silently degrades from "keep the newest 3" to
# "delete everything no container is holding" -- on three production boxes,
# with no error. A prune that quietly does far more than configured is the same
# failure class as a threshold that quietly never fires.
case "$IMAGE_RETENTION" in
  ''|*[!0-9]*)
    echo "FAIL: JANITOR_IMAGE_RETENTION must be a positive integer, got '${IMAGE_RETENTION}'"
    exit 1
    ;;
esac
if [ "$IMAGE_RETENTION" -lt 1 ]; then
  echo "FAIL: JANITOR_IMAGE_RETENTION must be >= 1, got '${IMAGE_RETENTION}' -- 0 would remove"
  echo "      every image no container currently holds, third-party images included."
  exit 1
fi
case "$DISK_THRESHOLD" in
  ''|*[!0-9]*)
    echo "FAIL: JANITOR_DISK_THRESHOLD must be an integer percentage, got '${DISK_THRESHOLD}'"
    exit 1
    ;;
esac
if [ "$DISK_THRESHOLD" -lt 1 ] || [ "$DISK_THRESHOLD" -gt 100 ]; then
  echo "FAIL: JANITOR_DISK_THRESHOLD must be between 1 and 100, got '${DISK_THRESHOLD}'"
  exit 1
fi

# Removing `|| true` from the prunes means a transient docker failure now
# aborts the run -- which is the point, but it would abort BEFORE the disk
# reading that explains what happened. This trap guarantees the reading is
# emitted on any early exit; the flag stops it printing twice on the normal
# path.
DISK_REPORTED=0
trap '[ "$DISK_REPORTED" = "1" ] || { echo "== Disk usage (after early exit) =="; df -h / || true; }' EXIT

echo "== Disk usage (before) =="
df -h /

echo "== Docker container prune (stopped containers) =="
docker container prune -f

# Keep the newest N tags of every repository and remove the rest. Two guards
# matter here and both are deliberate:
#   - Images referenced by ANY container, running or stopped, are never
#     removed. Retention is about history, not live workloads.
#   - A single failed removal is tolerated and reported, not fatal. One ref can
#     legitimately resist removal (another image depends on its layers, or a
#     container appeared between the listing and the removal). That is a
#     different case from the bulk prunes above and below, which now abort.
prune_images_by_retention() {
  local keep="$1"
  local in_use victims id ref kept=0 removed=0 skipped=0

  in_use="$(docker ps -aq | xargs -r docker inspect --format '{{.Image}}' 2>/dev/null | sort -u || true)"

  # Order and count, and both are subtler than they look.
  #
  # TIME: `docker images --format '{{.CreatedAt}}'` renders LOCAL time with the
  # offset attached ("2026-08-20 10:00:00 +0200 CEST"). Sorting that string is
  # wrong across the autumn DST fall-back: the offset flips +0200 -> +0100, and
  # a lexicographic compare ranks the larger offset as newer, so an image built
  # just AFTER the clock change sorts as older than one built just before it.
  # For that one hour a year the retention cut would keep the older image and
  # delete the newer. `docker image inspect --format '{{.Created}}'` returns
  # RFC3339 in UTC, which sorts correctly and has no offset at all.
  #
  # COUNT: retention is per distinct IMAGE, not per tag. One image carrying two
  # tags in the same repository would otherwise eat two of the N slots and push
  # a genuinely older, distinct image out early -- "keep 3" silently meaning
  # "keep 2". The awk below ranks by first sighting of an image ID and, once an
  # image falls past the cut, removes every tag it holds.
  #
  # One inspect call for the whole list, not one per image. Guarded because an
  # image can disappear between the listing and the inspect; that is a listing,
  # not a prune, so the guard here is not the `|| true` INF-51 removed.
  victims="$(
    docker images --no-trunc -q \
      | sort -u \
      | xargs -r docker image inspect --format '{{.Created}}|{{.Id}}|{{range .RepoTags}}{{.}} {{end}}' 2>/dev/null \
      | awk '{
          split($0, f, "|")
          created = f[1]; id = f[2]
          count = split(f[3], tags, " ")
          for (i = 1; i <= count; i++) {
            if (tags[i] == "" || index(tags[i], "<none>") > 0) continue
            # repository = everything before the LAST colon (registry hosts may
            # carry a :port, so a naive split on the first colon is wrong)
            pos = 0
            for (k = length(tags[i]); k > 0; k--) {
              if (substr(tags[i], k, 1) == ":") { pos = k; break }
            }
            if (pos < 2) continue
            print substr(tags[i], 1, pos - 1) "|" created "|" id "|" tags[i]
          }
        }' \
      | sort -t'|' -k1,1 -k2,2r \
      | awk -F'|' -v keep="$keep" '{
          key = $1 "|" $3
          if (!(key in rank)) { n[$1]++; rank[key] = n[$1] }
          if (rank[key] > keep) print $3 "|" $4
        }' \
      || true
  )"

  if [ -z "$victims" ]; then
    echo "   nothing beyond the retention count"
    return 0
  fi

  while IFS='|' read -r id ref; do
    [ -n "$ref" ] || continue
    if printf '%s\n' "$in_use" | grep -qxF "$id"; then
      echo "   keep (in use by a container): $ref"
      kept=$((kept + 1))
      continue
    fi
    if docker image rm "$ref" >/dev/null 2>&1; then
      echo "   removed: $ref"
      removed=$((removed + 1))
    else
      echo "   could not remove (skipped): $ref"
      skipped=$((skipped + 1))
    fi
  done <<EOF
$victims
EOF

  echo "   retention summary: removed=$removed kept-in-use=$kept skipped=$skipped"
}

echo "== Docker image retention (keep newest ${IMAGE_RETENTION} per repository) =="
prune_images_by_retention "$IMAGE_RETENTION"

# The retention pass above only considers TAGGED images (it filters `<none>`),
# so dangling layers still need their own sweep. No `-a`: that would delete
# every unused tagged image and defeat the retention count entirely.
echo "== Docker image prune (dangling only) =="
docker image prune -f

echo "== Docker builder prune (build cache, keep-storage floor) =="
docker builder prune -af --keep-storage=20GB

echo "== Docker storage diagnostic (verbose) =="
docker system df -v || true

echo "== Largest Docker/containerd top-level directories =="
du -sh /var/lib/docker/*/ /var/lib/containerd/*/ 2>/dev/null | sort -rh | head -n 15 || true

echo "== Disk usage (after) =="
df -h /
DISK_REPORTED=1

echo "== Docker images =="
# `head -n 50` closes the pipe once it has 50 lines; on a host with more than
# 50 images `docker images` is still writing when that happens and dies on
# SIGPIPE (exit 141), which `pipefail` + `set -e` then aborted the whole
# script on (INF-19: staging crossed 50 images on 2026-08-12 and has failed
# every night since). This line is diagnostic only -- `|| true` neutralises
# exactly that closed-pipe case. The prunes above no longer carry it (INF-51).
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | head -n 50 || true

echo "== Unattended-upgrades dry-run =="
if command -v unattended-upgrades >/dev/null; then
  unattended-upgrades --dry-run --debug | head -n 80 || true
else
  echo "unattended-upgrades not installed"
fi

# Last, so every diagnostic above is already on the log when this fires.
# `df -P` for the POSIX one-line-per-filesystem guarantee; `-h` above is for
# humans, this is for parsing.
echo "== Disk threshold check (fail above ${DISK_THRESHOLD}%) =="
DISK_USED_PCT="$(df -P / | awk 'NR==2 { gsub(/%/, "", $5); print $5 }')"
if [ -z "$DISK_USED_PCT" ]; then
  echo "FAIL: could not read disk usage from df -P /"
  exit 1
fi
echo "   disk used after prune: ${DISK_USED_PCT}% (threshold ${DISK_THRESHOLD}%)"
if [ "$DISK_USED_PCT" -gt "$DISK_THRESHOLD" ]; then
  echo "FAIL: janitor finished with the disk still at ${DISK_USED_PCT}%, above the ${DISK_THRESHOLD}% threshold."
  echo "   The prunes above ran and were not enough. This is the signal that cleanup"
  echo "   can no longer keep up with what is being created -- not a transient blip."
  exit 1
fi

echo "== Done =="
