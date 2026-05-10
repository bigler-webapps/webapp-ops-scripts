#!/usr/bin/env python3
import os
import sys
import json
import argparse
import subprocess
import shutil
from datetime import datetime, timedelta, timezone

RESTIC_BIN = os.environ.get("RESTIC_BIN_OVERRIDE") or shutil.which("restic") or "/usr/bin/restic"
DEBUG = os.environ.get("DEBUG") == "1"

# Window: prefer minutes, fallback to hours
MAX_SNAPSHOT_WINDOW_MINUTES = int(os.environ.get("MAX_SNAPSHOT_WINDOW_MINUTES", "30"))
MAX_SNAPSHOT_WINDOW_HOURS = int(os.environ.get("MAX_SNAPSHOT_WINDOW_HOURS", "0"))

def log(msg: str) -> None:
    print(f"[RESTORE] {msg}")

def run_cmd(cmd, env=None, check=True):
    if DEBUG:
        log(f"DEBUG CMD: {' '.join(cmd)}")
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and res.returncode != 0:
        print(f"ERROR executing {' '.join(cmd)}")
        if res.stderr.strip():
            print(res.stderr)
        if res.stdout.strip():
            print(res.stdout)
        sys.exit(1)
    return res

def candidate_projects(app: str, target_env: str):
    envs = [target_env]
    if target_env == "staging":
        envs.append("stage")
    elif target_env == "stage":
        envs.append("staging")

    seen = set()
    out = []
    for e in envs:
        p = f"{app}_{e}"
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def find_compose_container(project: str, service: str) -> str:
    # Prefer labels
    res = subprocess.run(
        ["docker", "ps", "-q",
         "-f", f"label=com.docker.compose.project={project}",
         "-f", f"label=com.docker.compose.service={service}"],
        capture_output=True,
        text=True,
    )
    ids = [x for x in res.stdout.splitlines() if x.strip()]
    if ids:
        return ids[0].strip()

    # Fallback: container name
    res = subprocess.run(["docker", "ps", "-q", "-f", f"name={project}_{service}"], capture_output=True, text=True)
    return res.stdout.strip()

def find_db_container_id(project_candidates):
    for project in project_candidates:
        cid = find_compose_container(project, "db")
        if cid:
            return cid, project
    return "", ""

def get_db_credentials(container_id: str):
    res = run_cmd(["docker", "inspect", container_id], check=True)
    data = json.loads(res.stdout)[0]
    labels = data.get("Config", {}).get("Labels", {}) or {}
    return labels.get("backup.pg.user"), labels.get("backup.pg.db")

def get_latest_snapshot(env: dict):
    snaps = json.loads(run_cmd([RESTIC_BIN, "snapshots", "--latest", "1", "--json"], env=env).stdout)
    if not snaps:
        log("ERROR: No snapshots found.")
        sys.exit(1)
    snaps.sort(key=lambda s: s["time"], reverse=True)
    return snaps[0]["id"], snaps[0]["time"]

def list_sql_gz(env: dict, snapshot_id: str):
    out = run_cmd([RESTIC_BIN, "ls", snapshot_id], env=env).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip().endswith(".sql.gz")]

def parse_ts_from_path(path: str):
    base = os.path.basename(path)
    if base.endswith(".sql.gz"):
        base = base[:-7]
    parts = base.split("_")
    if not parts:
        return None
    ts = parts[-1]
    for fmt in ("%Y-%m-%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def snapshot_window(snap_time: datetime):
    if MAX_SNAPSHOT_WINDOW_MINUTES > 0:
        w = timedelta(minutes=MAX_SNAPSHOT_WINDOW_MINUTES)
    else:
        w = timedelta(hours=max(MAX_SNAPSHOT_WINDOW_HOURS, 1))
    return (snap_time - w, snap_time + timedelta(minutes=5))

def choose_dump_for_db(all_files, db_name: str, snap_time: datetime):
    min_time, max_time = snapshot_window(snap_time)

    # We match by suffix: _{db_name}_{timestamp}.sql.gz
    # Your files look like: jg_prod_db_jg-dg_2026-...Z.sql.gz  -> db_name = jg-dg
    suffix = f"_{db_name}_"

    candidates = []
    for f in all_files:
        if suffix not in os.path.basename(f):
            continue
        ts = parse_ts_from_path(f)
        if ts is None or not (min_time <= ts <= max_time):
            continue
        candidates.append((ts, f))

    if not candidates:
        log(f"ERROR: No dump found for db '{db_name}' within snapshot window.")
        log(f"Hint: increase MAX_SNAPSHOT_WINDOW_MINUTES temporarily.")
        sys.exit(1)

    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]

def reset_database(db_container_id: str, db_user: str, db_name: str):
    log(f"-> Resetting DB '{db_name}' (drop + create)...")
    drop_sql = f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);'
    create_sql = f'CREATE DATABASE "{db_name}";'
    run_cmd(["docker", "exec", db_container_id, "psql", "-U", db_user, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", drop_sql])
    run_cmd(["docker", "exec", db_container_id, "psql", "-U", db_user, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", create_sql])

def stream_restore_into_psql(env: dict, snapshot_id: str, dump_path: str, db_container_id: str, db_user: str, db_name: str):
    log("-> Importing dump (streamed from restic)...")

    p_dump = subprocess.Popen(
        [RESTIC_BIN, "dump", snapshot_id, dump_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    p_gunzip = subprocess.Popen(
        ["gzip", "-dc"],
        stdin=p_dump.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        subprocess.run(
            ["docker", "exec", "-i", db_container_id, "psql", "-U", db_user, "-d", db_name, "-v", "ON_ERROR_STOP=1"],
            stdin=p_gunzip.stdout,
            check=True,
            stdout=subprocess.DEVNULL,   # <- keeps logs short
        )
    finally:
        if p_dump.stdout:
            p_dump.stdout.close()
        if p_gunzip.stdout:
            p_gunzip.stdout.close()

    dump_err = (p_dump.stderr.read() or b"").decode("utf-8", errors="replace").strip()
    gz_err = (p_gunzip.stderr.read() or b"").decode("utf-8", errors="replace").strip()
    dump_rc = p_dump.wait()
    gz_rc = p_gunzip.wait()

    if dump_rc != 0:
        raise RuntimeError(f"restic dump failed: {dump_err}")
    if gz_rc != 0:
        raise RuntimeError(f"gunzip failed: {gz_err}")

def run_migrations(project: str):
    be_id = find_compose_container(project, "backend")
    if not be_id:
        log("⚠️  Backend container not found. Skipping migrations.")
        return
    log("-> Running migrations...")
    subprocess.run(["docker", "exec", "-i", be_id, "python", "manage.py", "migrate", "--noinput"], check=True, stdout=subprocess.DEVNULL)
    log("✅ Migration successful.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--target-env", required=True, choices=["staging"])
    parser.add_argument("--snapshot", default="latest")
    args = parser.parse_args()

    env = os.environ.copy()
    if not env.get("RESTIC_REPOSITORY") or not env.get("RESTIC_PASSWORD"):
        log("ERROR: RESTIC_REPOSITORY / RESTIC_PASSWORD not set.")
        sys.exit(1)

    # 1) Find staging DB container first (so we can read db_name)
    projects = candidate_projects(args.app, args.target_env)
    log(f"Looking for Docker services in projects: {projects} ...")

    db_container_id, matched_project = find_db_container_id(projects)
    if not db_container_id:
        log(f"ERROR: DB container not found for any of: {projects}. Is staging running?")
        sys.exit(1)

    project = matched_project
    log(f"Using compose project: {project}")

    db_user, db_name = get_db_credentials(db_container_id)
    if not db_user or not db_name:
        log("ERROR: DB container is missing backup.pg.user / backup.pg.db labels.")
        sys.exit(1)

    # 2) Get latest snapshot
    log(f"Resolving snapshot '{args.snapshot}'...")
    snapshot_id, snap_time_str = get_latest_snapshot(env)
    log(f"Using Snapshot ID: {snapshot_id}")
    log(f"Snapshot Date: {snap_time_str}")

    snap_time = datetime.fromisoformat(snap_time_str.replace("Z", "+00:00"))

    # 3) Choose dump by db_name (robust, no app patterns)
    all_files = list_sql_gz(env, snapshot_id)
    dump_path = choose_dump_for_db(all_files, db_name, snap_time)
    log(f"Selected dump for db '{db_name}': {dump_path}")

    # 4) Reset + import + migrate
    reset_database(db_container_id, db_user, db_name)

    try:
        stream_restore_into_psql(env, snapshot_id, dump_path, db_container_id, db_user, db_name)
    except Exception as e:
        log(f"ERROR: Import failed: {e}")
        sys.exit(1)

    run_migrations(project)

    log(f"✅ App {args.app} successfully restored to staging.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
