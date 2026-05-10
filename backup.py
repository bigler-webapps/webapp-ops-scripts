#!/usr/bin/env python3
"""
backup.py

- Generates the include list (paths.txt) via generate_paths.py
- Creates gzip-compressed Postgres dumps from containers labelled backup.enable=true
- Runs restic backup to local repo and B2 repo
- Keeps logs short: summary on success, full output only on errors (or DEBUG=1)
"""

import os
import sys
import re
import json
import gzip
import shutil
import datetime
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- Environment / Config ---
RESTIC_REPO_LOCAL = os.environ.get("RESTIC_REPO_LOCAL")
RESTIC_REPO_B2 = os.environ.get("RESTIC_REPO_B2")
RESTIC_PASSWORD = os.environ.get("RESTIC_PASSWORD")

# Scripts live in the same directory as this file (webapp-ops-scripts/)
SCRIPTS_DIR = Path(__file__).parent

# infra_paths.txt is bundled alongside the scripts by the backup workflow
INFRA_PATHS_FILE = Path(os.environ.get("INFRA_PATHS_FILE", str(SCRIPTS_DIR / "infra_paths.txt")))

DUMP_DIR = Path("/srv/backups/db-dumps")
PATHS_FILE = Path("/tmp/restic_backup_paths.txt")
GENERATE_PATHS_SCRIPT = SCRIPTS_DIR / "generate_paths.py"

RESTIC_BIN = os.environ.get("RESTIC_BIN_OVERRIDE") or shutil.which("restic") or "/usr/bin/restic"
DEBUG = os.environ.get("DEBUG") == "1"

# Retention policy
KEEP_DAILY = "7"
KEEP_WEEKLY = "4"
KEEP_MONTHLY = "6"

_SNAP_RE = re.compile(r"snapshot\s+([0-9a-f]{8,})\s+saved", re.IGNORECASE)

DOCKER_CMD: List[str] = []


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def fail(msg: str) -> None:
    log(f"CRITICAL ERROR: {msg}")
    sys.exit(1)


def run_cmd(
    cmd: List[str],
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    show_on_success: bool = False,
) -> Tuple[bool, str, str]:
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    if DEBUG:
        log(f"DEBUG CMD: {' '.join(cmd)}")

    res = subprocess.run(cmd, capture_output=True, text=True, env=proc_env, cwd=cwd)

    if res.returncode != 0:
        log(f"ERROR executing: {' '.join(cmd)} (exit={res.returncode})")
        if res.stderr.strip():
            print("--- STDERR ---")
            print(res.stderr)
        if res.stdout.strip():
            print("--- STDOUT ---")
            print(res.stdout)
        return False, res.stdout, res.stderr

    if DEBUG or show_on_success:
        if res.stdout.strip():
            print(res.stdout)
        if res.stderr.strip():
            print(res.stderr)

    return True, res.stdout, res.stderr


def determine_docker_command() -> List[str]:
    ok, _, _ = run_cmd(["docker", "info"])
    if ok:
        return ["docker"]

    ok, _, _ = run_cmd(["sudo", "-n", "docker", "info"])
    if ok:
        return ["sudo", "-n", "docker"]

    fail("Docker not accessible via 'docker' or 'sudo -n docker'. Check permissions.")
    return []


def ensure_restic_available() -> None:
    if shutil.which("restic"):
        return
    if Path(RESTIC_BIN).exists():
        return

    fail(
        "Restic is not installed or not available in PATH. "
        "Install 'restic' on the server or set RESTIC_BIN_OVERRIDE to a valid binary."
    )


def generate_paths_file() -> None:
    if not GENERATE_PATHS_SCRIPT.exists():
        fail(f"Missing paths generator: {GENERATE_PATHS_SCRIPT}")

    ok, out, err = run_cmd(
        ["python3", str(GENERATE_PATHS_SCRIPT), "--infra-paths-file", str(INFRA_PATHS_FILE)]
    )
    if not ok:
        fail("generate_paths.py failed")

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    with open(PATHS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    log(f"Paths file generated: {len(lines)} entries -> {PATHS_FILE}")

    if err.strip():
        if DEBUG:
            log("DEBUG: generate_paths.py stderr:")
            print(err)
        else:
            warn_lines = [ln for ln in err.splitlines() if ln.strip()]
            log(f"WARN: generate_paths.py reported {len(warn_lines)} warnings (set DEBUG=1 to see all).")


def discover_docker_targets() -> List[Dict[str, str]]:
    log("Scanning Docker for dump targets...")

    cmd = DOCKER_CMD + ["ps", "-q", "-f", "label=backup.enable=true"]
    res = subprocess.run(cmd, capture_output=True, text=True)

    if res.returncode != 0:
        fail(f"Docker scan failed: {res.stderr.strip()}")

    container_ids = res.stdout.strip().split()
    if not container_ids:
        fail("No containers found with label 'backup.enable=true'. Check deployments and labels.")

    cmd_inspect = DOCKER_CMD + ["inspect"] + container_ids
    res_inspect = subprocess.run(cmd_inspect, capture_output=True, text=True)
    if res_inspect.returncode != 0:
        fail(f"Docker inspect failed: {res_inspect.stderr.strip()}")

    data = json.loads(res_inspect.stdout)
    targets: List[Dict[str, str]] = []

    for container in data:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        c_name = (container.get("Name") or "").strip("/")
        db_user = labels.get("backup.pg.user")
        db_name = labels.get("backup.pg.db")

        if DEBUG:
            log(f"DEBUG labels {c_name}: backup.pg.user={db_user!r}, backup.pg.db={db_name!r}")

        if db_user and db_name:
            targets.append({"id": container["Id"][:12], "name": c_name, "user": db_user, "db": db_name})
        else:
            log(f"WARN: {c_name} missing DB labels (backup.pg.user/backup.pg.db) or empty.")

    if not targets:
        fail("Found backup.enable=true containers, but none had valid DB labels.")

    return targets


def perform_db_dumps() -> None:
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=2)).timestamp()
    for f in DUMP_DIR.glob("*.sql.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()

    try:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DUMP_DIR, 0o700)
    except PermissionError as exc:
        fail(f"Cannot prepare dump directory '{DUMP_DIR}'. ({exc})")
    except FileNotFoundError as exc:
        fail(f"Cannot prepare dump directory '{DUMP_DIR}'. Ensure /srv/backups exists. ({exc})")

    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%SZ")
    targets = discover_docker_targets()

    log(f"Dump targets: {len(targets)}")
    success_count = 0

    for t in targets:
        safe_name = t["name"].replace("@", "_").replace("/", "_")
        outfile = DUMP_DIR / f"{safe_name}_{t['db']}_{timestamp}.sql.gz"
        tmpfile = outfile.with_suffix(outfile.suffix + ".tmp")

        docker_dump_cmd = DOCKER_CMD + ["exec", "-i", t["id"], "pg_dump", "-U", t["user"], t["db"]]

        try:
            with tempfile.TemporaryFile() as errf:
                p = subprocess.Popen(docker_dump_cmd, stdout=subprocess.PIPE, stderr=errf)

                try:
                    if p.stdout is None:
                        raise RuntimeError("pg_dump stdout pipe not available")

                    with gzip.open(tmpfile, "wb") as gz:
                        shutil.copyfileobj(p.stdout, gz)

                finally:
                    if p.stdout:
                        p.stdout.close()

                rc = p.wait()
                if rc != 0:
                    errf.seek(0)
                    err = errf.read().decode("utf-8", errors="replace")
                    raise subprocess.CalledProcessError(rc, docker_dump_cmd, stderr=err)

            ok, _, stderr = run_cmd(["gzip", "-t", str(tmpfile)])
            if not ok:
                raise RuntimeError(f"gzip integrity check failed: {stderr.strip()}")

            os.replace(tmpfile, outfile)
            os.chmod(outfile, 0o600)

            size_kib = outfile.stat().st_size // 1024
            log(f"Dump OK: {t['name']} / {t['db']} ({size_kib} KiB)")
            success_count += 1

        except Exception as e:
            log(f"Dump FAILED: {t['name']} / {t['db']}")
            if isinstance(e, subprocess.CalledProcessError) and getattr(e, "stderr", None):
                print(e.stderr)
            else:
                print(str(e))

            if tmpfile.exists():
                tmpfile.unlink()
            if outfile.exists():
                outfile.unlink()

            fail("Database dump failed, aborting backup.")

    if success_count != len(targets):
        fail(f"Not all databases dumped successfully ({success_count}/{len(targets)}).")

    log(f"Dumps OK: {success_count}/{len(targets)}")


def restic_repo_ready(env: Dict[str, str], repo_name: str) -> None:
    if not RESTIC_PASSWORD:
        fail("RESTIC_PASSWORD missing")

    ok, _, _ = run_cmd([RESTIC_BIN, "snapshots", "--latest", "1"], env=env)
    if ok:
        return

    log(f"{repo_name}: repo not accessible, trying init...")
    ok_init, _, _ = run_cmd([RESTIC_BIN, "init"], env=env)
    if not ok_init:
        fail(f"{repo_name}: restic init failed")


def run_restic(repo_url: Optional[str], repo_name: str) -> Tuple[bool, Optional[str]]:
    if not repo_url:
        log(f"{repo_name}: SKIP (no repo url)")
        return True, None

    if not RESTIC_PASSWORD:
        fail(f"{repo_name}: RESTIC_PASSWORD missing")

    env = {"RESTIC_REPOSITORY": repo_url}

    restic_repo_ready(env, repo_name)

    backup_cmd = [
        RESTIC_BIN,
        "backup",
        "--host",
        os.uname().nodename,
        "--tag",
        "daily",
        "--files-from",
        str(PATHS_FILE),
        str(DUMP_DIR),
    ]

    ok_b, out_b, _ = run_cmd(backup_cmd, env=env)
    if not ok_b:
        return False, None

    snap_id = None
    m = _SNAP_RE.search(out_b or "")
    if m:
        snap_id = m.group(1)

    log(f"{repo_name}: backup OK (snapshot={snap_id or 'unknown'})")

    forget_cmd = [
        RESTIC_BIN,
        "--quiet",
        "forget",
        "--prune",
        "--keep-daily",
        KEEP_DAILY,
        "--keep-weekly",
        KEEP_WEEKLY,
        "--keep-monthly",
        KEEP_MONTHLY,
    ]

    ok_p, _, _ = run_cmd(forget_cmd, env=env)
    if not ok_p:
        return False, snap_id

    log(f"{repo_name}: retention OK")
    return True, snap_id


def main() -> None:
    global DOCKER_CMD

    log("== Backup Start ==")

    DOCKER_CMD = determine_docker_command()
    ensure_restic_available()

    generate_paths_file()
    perform_db_dumps()

    ok_local, snap_local = run_restic(RESTIC_REPO_LOCAL, "Local")
    ok_b2, snap_b2 = run_restic(RESTIC_REPO_B2, "B2")

    if not ok_local or not ok_b2:
        fail("Restic backup failed for one or more repositories.")

    log(f"SUMMARY: dumps ok, local={snap_local or 'n/a'}, b2={snap_b2 or 'n/a'}")
    log("== Backup Success ==")


if __name__ == "__main__":
    main()
