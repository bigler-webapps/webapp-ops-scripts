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
from contextlib import contextmanager
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


@contextmanager
def application_services_stopped(
    project: str,
    db_container_id: str,
    restart_on_error: bool = True,
    start_left_stopped: bool = False,
):
    """Stop the running non-database containers in one compose project.

    Three properties this must hold, each of them a review finding:

    * For modes that restart, the restart survives every exit path (INF-33 R1). SIGTERM is the realistic
      one for a CI-invoked script -- Python's default disposition terminates
      without unwinding, so `finally` would never run and the application would
      stay down. A handler that raises turns it into a normal unwind. SIGKILL
      remains uncatchable; nothing can cover that.
    * A failing restart must not eat the original error (INF-33 R2/S2).
      `run_cmd` exits via `sys.exit`, i.e. `SystemExit`, which is NOT caught by
      the callers' `except Exception` -- a raise from inside `finally` would
      replace the real failure and suppress its message entirely. The restart is
      therefore best-effort and loud.
    * Finding nothing is not the same as there being nothing (INF-33 R3).
      `find_compose_container` falls back to a name match when the compose label
      is absent, so a label-only lookup that comes back empty may simply be
      blind. Deliberately NOT solved by adding that fallback here: `docker ps -f
      name=` is a substring match and would let one project's stop reach
      another's containers. Disagreement is reported instead of papered over.

    `restart_on_error=False` leaves import-only failures stopped and asks for
    the refresh repair; the default preserves the in-place restart behavior.
    `start_left_stopped=True` makes a successful run also start the services an
    earlier failed import left stopped (restart policy other than "no"), so the
    next import repairs the application instead of leaving it down.
    The helper snapshots every labelled project container before stopping and
    checks running state and StartedAt after a normal body exit, and reads the
    Docker events for any create or start in the window. Together they detect a
    start during the window, including a container that started and exited, or
    started and was removed again.
    """
    def _container_states():
        """State of every labelled container, running or not, keyed by the SHORT id.

        `docker ps -q` (which is where `db_container_id` comes from) prints the 12
        character id while `docker inspect` reports the full one, so the inspected
        containers are mapped back onto the short ids; keying by the full id would
        never match the database container.
        """
        result = run_cmd([
            "docker", "ps", "-a", "-q",
            "-f", f"label=com.docker.compose.project={project}",
        ])
        ids = [cid.strip() for cid in result.stdout.splitlines() if cid.strip()]
        if not ids:
            return {}
        inspected = json.loads(run_cmd(["docker", "inspect", *ids]).stdout)
        states = {}
        for item in inspected:
            full_id = item.get("Id") or ""
            short_id = next((cid for cid in ids if full_id.startswith(cid)), None)
            if short_id is None:
                continue
            states[short_id] = {
                "name": (item.get("Name") or full_id or "unknown").lstrip("/"),
                "running": bool(item.get("State", {}).get("Running")),
                "started_at": item.get("State", {}).get("StartedAt"),
                "restart_policy": (
                    (item.get("HostConfig") or {}).get("RestartPolicy", {}).get("Name") or "no"
                ),
            }
        return states

    baseline_all = _container_states()
    if db_container_id and db_container_id not in baseline_all:
        log(
            f"ERROR: The database container was resolved for compose project "
            f"'{project}', but a label lookup for that project returns no such "
            f"container. Application containers cannot be identified reliably, so "
            f"live writers may survive the import (INF-33). Refusing to continue."
        )
        sys.exit(1)
    baseline = {cid: state for cid, state in baseline_all.items() if cid != db_container_id}
    application_container_ids = [
        cid for cid, state in baseline.items() if state["running"]
    ]
    if application_container_ids:
        log(f"-> Stopping application services for compose project '{project}'...")
    else:
        log(f"-> No running application services to stop for compose project '{project}'.")

    def _current_application_states():
        return {
            cid: state for cid, state in _container_states().items()
            if cid != db_container_id
        }

    def _window_violations(current):
        """Names of application containers that ran, or run, since the baseline."""
        names = {
            state["name"]
            for cid, state in current.items()
            if cid not in baseline
            or state["running"]
            or state["started_at"] != baseline[cid]["started_at"]
        }
        for cid, name in watcher.started():
            if db_container_id and cid.startswith(db_container_id):
                continue
            names.add(name or cid[:12])
        if not names:
            return ""
        return f"application container(s) ran during the import window: {', '.join(sorted(names))}"

    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

    def _raise_on_sigterm(signum, frame):
        raise SystemExit(f"Received signal {signum} while application services were stopped.")

    try:
        signal.signal(signal.SIGTERM, _raise_on_sigterm)
    except ValueError:
        # Not on the main thread; the handler is a best-effort guard.
        previous_sigterm_handler = None

    # What a successful run starts again. Import-only (start_left_stopped) also
    # starts the services an earlier failed import left stopped, which is what lets
    # the next refresh repair the application; a restart policy of "no" marks the
    # one-shot init containers, which are never started this way.
    containers_to_start = list(application_container_ids)
    if start_left_stopped:
        containers_to_start += [
            cid for cid, state in baseline.items()
            if not state["running"] and state["restart_policy"] != "no"
        ]

    body_succeeded = False
    window_open = False
    watcher = ContainerStartWatcher(project)
    try:
        if application_container_ids:
            run_cmd(["docker", "stop", *application_container_ids])
        window_open = True
        yield

        violations = _window_violations(_current_application_states())
        if violations:
            raise RuntimeError(f"IMPORT_WINDOW_VIOLATION: {violations}")
        body_succeeded = True
    finally:
        if not body_succeeded and not restart_on_error:
            log(
                "IMPORT_FAILED_APP_STOPPED: The import failed; the application is "
                "being left stopped and needs the refresh repair."
            )
            try:
                current = _current_application_states()
                # The import's own error stays the primary one; this names the
                # cause when something started the application during the window.
                violations = _window_violations(current) if window_open else ""
                if violations:
                    log(f"IMPORT_WINDOW_VIOLATION: {violations}")
                running_ids = [cid for cid, state in current.items() if state["running"]]
                if running_ids:
                    stopped = run_cmd(["docker", "stop", *running_ids], check=False)
                    if stopped.returncode != 0:
                        log(
                            f"ERROR: IMPORT_FAILED_APP_STILL_RUNNING: could not stop "
                            f"{' '.join(running_ids)} after the failed import; the "
                            f"application may be serving a half-imported database."
                        )
                        if stopped.stderr.strip():
                            log(stopped.stderr.strip())
            except BaseException as stop_error:
                log(
                    f"ERROR: IMPORT_FAILED_APP_STILL_RUNNING: the post-failure check or "
                    f"stop failed: {stop_error}"
                )
        elif containers_to_start:
            log(f"-> Starting application services for compose project '{project}'...")
            try:
                restart = run_cmd(["docker", "start", *containers_to_start], check=False)
                if restart.returncode != 0:
                    log(
                        f"ERROR: Failed to restart application services for compose project "
                        f"'{project}'. THE APPLICATION IS STILL STOPPED and needs manual "
                        f"attention: docker start {' '.join(containers_to_start)}"
                    )
                    if restart.stderr.strip():
                        log(restart.stderr.strip())
            except BaseException as restart_error:
                log(
                    f"ERROR: Failed to restart application services for compose project "
                    f"'{project}'. THE APPLICATION IS STILL STOPPED and needs manual "
                    f"attention: {restart_error}"
                )
        watcher.close()
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


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


def assert_schema_healthy(db_container_id: str, db_user: str, db_name: str):
    """Fail when a restored public base table has no primary key."""
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
    result = run_cmd([
        "docker", "exec", db_container_id,
        "psql", "-U", db_user, "-d", db_name,
        "-v", "ON_ERROR_STOP=1", "-Atc", missing_primary_keys_sql,
    ])
    missing_tables = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if missing_tables:
        log(
            "ERROR: Schema assertion failed: "
            f"{len(missing_tables)} public base table(s) have no primary key: "
            + ", ".join(missing_tables)
        )
        sys.exit(1)

    log("-> Schema assertion passed: every public base table has a primary key.")


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

    try:
        with application_services_stopped(matched_project, db_container_id):
            reset_database(db_container_id, db_user, db_name)
            stream_restore_into_psql(env, snapshot_id, dump_path, db_container_id, db_user, db_name)
    except Exception as e:
        log(f"ERROR: Import failed: {e}")
        sys.exit(1)

    # Migrations require the backend container, so the application is restarted first.
    run_migrations(matched_project)
    assert_schema_healthy(db_container_id, db_user, db_name)

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

    try:
        with application_services_stopped(
            matched_project,
            db_container_id,
            restart_on_error=False,
            start_left_stopped=True,
        ):
            reset_database(db_container_id, db_user, db_name)
            stream_restore_from_local_file(dump_file, db_container_id, db_user, db_name)
    except Exception as e:
        log(f"ERROR: Import failed: {e}")
        sys.exit(1)

    # Migrations require the backend container, so the application is restarted first.
    run_migrations(matched_project)
    assert_schema_healthy(db_container_id, db_user, db_name)

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
