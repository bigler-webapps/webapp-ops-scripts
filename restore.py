#!/usr/bin/env python3
import os
import sys
import json
import argparse
import subprocess
import shutil
import signal
import tempfile
import time
import uuid
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stack_recovery

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


def require_known_env(env_name: str, flag: str) -> None:
    """Reject an environment name that is not one of the known ones (INF-33 S1).

    The compose project is built as `f"{app}_{env}"`, and the separator is an
    ordinary underscore, so the boundary is ambiguous while `env` is free text:
    `--app foo --target-env bar_staging` and `--app foo_bar --target-env staging`
    resolve to the same project. Constraining the env side removes the second
    spelling.

    This closes the AMBIGUITY, not the whole finding. A caller can still name a
    different application directly (`--app other_app --target-env staging`).
    Preventing that needs an allowlist of valid app names, which this script has
    no source for -- it belongs at the boundary that already knows the registry.
    """
    if env_name not in ENV_ALIASES:
        known = ", ".join(sorted(ENV_ALIASES))
        log(f"ERROR: {flag} must be one of: {known} (got '{env_name}').")
        sys.exit(1)


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


def resolve_target_database(app: str, target_env: str):
    """Resolve the local, labelled destination DB after receiver authorization."""
    require_known_env(target_env, "--target-env")
    projects = candidate_projects(app, target_env)
    container_id, project = find_db_container_id(projects)
    if not container_id:
        raise RuntimeError("authorized destination database was not found")
    db_user, db_name = get_db_credentials(container_id)
    if not db_user or not db_name:
        raise RuntimeError("destination database labels are incomplete")
    return project, container_id, db_user, db_name


class ContainerStartWatcher:
    """Collects the container create/start events of one compose project while it runs.

    The daemon's own event history is a 256 entry ring buffer, and on a host with
    healthchecks that is a quarter of an hour at best, shorter than an import. A
    listener started before the window covers all of it, and it sees a container
    that was started and removed again (`docker compose run --rm`), which a
    comparison of container states cannot.

    Best effort: if the listener cannot be started or dies, that is reported once
    and the state comparison stands on its own.
    """

    def __init__(self, project: str):
        self._project = project
        self._out = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._proc = None
        self._warned = False
        try:
            self._proc = subprocess.Popen(
                [
                    "docker", "events",
                    "--since", str(int(time.time())),
                    "--filter", f"label=com.docker.compose.project={project}",
                    "--filter", "type=container",
                    "--filter", "event=create", "--filter", "event=start",
                    "--format", "{{.Actor.ID}} {{.Actor.Attributes.name}}",
                ],
                stdout=self._out,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            self._warn(f"could not start the Docker events listener: {exc}")

    def _warn(self, reason: str) -> None:
        if not self._warned:
            self._warned = True
            log(f"WARNING: {reason}; checking container state only for '{self._project}'.")

    def started(self) -> list:
        """(container id, name) for every create/start seen since the watcher began."""
        if self._proc is None:
            return []
        if self._proc.poll() is not None and self._proc.returncode != 0:
            self._warn("the Docker events listener exited early")
            return []
        self._out.flush()
        self._out.seek(0)
        seen = []
        for line in self._out.read().splitlines():
            cid, _, name = line.strip().partition(" ")
            if cid:
                seen.append((cid, name))
        return seen

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._out.close()


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


def _psql(tx, db_container_id: str, *args):
    if tx is None:
        return run_cmd(["docker", "exec", db_container_id, "psql", *args])
    child = tx.exec_psql(db_container_id, *args, stdout=subprocess.DEVNULL)
    if child.wait() != 0:
        raise RuntimeError("database command failed")


def reset_database(db_container_id: str, db_user: str, db_name: str, tx=None):
    log(f"-> Resetting DB '{db_name}' (drop + create)...")
    drop_sql = f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);'
    create_sql = f'CREATE DATABASE "{db_name}";'
    _psql(tx, db_container_id, "-U", db_user, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", drop_sql)
    _psql(tx, db_container_id, "-U", db_user, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", create_sql)


def assert_schema_healthy(db_container_id: str, db_user: str, db_name: str, tx=None):
    """Fail when a restored public base table has no primary key."""
    if tx is not None:
        tx.update_phase("checks", "running")
    missing_primary_keys_sql = """
        SELECT format('%I.%I', t.table_schema, t.table_name)
        FROM information_schema.tables AS t
        WHERE t.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
          AND NOT EXISTS (
              SELECT 1
              FROM information_schema.table_constraints AS tc
              WHERE tc.table_schema = t.table_schema
                AND tc.table_name = t.table_name
                AND tc.constraint_type = 'PRIMARY KEY'
          )
        ORDER BY t.table_schema, t.table_name;
    """
    command = [
        "docker", "exec", db_container_id,
        "psql", "-U", db_user, "-d", db_name,
        "-v", "ON_ERROR_STOP=1", "-Atc", missing_primary_keys_sql,
    ]
    try:
        if tx is None:
            result = run_cmd(command)
        else:
            proc = tx.exec_psql(db_container_id, "-U", db_user, "-d", db_name,
                                "-v", "ON_ERROR_STOP=1", "-Atc", missing_primary_keys_sql,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate()
            if proc.returncode:
                raise RuntimeError("schema health check failed")
            result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    except BaseException:
        if tx is not None:
            with contextlib.suppress(Exception):
                tx.update_phase("checks", "failed", error_code="schema-check-failed")
        raise
    missing_tables = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if missing_tables:
        sample = missing_tables[:10]
        suffix = f" (+{len(missing_tables) - len(sample)} more)" if len(missing_tables) > len(sample) else ""
        log(
            "ERROR: Schema assertion failed: "
            f"{len(missing_tables)} public base table(s) have no primary key: "
            + ", ".join(sample) + suffix
        )
        if tx is not None:
            tx.update_phase("checks", "failed", error_code="schema-assertion-failed")
        sys.exit(1)

    if tx is not None:
        tx.update_phase("checks", "completed")
    log("-> Schema assertion passed: every public base table has a primary key.")


def stream_restore_into_psql(env: dict, snapshot_id: str, dump_path: str, db_container_id: str, db_user: str, db_name: str, tx=None):
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
        if tx is None:
            subprocess.run(
                ["docker", "exec", "-i", db_container_id, "psql", "-U", db_user, "-d", db_name, "-v", "ON_ERROR_STOP=1"],
                stdin=p_gunzip.stdout, check=True, stdout=subprocess.DEVNULL,
            )
        else:
            child = tx.exec_psql(db_container_id, "-i", "-U", db_user, "-d", db_name,
                                 "-v", "ON_ERROR_STOP=1", stdin=p_gunzip.stdout,
                                 stdout=subprocess.DEVNULL)
            if child.wait() != 0:
                raise RuntimeError("database import command failed")
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


def stream_restore_from_local_file(dump_file: Path, db_container_id: str, db_user: str, db_name: str, tx=None):
    log(f"-> Importing dump from local file: {dump_file}")

    with dump_file.open("rb") as src:
        p_gunzip = subprocess.Popen(
            ["gzip", "-dc"],
            stdin=src,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            command = ["docker", "exec", "-i", db_container_id, "psql", "-U", db_user, "-d", db_name, "-v", "ON_ERROR_STOP=1"]
            if tx is None:
                subprocess.run(command, stdin=p_gunzip.stdout, check=True, stdout=subprocess.DEVNULL)
            else:
                child = tx.exec_psql(db_container_id, "-i", "-U", db_user, "-d", db_name,
                                     "-v", "ON_ERROR_STOP=1", stdin=p_gunzip.stdout,
                                     stdout=subprocess.DEVNULL)
                if child.wait() != 0:
                    raise RuntimeError("database import command failed")
        finally:
            if p_gunzip.stdout:
                p_gunzip.stdout.close()

        gz_err = (p_gunzip.stderr.read() or b"").decode("utf-8", errors="replace").strip()
        gz_rc = p_gunzip.wait()
        if gz_rc != 0:
            raise RuntimeError(f"gunzip failed: {gz_err}")


def run_migrations(project: str, tx=None):
    log("-> Running migrations...")
    if tx is None:
        be_id = find_compose_container(project, "backend")
        if not be_id:
            raise RuntimeError("backend container not found; required migrations were not run")
        subprocess.run(["docker", "exec", "-i", be_id, "python", "manage.py", "migrate", "--noinput"],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        tx.update_phase("migration", "running")
        try:
            child = tx.run_migrations(stdout=subprocess.DEVNULL)
            if child.wait() != 0:
                raise RuntimeError("database migration command failed")
        except BaseException:
            with contextlib.suppress(Exception):
                tx.update_phase("migration", "failed", error_code="migration-failed")
            raise
        tx.update_phase("migration", "completed")
    log("✅ Migration successful.")


def import_database(tx, dump_file: Path, db_container_id: str, db_user: str, db_name: str) -> None:
    """Database-only recovery phase for framed receivers; caller owns later phases."""
    tx.mark_database_started()
    reset_database(db_container_id, db_user, db_name, tx=tx)
    stream_restore_from_local_file(dump_file, db_container_id, db_user, db_name, tx=tx)
    tx.update_phase("database", "completed")


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

    operation_id = getattr(args, "operation_id", None) or uuid.uuid4().hex
    tx_context = stack_recovery.RecoveryTransaction(
        matched_project, args.app, operation_id, getattr(args, "recovery_source", None) or "restic",
        expected_final="preserve", auto_restart=True,
        origin_run_id=getattr(args, "origin_run_id", None),
        origin_attempt=getattr(args, "origin_attempt", None),
    )
    try:
        with tx_context as tx:
            if tx.already_completed:
                log(stack_recovery.recovery_result(tx.record))
                return 0
            tx.mark_database_started()
            reset_database(db_container_id, db_user, db_name, tx=tx)
            stream_restore_into_psql(env, snapshot_id, dump_path, db_container_id, db_user, db_name, tx=tx)
            tx.update_phase("database", "completed")
            run_migrations(matched_project, tx=tx)
            assert_schema_healthy(db_container_id, db_user, db_name, tx=tx)
            tx.complete(artifacts="not_applicable", migration="completed", checks="completed")
            log(stack_recovery.recovery_result(tx.record))
    except Exception as e:
        log(f"ERROR: Import failed ({type(e).__name__}).")
        if getattr(tx_context, "record", None) is not None:
            log(stack_recovery.recovery_result(tx_context.record))
        sys.exit(1)

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
    require_known_env(args.source_env, "--source-env")
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
    require_known_env(args.target_env, "--target-env")
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

    operation_id = getattr(args, "operation_id", None) or uuid.uuid4().hex
    recovery_source = getattr(args, "recovery_source", None) or "manual"
    auto_restart = bool(getattr(args, "auto_restart", False))
    tx_context = stack_recovery.RecoveryTransaction(
        matched_project, args.app, operation_id, recovery_source,
        expected_final="preserve" if auto_restart else "stopped",
        auto_restart=auto_restart,
        restart_left_stopped=bool(getattr(args, "restart_left_stopped", False)),
        origin_run_id=getattr(args, "origin_run_id", None),
        origin_attempt=getattr(args, "origin_attempt", None),
    )
    try:
        with tx_context as tx:
            if tx.already_completed:
                log(stack_recovery.recovery_result(tx.record))
                return 0
            import_database(tx, dump_file, db_container_id, db_user, db_name)
            run_migrations(matched_project, tx=tx)
            assert_schema_healthy(db_container_id, db_user, db_name, tx=tx)
            tx.complete(artifacts="not_applicable", migration="completed", checks="completed")
            log(stack_recovery.recovery_result(tx.record))
    except Exception as e:
        log(f"ERROR: Import failed ({type(e).__name__}).")
        if getattr(tx_context, "record", None) is not None:
            log(stack_recovery.recovery_result(tx_context.record))
        sys.exit(1)

    log(f"App {args.app} database restore completed for {args.target_env}; observed application state: {tx_context.record.get('application', 'unknown')}.")
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
    parser.add_argument("--operation-id", help="Stable recovery correlation ID supplied by an authorized retry.")
    parser.add_argument("--recovery-source", help="Bound source identity supplied by an authorized receiver.")
    parser.add_argument("--origin-run-id", help="Stable originating workflow run ID for repair evidence.")
    parser.add_argument("--origin-attempt", help="Originating workflow attempt number for repair evidence.")
    parser.add_argument("--auto-restart", action="store_true", help="Explicitly restore eligible application services after all checks pass.")
    parser.add_argument("--restart-left-stopped", action="store_true", help="Also restart previously stopped non-one-shot services after successful recovery.")
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
