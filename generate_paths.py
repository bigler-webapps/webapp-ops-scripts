#!/usr/bin/env python3
import argparse
import os
import subprocess
import json
import sys
from pathlib import Path

IGNORE_KEYWORDS = ["postgres", "postgres_data", "db_data", "pgdata", "redis", "redis_data", "mariadb", "mariadb_data", "mysql"]

ALLOW_VOLUME_FAILURE = os.environ.get("ALLOW_VOLUME_FAILURE") == "1"


def docker_cmd_prefix():
    """Choose docker invocation, either direct or via sudo -n."""
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return ["docker"]
    except subprocess.CalledProcessError:
        return ["sudo", "-n", "docker"]


def emit_existing(path: str, seen: set) -> None:
    """Print a path only if it exists, and only once."""
    p = Path(path)
    try:
        exists = p.exists()
    except PermissionError:
        sys.stderr.write(f"Skipping unreadable path: {path}\n")
        return

    if exists:
        s = str(p)
        if s not in seen:
            seen.add(s)
            print(s)
    else:
        sys.stderr.write(f"Skipping missing path: {path}\n")


def list_volume_names(prefix: list) -> list:
    cmd = prefix + ["volume", "ls", "--format", "{{.Name}}"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [v.strip() for v in res.stdout.splitlines() if v.strip()]


def inspect_mountpoints(prefix: list, volumes: list) -> list:
    """Inspect Docker volumes and return mountpoints, filtered by name."""
    mountpoints = []
    if not volumes:
        return mountpoints

    chunk_size = 200
    for i in range(0, len(volumes), chunk_size):
        chunk = volumes[i:i + chunk_size]
        cmd = prefix + ["volume", "inspect"] + chunk
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)

        for vol in data:
            name = (vol.get("Name") or "").lower()
            mountpoint = vol.get("Mountpoint") or ""
            if not mountpoint:
                continue
            if any(k in name for k in IGNORE_KEYWORDS):
                continue
            mountpoints.append(mountpoint)

    return mountpoints


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate restic include-paths list.")
    parser.add_argument(
        "--infra-paths-file",
        default=os.environ.get("INFRA_PATHS_FILE", ""),
        help="File with static infra paths to back up (one per line, # = comment).",
    )
    args = parser.parse_args()

    seen: set = set()

    # Infra-specific static paths from the calling infra repo
    if args.infra_paths_file:
        infra_file = Path(args.infra_paths_file)
        if infra_file.exists():
            for line in infra_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    emit_existing(line, seen)
        else:
            sys.stderr.write(f"Infra paths file not found: {args.infra_paths_file}\n")

    prefix = docker_cmd_prefix()

    try:
        volumes = list_volume_names(prefix)
        mountpoints = inspect_mountpoints(prefix, volumes)
        for mp in sorted(mountpoints):
            emit_existing(mp, seen)
        return 0
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stderr or f"Docker command failed: {e}\n")
        return 0 if ALLOW_VOLUME_FAILURE else 1


if __name__ == "__main__":
    raise SystemExit(main())
