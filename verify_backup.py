#!/usr/bin/env python3
"""
verify_backup.py

Verifies that the latest restic snapshot in the B2 repo contains fresh Postgres dumps
and that every dump can be streamed from the repo and passes a gzip integrity test.

- Picks the newest snapshot across restic "--latest 1" groups.
- Filters *.sql.gz by a time window around the snapshot time (default: 2 hours).
- Verifies ALL matching dumps (not just one) by piping `restic dump` -> `gzip -t`.

Exit code:
- 0 on success
- 1 on any failure
"""

import os
import sys
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import List, Optional

# --- Config ---
REPO_URL = os.environ.get("RESTIC_REPO_B2")
REPO_PWD = os.environ.get("RESTIC_PASSWORD")

AWS_ID = os.environ.get("B2_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
AWS_KEY = os.environ.get("B2_APP_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")

RESTIC_BIN = os.environ.get("RESTIC_BIN_OVERRIDE") or shutil.which("restic") or "/usr/bin/restic"

# Verify dumps within this window around snapshot time
MAX_SNAPSHOT_WINDOW_HOURS = float(os.environ.get("MAX_SNAPSHOT_WINDOW_HOURS", "0.2"))

# If set to 1, do not filter by timestamp and verify all *.sql.gz found in the snapshot
VERIFY_ALL_SQL_GZ = os.environ.get("VERIFY_ALL_SQL_GZ") == "1"


def mask_repo(url: str) -> str:
    if not url:
        return "UNKNOWN"
    return (url[:15] + "..." + url[-10:]) if len(url) > 25 else url


def parse_timestamp_from_filename(path: str) -> Optional[datetime]:
    """
    Extracts timestamp from filenames like:
      ..._2026-02-08T103000Z.sql.gz
    or (legacy):
      ..._2025-11-20T22:06:07Z.sql.gz
    """
    try:
        base = os.path.basename(path)
        if base.endswith(".sql.gz"):
            base = base[:-7]

        parts = base.split("_")
        if not parts:
            return None

        timestamp_str = parts[-1]

        try:
            return datetime.strptime(timestamp_str, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

        try:
            return datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    except Exception:
        return None


def build_restic_env() -> dict:
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = REPO_URL or ""
    env["RESTIC_PASSWORD"] = REPO_PWD or ""

    # Required for B2 S3-style repos
    if AWS_ID:
        env["AWS_ACCESS_KEY_ID"] = AWS_ID
    if AWS_KEY:
        env["AWS_SECRET_ACCESS_KEY"] = AWS_KEY

    return env


def get_latest_snapshot(env: dict) -> tuple[str, str]:
    """
    Uses `restic snapshots --latest 1 --json`, then sorts by time to pick the absolute newest.
    """
    cmd = [RESTIC_BIN, "snapshots", "--latest", "1", "--json"]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)

    if res.returncode != 0:
        raise RuntimeError(f"Failed to list snapshots: {res.stderr.strip()}")

    snapshots = json.loads(res.stdout)
    if not snapshots:
        raise RuntimeError("No snapshots found in repository")

    snapshots.sort(key=lambda s: s["time"], reverse=True)
    latest_id = snapshots[0]["id"]
    snap_time_str = snapshots[0]["time"]
    return latest_id, snap_time_str


def list_sql_gz_files(env: dict, snapshot_id: str) -> List[str]:
    """
    Lists files in snapshot and returns those ending with .sql.gz.
    Assumes `restic ls` output contains lines ending with paths.
    """
    cmd = [RESTIC_BIN, "ls", snapshot_id]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)

    if res.returncode != 0:
        raise RuntimeError(f"Failed to list snapshot files: {res.stderr.strip()}")

    files = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.endswith(".sql.gz"):
            files.append(line)

    return files


def verify_gzip_stream_from_restic(env: dict, snapshot_id: str, path: str) -> None:
    """
    Streams a file from restic and verifies gzip integrity without writing to disk:
      restic dump <snap> <path> | gzip -t
    """
    dump_cmd = [RESTIC_BIN, "dump", snapshot_id, path]
    gzip_cmd = ["gzip", "-t"]

    p_dump = subprocess.Popen(dump_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
    try:
        # gzip reads from stdin when no filenames are provided
        p_gz = subprocess.run(gzip_cmd, stdin=p_dump.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=False)
    finally:
        if p_dump.stdout:
            p_dump.stdout.close()

    dump_stderr = p_dump.stderr.read() if p_dump.stderr else b""
    dump_rc = p_dump.wait()

    if dump_rc != 0:
        err = dump_stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"restic dump failed for {path}: {err}")

    if p_gz.returncode != 0:
        gz_err = (p_gz.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"gzip integrity check failed for {path}: {gz_err}")


def main() -> int:
    print("=== Starting Strict Backup Verification (Freshness + GZIP) ===")

    if not REPO_URL or not REPO_PWD:
        print("[CRITICAL] RESTIC_REPO_B2 or RESTIC_PASSWORD missing.")
        return 1

    print(f"-> Target Repository: {mask_repo(REPO_URL)}")

    env = build_restic_env()

    try:
        print("-> Checking snapshots...")
        latest_id, snap_time_str = get_latest_snapshot(env)
        print(f"-> Latest snapshot ID: {latest_id}")
        print(f"-> Snapshot Date:      {snap_time_str}")

        snap_time = datetime.fromisoformat(snap_time_str.replace("Z", "+00:00"))
        min_time = snap_time - timedelta(hours=MAX_SNAPSHOT_WINDOW_HOURS)
        max_time = snap_time + timedelta(minutes=5)

        print("-> Searching for .sql.gz files in snapshot...")
        all_files = list_sql_gz_files(env, latest_id)

        if not all_files:
            print("[ERROR] No .sql.gz files found in snapshot at all!")
            return 1

        if VERIFY_ALL_SQL_GZ:
            candidates = all_files
            print(f"-> VERIFY_ALL_SQL_GZ=1: verifying ALL {len(candidates)} .sql.gz files (no time filter).")
        else:
            candidates = []
            for f in all_files:
                ts = parse_timestamp_from_filename(f)
                if ts and (min_time <= ts <= max_time):
                    candidates.append(f)

            print(f"-> Filtering for files within {MAX_SNAPSHOT_WINDOW_HOURS}h of snapshot time")
            print(f"   Window: {min_time.strftime('%Y-%m-%d %H:%M:%S')} UTC  ..  {max_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"-> Found {len(candidates)} matching dumps out of {len(all_files)} .sql.gz files.")

        if not candidates:
            print("----------------------------------------------------------------")
            print(f"[CRITICAL FAILURE] No matching .sql.gz dumps found for this snapshot window.")
            print(f"Snapshot is from: {snap_time_str}")
            print("The backup job claims success, but dump files for this run are not present or not parseable.")
            print("You can set VERIFY_ALL_SQL_GZ=1 to verify all .sql.gz in the snapshot.")
            print("----------------------------------------------------------------")
            return 1

        print(f"-> Verifying {len(candidates)} dump(s) by streaming from restic and running gzip -t...")
        failures = []
        for idx, path in enumerate(sorted(candidates), start=1):
            print(f"   [{idx}/{len(candidates)}] {path} ...", end="", flush=True)
            try:
                verify_gzip_stream_from_restic(env, latest_id, path)
                print(" OK")
            except Exception as e:
                print(" FAIL")
                failures.append((path, str(e)))

        if failures:
            print("----------------------------------------------------------------")
            print(f"[CRITICAL FAILURE] {len(failures)}/{len(candidates)} dump(s) failed verification:")
            for path, err in failures:
                print(f"- {path}: {err}")
            print("----------------------------------------------------------------")
            return 1

        print("[SUCCESS] Backup is FRESH and VALID (all selected dumps passed gzip integrity).")
        return 0

    except Exception as e:
        print(f"[CRITICAL ERROR] Script crashed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
