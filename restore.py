#!/usr/bin/env python3
import os
import sys
import json
import argparse
import subprocess
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

RESTIC_BIN = os.environ.get("RESTIC_BIN_OVERRIDE") or shutil.which("restic") or "/usr/bin/restic"
DEBUG = os.environ.get("DEBUG") == "1"

MAX_SNAPSHOT_WINDOW_MINUTES = int(os.environ.get("MAX_SNAPSHOT_WINDOW_MINUTES", "30"))
MAX_SNAPSHOT_WINDOW_HOURS = int(os.environ.get("MAX_SNAPSHOT_WINDOW_HOURS", "0"))

ENV_ALIASES = {
    "staging": ["staging", "stage"],
    "stage": ["stage", "staging"],
    "production": ["production", "prod"],
    "prod": ["prod", "production"],
}


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


def candidate_projects(app: str, env_name: str):
    envs = ENV_ALIASES.get(env_name, [env_name])
    seen = set()
    out = []
    for e in envs:
        p = f"{app}_{e}"
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def find_compose_container(project: str, service: str) -> str:
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


def get_snapshot_by_id(env: dict, snapshot_id: str):
    res = run_cmd([RESTIC_BIN, "snapshots", snapshot_id, "--json"], env=env)
    snaps = json.loads(res.stdout)
    if not snaps:
        log(f"ERROR: Snapshot '{snapshot_id}' not found.")
        sys.exit(1)
    return snaps[0]["id"], snaps[0]["time"]


def resolve_snapshot(env: dict, snapshot_arg: str):
    if snapshot_arg == "latest":
        return get_latest_snapshot(env)
    return get_snapshot_by_id(env, snapshot_arg)


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
        log("Hint: increase MAX_SNAPSHOT_WINDOW_MINUTES temporarily.")
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
            stdout=subprocess.DEVNULL,
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


def stream_restore_from_local_file(dump_file: Path, db_container_id: str, db_user: str, db_name: str):
    log(f"-> Importing dump from local file: {dump_file}")

    with dump_file.open("rb") as src:
        p_gunzip = subprocess.Popen(
            ["gzip", "-dc"],
            stdin=src,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            subprocess.run(
                ["docker", "exec", "-i", db_container_id, "psql", "-U", db_user, "-d", db_name, "-v", "ON_ERROR_STOP=1"],
                stdin=p_gunzip.stdout,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        finally:
            if p_gunzip.stdout:
                p_gunzip.stdout.close()

        gz_err = (p_gunzip.stderr.read() or b"").decode("utf-8", errors="replace").strip()
        gz_rc = p_gunzip.wait()
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


def mode_in_place(args):
    """Original same-server flow: restic snapshot -> staging DB on same host."""
    env = os.environ.copy()
    if not env.get("RESTIC_REPOSITORY") or not env.get("RESTIC_PASSWORD"):
        log("ERROR: RESTIC_REPOSITORY / RESTIC_PASSWORD not set.")
        sys.exit(1)

    if args.target_env != "staging":
        log("ERROR: --mode in-place currently only supports --target-env staging.")
        sys.exit(1)

    projects = candidate_projects(args.app, args.target_env)
    log(f"Looking for Docker services in projects: {projects} ...")

    db_container_id, matched_project = find_db_container_id(projects)
    if not db_container_id:
        log(f"ERROR: DB container not found for any of: {projects}. Is staging running?")
        sys.exit(1)

    log(f"Using compose project: {matched_project}")

    db_user, db_name = get_db_credentials(db_container_id)
    if not db_user or not db_name:
        log("ERROR: DB container is missing backup.pg.user / backup.pg.db labels.")
        sys.exit(1)

    log(f"Resolving snapshot '{args.snapshot}'...")
    snapshot_id, snap_time_str = resolve_snapshot(env, args.snapshot)
    log(f"Using Snapshot ID: {snapshot_id}")
    log(f"Snapshot Date: {snap_time_str}")

    snap_time = datetime.fromisoformat(snap_time_str.replace("Z", "+00:00"))

    all_files = list_sql_gz(env, snapshot_id)
    dump_path = choose_dump_for_db(all_files, db_name, snap_time)
    log(f"Selected dump for db '{db_name}': {dump_path}")

    reset_database(db_container_id, db_user, db_name)

    try:
        stream_restore_into_psql(env, snapshot_id, dump_path, db_container_id, db_user, db_name)
    except Exception as e:
        log(f"ERROR: Import failed: {e}")
        sys.exit(1)

    run_migrations(matched_project)

    log(f"✅ App {args.app} successfully restored to staging.")
    return 0


def mode_dump_only(args):
    """Cross-server source side: dump a snapshot's .sql.gz to a local file + manifest, no DB ops."""
    env = os.environ.copy()
    if not env.get("RESTIC_REPOSITORY") or not env.get("RESTIC_PASSWORD"):
        log("ERROR: RESTIC_REPOSITORY / RESTIC_PASSWORD not set.")
        sys.exit(1)

    if not args.source_env:
        log("ERROR: --mode dump-only requires --source-env.")
        sys.exit(1)
    if not args.output_dir:
        log("ERROR: --mode dump-only requires --output-dir.")
        sys.exit(1)

    projects = candidate_projects(args.app, args.source_env)
    log(f"Looking for source-env Docker services in projects: {projects} ...")

    db_container_id, matched_project = find_db_container_id(projects)
    if not db_container_id:
        log(f"ERROR: Source-env DB container not found for any of: {projects}. Is the source stack running?")
        sys.exit(1)

    log(f"Using compose project: {matched_project}")

    db_user, db_name = get_db_credentials(db_container_id)
    if not db_user or not db_name:
        log("ERROR: Source DB container is missing backup.pg.user / backup.pg.db labels.")
        sys.exit(1)

    log(f"Resolving snapshot '{args.snapshot}'...")
    snapshot_id, snap_time_str = resolve_snapshot(env, args.snapshot)
    log(f"Using Snapshot ID: {snapshot_id}")
    log(f"Snapshot Date: {snap_time_str}")

    snap_time = datetime.fromisoformat(snap_time_str.replace("Z", "+00:00"))

    all_files = list_sql_gz(env, snapshot_id)
    dump_path = choose_dump_for_db(all_files, db_name, snap_time)
    log(f"Selected dump for db '{db_name}': {dump_path}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / "dump.sql.gz"

    log(f"-> Dumping snapshot file -> {out_file}")
    with out_file.open("wb") as fh:
        proc = subprocess.run(
            [RESTIC_BIN, "dump", snapshot_id, dump_path],
            env=env,
            stdout=fh,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        log(f"ERROR: restic dump failed: {err}")
        sys.exit(1)

    try:
        os.chmod(out_file, 0o600)
    except OSError:
        pass

    manifest = {
        "app": args.app,
        "source_env": args.source_env,
        "source_project": matched_project,
        "source_db_name": db_name,
        "source_db_user": db_user,
        "snapshot_id": snapshot_id,
        "snapshot_time": snap_time_str,
        "dump_path_in_snapshot": dump_path,
        "dump_file": out_file.name,
    }
    manifest_file = output_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2))
    try:
        os.chmod(manifest_file, 0o600)
    except OSError:
        pass

    log(f"✅ dump-only complete: {out_file} (manifest: {manifest_file})")
    return 0


def mode_import_only(args):
    """Cross-server dest side: import a local .sql.gz into the dest app's DB. No restic ops."""
    if not args.target_env:
        log("ERROR: --mode import-only requires --target-env.")
        sys.exit(1)
    if not args.dump_file:
        log("ERROR: --mode import-only requires --dump-file.")
        sys.exit(1)

    dump_file = Path(args.dump_file)
    if not dump_file.is_file():
        log(f"ERROR: Dump file not found: {dump_file}")
        sys.exit(1)

    projects = candidate_projects(args.app, args.target_env)
    log(f"Looking for target-env Docker services in projects: {projects} ...")

    db_container_id, matched_project = find_db_container_id(projects)
    if not db_container_id:
        log(f"ERROR: Target-env DB container not found for any of: {projects}. Is the dest stack running?")
        sys.exit(1)

    log(f"Using compose project: {matched_project}")

    db_user, db_name = get_db_credentials(db_container_id)
    if not db_user or not db_name:
        log("ERROR: Target DB container is missing backup.pg.user / backup.pg.db labels.")
        sys.exit(1)

    reset_database(db_container_id, db_user, db_name)

    try:
        stream_restore_from_local_file(dump_file, db_container_id, db_user, db_name)
    except Exception as e:
        log(f"ERROR: Import failed: {e}")
        sys.exit(1)

    run_migrations(matched_project)

    log(f"✅ App {args.app} successfully restored from local dump into {args.target_env}.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Restore an app DB from a Restic snapshot. Supports same-host (in-place) and cross-host (dump-only / import-only) flows.",
    )
    parser.add_argument("--app", required=True)
    parser.add_argument(
        "--mode",
        default="in-place",
        choices=["in-place", "dump-only", "import-only"],
        help="in-place: original same-host flow (default). dump-only: extract snapshot to local file (source side of cross-server). import-only: import a local dump (dest side of cross-server).",
    )
    parser.add_argument("--target-env", help="Required for in-place and import-only.")
    parser.add_argument("--source-env", help="Required for dump-only (e.g. production).")
    parser.add_argument("--snapshot", default="latest", help="Used in in-place and dump-only modes.")
    parser.add_argument("--output-dir", help="Required for dump-only — where to write dump.sql.gz + manifest.json.")
    parser.add_argument("--dump-file", help="Required for import-only — path to a local .sql.gz produced by dump-only.")
    args = parser.parse_args()

    if args.mode == "in-place":
        if not args.target_env:
            log("ERROR: --mode in-place requires --target-env.")
            sys.exit(1)
        return mode_in_place(args)
    if args.mode == "dump-only":
        return mode_dump_only(args)
    if args.mode == "import-only":
        return mode_import_only(args)

    log(f"ERROR: Unknown mode '{args.mode}'.")
    sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(main())
