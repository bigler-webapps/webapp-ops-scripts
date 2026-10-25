"""Focused restore-integrity regression tests for CI-10.

Run: pytest test_restore.py
"""

import gzip
import json
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import restore  # noqa: E402

REAL_WATCHER = restore.ContainerStartWatcher  # the autouse fixture swaps the module's name for a fake


def completed_process(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def fake_application_service_ownership(events=None):
    @contextmanager
    def ownership(project, db_container_id, restart_on_error=True, start_left_stopped=False):
        if events is not None:
            events.append(("stop", project, db_container_id))
        try:
            yield
        finally:
            if events is not None:
                events.append(("start", project, db_container_id))

    return ownership


class FakeStartWatcher:
    """Stands in for ContainerStartWatcher: no real `docker events` process in a unit test."""

    seen = []
    instances = []

    def __init__(self, project):
        self.project = project
        self.closed = False
        FakeStartWatcher.instances.append(self)

    def started(self):
        return list(FakeStartWatcher.seen)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_real_docker_events(monkeypatch):
    FakeStartWatcher.seen = []
    FakeStartWatcher.instances = []
    monkeypatch.setattr(restore, "ContainerStartWatcher", FakeStartWatcher)


def docker_container(container_id, name, running, started_at, restart_policy="unless-stopped"):
    # Real Docker: `ps -q` prints the short id, `inspect` the full 64 character one.
    return {
        "Id": container_id + "0" * 40,
        "Name": f"/{name}",
        "State": {"Running": running, "StartedAt": started_at},
        "HostConfig": {"RestartPolicy": {"Name": restart_policy}},
    }


def project_docker_run_cmd(calls, containers, stop_rc=0):
    def fake_run_cmd(cmd, env=None, check=True):
        calls.append(cmd)
        if cmd[:2] == ["docker", "events"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=events_rc, stdout="\n".join(events or []), stderr=""
            )
        if cmd[:2] == ["docker", "stop"] and stop_rc:
            return subprocess.CompletedProcess(args=cmd, returncode=stop_rc, stdout="", stderr="denied")
        if cmd[:4] == ["docker", "ps", "-a", "-q"]:
            return completed_process("\n".join(containers) + ("\n" if containers else ""))
        if cmd[:2] == ["docker", "inspect"]:
            return completed_process(json.dumps([containers[cid] for cid in cmd[2:]]))
        if cmd[:2] == ["docker", "stop"]:
            for cid in cmd[2:]:
                containers[cid]["State"]["Running"] = False
        if cmd[:2] == ["docker", "start"]:
            for cid in cmd[2:]:
                containers[cid]["State"]["Running"] = True
                containers[cid]["State"]["StartedAt"] = "2026-10-25T12:00:00Z"
        return completed_process()
    return fake_run_cmd


def test_assert_schema_healthy_passes_when_every_table_has_a_primary_key(monkeypatch):
    calls = []

    def fake_run_cmd(cmd, env=None, check=True):
        calls.append(cmd)
        return completed_process()

    monkeypatch.setattr(restore, "run_cmd", fake_run_cmd)

    restore.assert_schema_healthy("db-container", "app-user", "app-db")

    assert calls[0][:8] == [
        "docker", "exec", "db-container", "psql", "-U", "app-user", "-d", "app-db",
    ]
    assert "PRIMARY KEY" in calls[0][-1]


def test_assert_schema_healthy_exits_non_zero_and_names_tables_missing_primary_keys(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        restore,
        "run_cmd",
        lambda cmd, env=None, check=True: completed_process(
            "public.status_slurmqueuestate\npublic.status_workerstate\n"
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.assert_schema_healthy("db-container", "app-user", "app-db")

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "2 public base table(s) have no primary key" in output
    assert "public.status_slurmqueuestate" in output
    assert "public.status_workerstate" in output


def test_complete_import_runs_schema_assertion_before_reporting_success(tmp_path, monkeypatch):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"complete-dump-placeholder")
    events = []

    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "app_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
    helper_flags = {}
    fake_ownership = fake_application_service_ownership()

    def recording_ownership(project, db_container_id, **flags):
        helper_flags.update(flags)
        return fake_ownership(project, db_container_id, **flags)

    monkeypatch.setattr(restore, "application_services_stopped", recording_ownership)
    monkeypatch.setattr(restore, "reset_database", lambda *args: events.append("reset"))
    monkeypatch.setattr(
        restore, "stream_restore_from_local_file", lambda *args: events.append("import")
    )
    monkeypatch.setattr(restore, "run_migrations", lambda project: events.append("migrate"))
    monkeypatch.setattr(restore, "assert_schema_healthy", lambda *args: events.append("assert"))

    result = restore.mode_import_only(
        SimpleNamespace(
            app="example",
            target_env="staging",
            dump_file=str(dump_file),
        )
    )

    assert result == 0
    assert events == ["reset", "import", "migrate", "assert"]
    assert helper_flags == {"restart_on_error": False, "start_left_stopped": True}


def test_import_only_propagates_schema_assertion_failure(tmp_path, monkeypatch):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"truncated-dump-placeholder")

    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "app_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
    monkeypatch.setattr(
        restore, "application_services_stopped", fake_application_service_ownership()
    )
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)
    monkeypatch.setattr(restore, "stream_restore_from_local_file", lambda *args: None)
    monkeypatch.setattr(restore, "run_migrations", lambda project: None)
    monkeypatch.setattr(
        restore,
        "run_cmd",
        lambda cmd, env=None, check=True: completed_process(
            "public.status_slurmqueuestate\n"
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(
                app="example",
                target_env="staging",
                dump_file=str(dump_file),
            )
        )

    assert exc_info.value.code == 1


def test_inf_33_import_only_stops_before_reset_and_restarts_before_migrations(
    tmp_path, monkeypatch
):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"complete-dump-placeholder")
    events = []

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
    monkeypatch.setattr(
        restore,
        "application_services_stopped",
        fake_application_service_ownership(events),
    )
    monkeypatch.setattr(restore, "reset_database", lambda *args: events.append("reset"))
    monkeypatch.setattr(
        restore, "stream_restore_from_local_file", lambda *args: events.append("import")
    )
    monkeypatch.setattr(restore, "run_migrations", lambda project: events.append("migrate"))
    monkeypatch.setattr(restore, "assert_schema_healthy", lambda *args: events.append("assert"))

    result = restore.mode_import_only(
        SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file))
    )

    assert result == 0
    assert events == [
        ("stop", "resolved_staging", "db-id"),
        "reset",
        "import",
        ("start", "resolved_staging", "db-id"),
        "migrate",
        "assert",
    ]


def test_inf_33_in_place_stops_before_reset_and_restarts_before_migrations(monkeypatch):
    events = []

    monkeypatch.setenv("RESTIC_REPOSITORY", "test-repository")
    monkeypatch.setenv("RESTIC_PASSWORD", "test-password")
    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
    monkeypatch.setattr(
        restore,
        "resolve_snapshot",
        lambda env, snapshot: ("snapshot-id", "2026-08-18T12:00:00Z"),
    )
    monkeypatch.setattr(restore, "list_sql_gz", lambda env, snapshot_id: ["dump.sql.gz"])
    monkeypatch.setattr(
        restore,
        "choose_dump_for_db",
        lambda all_files, db_name, snap_time: "dump.sql.gz",
    )
    monkeypatch.setattr(
        restore,
        "application_services_stopped",
        fake_application_service_ownership(events),
    )
    monkeypatch.setattr(restore, "reset_database", lambda *args: events.append("reset"))
    monkeypatch.setattr(restore, "stream_restore_into_psql", lambda *args: events.append("import"))
    monkeypatch.setattr(restore, "run_migrations", lambda project: events.append("migrate"))
    monkeypatch.setattr(restore, "assert_schema_healthy", lambda *args: events.append("assert"))

    result = restore.mode_in_place(
        SimpleNamespace(app="example", target_env="staging", snapshot="latest")
    )

    assert result == 0
    assert events == [
        ("stop", "resolved_staging", "db-id"),
        "reset",
        "import",
        ("start", "resolved_staging", "db-id"),
        "migrate",
        "assert",
    ]


def test_inf_33_stop_excludes_database_and_targets_only_resolved_project(monkeypatch):
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
        "beat-id": docker_container("beat-id", "resolved_staging-celery-beat-1", True, "beat-start"),
        "stopped-id": docker_container("stopped-id", "resolved_staging-old-1", False, "old-start"),
    }
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with restore.application_services_stopped("resolved_staging", "db-id"):
        calls.append(["restore-body"])

    assert calls[0] == ["docker", "ps", "-a", "-q", "-f", "label=com.docker.compose.project=resolved_staging"]
    assert calls[1] == ["docker", "inspect", "db-id", "backend-id", "beat-id", "stopped-id"]
    assert ["docker", "stop", "backend-id", "beat-id"] in calls
    assert calls.index(["docker", "start", "backend-id", "beat-id"]) > calls.index(["restore-body"])


def test_inf_33_failed_import_only_leaves_services_stopped(tmp_path, monkeypatch, capsys):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"broken-dump-placeholder")
    calls = []

    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)
    monkeypatch.setattr(
        restore,
        "stream_restore_from_local_file",
        lambda *args: (_ for _ in ()).throw(RuntimeError("broken import")),
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file))
        )

    assert exc_info.value.code == 1
    assert ["docker", "stop", "backend-id"] in calls
    assert ["docker", "start", "backend-id"] not in calls
    assert containers["backend-id"]["State"]["Running"] is False
    assert "IMPORT_FAILED_APP_STOPPED" in capsys.readouterr().out


def test_inf_33_interrupted_import_only_leaves_services_stopped(tmp_path, monkeypatch):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"interrupted-dump-placeholder")
    calls = []

    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)
    monkeypatch.setattr(
        restore,
        "stream_restore_from_local_file",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file))
        )

    assert ["docker", "start", "backend-id"] not in calls
    assert containers["backend-id"]["State"]["Running"] is False


def test_inf_94_import_only_detects_running_container_at_window_end(tmp_path, monkeypatch, capsys):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"placeholder")
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }
    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)

    def import_and_start(*args):
        containers["backend-id"]["State"]["Running"] = True
        containers["backend-id"]["State"]["StartedAt"] = "2026-10-25T09:07:32Z"

    monkeypatch.setattr(restore, "stream_restore_from_local_file", import_and_start)
    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file)))

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "IMPORT_WINDOW_VIOLATION" in output
    assert "resolved_staging-backend-1" in output
    assert "IMPORT_FAILED_APP_STOPPED" in output
    assert ["docker", "start", "backend-id"] not in calls
    assert containers["backend-id"]["State"]["Running"] is False


def test_inf_94_in_place_detects_running_container_at_window_end(monkeypatch, capsys):
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }
    monkeypatch.setenv("RESTIC_REPOSITORY", "test-repository")
    monkeypatch.setenv("RESTIC_PASSWORD", "test-password")
    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "resolve_snapshot", lambda *args: ("snapshot-id", "2026-10-25T08:00:00Z"))
    monkeypatch.setattr(restore, "list_sql_gz", lambda *args: ["dump.sql.gz"])
    monkeypatch.setattr(restore, "choose_dump_for_db", lambda *args: "dump.sql.gz")
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)

    def import_and_start(*args):
        containers["backend-id"]["State"]["Running"] = True
        containers["backend-id"]["State"]["StartedAt"] = "2026-10-25T09:07:32Z"

    monkeypatch.setattr(restore, "stream_restore_into_psql", import_and_start)
    with pytest.raises(SystemExit) as exc_info:
        restore.mode_in_place(SimpleNamespace(app="example", target_env="staging", snapshot="latest"))

    assert exc_info.value.code == 1
    assert "IMPORT_WINDOW_VIOLATION" in capsys.readouterr().out


def test_inf_94_detects_container_started_and_exited_during_window(monkeypatch):
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with pytest.raises(RuntimeError, match="IMPORT_WINDOW_VIOLATION.*resolved_staging-backend-1"):
        with restore.application_services_stopped("resolved_staging", "db-id"):
            containers["backend-id"]["State"]["StartedAt"] = "2026-10-25T09:07:32Z"
            containers["backend-id"]["State"]["Running"] = False


def test_inf_94_checks_window_when_no_application_was_running(monkeypatch):
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", False, "old-start"),
    }
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with pytest.raises(RuntimeError, match="IMPORT_WINDOW_VIOLATION.*resolved_staging-backend-1"):
        with restore.application_services_stopped("resolved_staging", "db-id"):
            containers["backend-id"]["State"]["Running"] = True


def test_truncated_valid_dump_imports_but_post_restore_assertion_rejects_it(
    tmp_path, monkeypatch
):
    """Reproduce the pre-fix false success, then prove the new assertion closes it."""
    full_sql = (
        b"CREATE TABLE public.status_slurmqueuestate (id bigint NOT NULL);\n"
        b"COPY public.status_slurmqueuestate (id) FROM stdin;\n"
        b"1\n"
        b"\\.\n\n"
        b"ALTER TABLE ONLY public.status_slurmqueuestate\n"
        b"    ADD CONSTRAINT status_slurmqueuestate_pkey PRIMARY KEY (id);\n"
    )
    constraint_start = full_sql.index(b"ALTER TABLE ONLY")
    truncated_sql = full_sql[:constraint_start]
    dump_file = tmp_path / "truncated-at-statement-boundary.sql.gz"
    with gzip.open(dump_file, "wb") as compressed:
        compressed.write(truncated_sql)

    imported_sql = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "exec", "-i"] and "psql" in cmd:
            imported_sql.append(kwargs["stdin"].read())
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(restore.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(restore, "find_compose_container", lambda project, service: "backend-id")

    # This is the measured pre-fix behaviour: valid gzip and valid partial SQL
    # both finish successfully, and migrations also report success.
    restore.stream_restore_from_local_file(
        dump_file, "db-container", "app-user", "app-db"
    )
    restore.run_migrations("app_staging")
    assert imported_sql == [truncated_sql]
    assert b"PRIMARY KEY" not in imported_sql[0]

    # The independent post-restore check turns that same restore path red.
    monkeypatch.setattr(
        restore,
        "run_cmd",
        lambda cmd, env=None, check=True: completed_process(
            "public.status_slurmqueuestate\n"
        ),
    )
    with pytest.raises(SystemExit) as exc_info:
        restore.assert_schema_healthy("db-container", "app-user", "app-db")

    assert exc_info.value.code == 1


def test_inf_94_failed_import_does_not_attempt_restart_or_hide_original_error(
    tmp_path, monkeypatch, capsys
):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"broken-dump-placeholder")

    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)
    monkeypatch.setattr(
        restore,
        "stream_restore_from_local_file",
        lambda *args: (_ for _ in ()).throw(RuntimeError("broken import")),
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file))
        )

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "broken import" in out, "the original failure must still be reported"
    assert "IMPORT_FAILED_APP_STOPPED" in out
    assert "refresh repair" in out
    assert ["docker", "start", "backend-id"] not in calls
    assert containers["backend-id"]["State"]["Running"] is False


def test_inf_94_failed_import_names_a_container_started_during_the_window(
    tmp_path, monkeypatch, capsys
):
    """The 2026-10-25 case: something started the stack mid-import and the import
    then failed on its own. The import error stays primary, the cause is named,
    and the foreign start is stopped again."""
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"broken-dump-placeholder")
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }

    def import_with_foreign_start(*args):
        containers["backend-id"]["State"]["Running"] = True
        containers["backend-id"]["State"]["StartedAt"] = "2026-10-25T09:07:35Z"
        raise RuntimeError("broken import")

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))
    monkeypatch.setattr(restore, "reset_database", lambda *args: None)
    monkeypatch.setattr(restore, "stream_restore_from_local_file", import_with_foreign_start)

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file))
        )

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "broken import" in out
    assert "IMPORT_WINDOW_VIOLATION" in out and "resolved_staging-backend-1" in out
    assert containers["backend-id"]["State"]["Running"] is False
    assert ["docker", "start", "backend-id"] not in calls


def _two_containers():
    return {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }


def test_inf_94_container_created_and_running_at_window_end_is_a_violation(monkeypatch):
    calls = []
    containers = _two_containers()
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with pytest.raises(RuntimeError, match="IMPORT_WINDOW_VIOLATION.*resolved_staging-celery-1"):
        with restore.application_services_stopped("resolved_staging", "db-id"):
            containers["celery-id"] = docker_container(
                "celery-id", "resolved_staging-celery-1", True, "2026-10-25T09:07:35Z"
            )


def test_inf_94_container_started_and_removed_again_is_found_by_the_event_watcher(monkeypatch):
    calls = []
    containers = _two_containers()
    FakeStartWatcher.seen = [
        ("db-id0000000000000000", "resolved_staging-db-1"),
        ("run00000000000000000000", "resolved_staging-backend-run-1"),
    ]
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with pytest.raises(RuntimeError) as exc_info:
        with restore.application_services_stopped("resolved_staging", "db-id"):
            pass

    assert "IMPORT_WINDOW_VIOLATION" in str(exc_info.value)
    assert "resolved_staging-backend-run-1" in str(exc_info.value)
    assert "resolved_staging-db-1" not in str(exc_info.value), "the database's own events are not application starts"
    assert FakeStartWatcher.instances[0].project == "resolved_staging"
    assert FakeStartWatcher.instances[0].closed, "the listener is always stopped again"


def test_inf_94_event_watcher_streams_the_project_events_and_reads_them_back(monkeypatch):
    started = {}

    class FakePopen:
        returncode = None

        def __init__(self, cmd, stdout=None, stderr=None, text=None):
            started["cmd"] = cmd
            self.stdout = stdout
            stdout.write("abc123 resolved_staging-backend-run-1\n")
            stdout.flush()
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15

    monkeypatch.setattr(restore.subprocess, "Popen", FakePopen)
    watcher = REAL_WATCHER("resolved_staging")
    try:
        assert watcher.started() == [("abc123", "resolved_staging-backend-run-1")]
    finally:
        watcher.close()

    cmd = started["cmd"]
    assert cmd[:2] == ["docker", "events"]
    assert "label=com.docker.compose.project=resolved_staging" in cmd
    assert "event=create" in cmd and "event=start" in cmd
    assert "{{.Actor.ID}} {{.Actor.Attributes.name}}" in cmd


def test_inf_94_event_watcher_that_cannot_run_is_reported_and_not_fatal(monkeypatch, capsys):
    def no_docker(*args, **kwargs):
        raise OSError("docker not found")

    monkeypatch.setattr(restore.subprocess, "Popen", no_docker)
    watcher = REAL_WATCHER("resolved_staging")
    try:
        assert watcher.started() == []
    finally:
        watcher.close()

    assert "checking container state only" in capsys.readouterr().out


def test_inf_94_event_watcher_that_dies_early_is_reported_once(monkeypatch, capsys):
    class DeadPopen:
        returncode = 1

        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return 1

    monkeypatch.setattr(restore.subprocess, "Popen", DeadPopen)
    watcher = REAL_WATCHER("resolved_staging")
    try:
        assert watcher.started() == [] and watcher.started() == []
    finally:
        watcher.close()

    assert capsys.readouterr().out.count("exited early") == 1


def test_inf_94_database_container_is_matched_against_real_docker_id_lengths(monkeypatch):
    """`ps -q` gives the short id, `inspect` the full one: the database must still be found
    and still never be stopped."""
    calls = []
    containers = _two_containers()
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with restore.application_services_stopped("resolved_staging", "db-id"):
        pass

    assert ["docker", "stop", "backend-id"] in calls
    assert not any(cmd[:2] == ["docker", "stop"] and "db-id" in cmd for cmd in calls)


def test_inf_94_successful_import_only_starts_services_a_failed_import_left_stopped(monkeypatch):
    calls = []
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", False, "be-start"),
        "init-id": docker_container(
            "init-id", "resolved_staging-init-app-volumes-1", False, "init-start", restart_policy="no"
        ),
    }
    monkeypatch.setattr(restore, "run_cmd", project_docker_run_cmd(calls, containers))

    with restore.application_services_stopped(
        "resolved_staging", "db-id", restart_on_error=False, start_left_stopped=True
    ):
        pass

    assert ["docker", "start", "backend-id"] in calls
    assert not any(cmd[:2] == ["docker", "start"] and "init-id" in cmd for cmd in calls), (
        "one-shot init containers are not started"
    )


def test_inf_94_failed_import_reports_when_the_post_failure_stop_does_not_work(monkeypatch, capsys):
    calls = []
    containers = _two_containers()
    monkeypatch.setattr(
        restore, "run_cmd", project_docker_run_cmd(calls, containers, stop_rc=0)
    )
    real = restore.run_cmd

    def stop_fails_after_the_window(cmd, env=None, check=True):
        if cmd[:2] == ["docker", "stop"] and containers["backend-id"]["State"]["StartedAt"] == "foreign":
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="denied")
        return real(cmd, env=env, check=check)

    monkeypatch.setattr(restore, "run_cmd", stop_fails_after_the_window)

    with pytest.raises(RuntimeError, match="broken import"):
        with restore.application_services_stopped("resolved_staging", "db-id", restart_on_error=False):
            containers["backend-id"]["State"]["Running"] = True
            containers["backend-id"]["State"]["StartedAt"] = "foreign"
            raise RuntimeError("broken import")

    out = capsys.readouterr().out
    assert "IMPORT_FAILED_APP_STILL_RUNNING" in out
    assert "denied" in out


def test_inf_33_refuses_when_label_lookup_cannot_see_the_resolved_db_container(
    tmp_path, monkeypatch, capsys
):
    """INF-33 R3: find_compose_container falls back to a name match, so a
    label-only lookup returning nothing may mean 'blind', not 'empty'. Proceeding
    would silently reopen the race. It must refuse instead."""
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"placeholder")
    imported = []

    def fake_run_cmd(cmd, env=None, check=True):
        if cmd[:3] == ["docker", "ps", "-q"]:
            return completed_process("")
        return completed_process()

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: ("db-id", "resolved_staging")
    )
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(restore, "reset_database", lambda *a: imported.append("reset"))
    monkeypatch.setattr(
        restore, "stream_restore_from_local_file", lambda *a: imported.append("import")
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="staging", dump_file=str(dump_file))
        )

    assert exc_info.value.code == 1
    assert imported == [], "nothing may be reset or imported when the stop is unreliable"
    assert "Refusing to continue" in capsys.readouterr().out


def test_inf_33_sigterm_handler_is_installed_inside_the_window_and_restored_after():
    """INF-33 R1: SIGTERM's default disposition terminates without unwinding, so
    `finally` would never run. Asserts the handler is installed for the duration
    and raises; a real signal is deliberately not delivered (not portable)."""
    calls = []
    previous = signal.getsignal(signal.SIGTERM)
    containers = {
        "db-id": docker_container("db-id", "resolved_staging-db-1", True, "db-start"),
        "backend-id": docker_container("backend-id", "resolved_staging-backend-1", True, "be-start"),
    }
    original_run_cmd = restore.run_cmd
    restore.run_cmd = project_docker_run_cmd(calls, containers)
    try:
        with restore.application_services_stopped("resolved_staging", "db-id"):
            handler = signal.getsignal(signal.SIGTERM)
            assert handler is not previous
            with pytest.raises(SystemExit):
                handler(signal.SIGTERM, None)
    finally:
        restore.run_cmd = original_run_cmd

    assert signal.getsignal(signal.SIGTERM) is previous
    assert ["docker", "start", "backend-id"] in calls


def test_inf_33_s1_import_only_rejects_an_unknown_target_env(tmp_path, monkeypatch, capsys):
    """INF-33 S1: the compose project is f"{app}_{env}" and the separator is a
    plain underscore, so free-text env makes the boundary ambiguous --
    `--app foo --target-env bar_staging` names the same project as
    `--app foo_bar --target-env staging`. Constraining env removes the second
    spelling. It does NOT stop a caller naming another app directly; that needs
    an app allowlist this script cannot source."""
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"placeholder")
    touched = []

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: touched.append(projects) or ("", "")
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="bar_staging", dump_file=str(dump_file))
        )

    assert exc_info.value.code == 1
    assert touched == [], "no container lookup may happen for an unknown env"
    assert "--target-env must be one of" in capsys.readouterr().out


def test_inf_33_s1_known_envs_are_still_accepted(tmp_path, monkeypatch):
    """The guard must not break the real callers: every ENV_ALIASES key passes."""
    for env_name in sorted(restore.ENV_ALIASES):
        restore.require_known_env(env_name, "--target-env")


def test_inf_33_s1_dump_only_rejects_an_unknown_source_env(tmp_path, monkeypatch, capsys):
    """INF-33 R1 (delta review): the mode_dump_only call site had no coverage at
    all -- no test invoked that mode. A wrong attribute, a wrong flag string, or
    the guard drifting below the output_dir side effects would have gone
    unnoticed. This pins the call site, not just the helper."""
    monkeypatch.setenv("RESTIC_REPOSITORY", "b2:example")
    monkeypatch.setenv("RESTIC_PASSWORD", "irrelevant")
    touched = []
    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: touched.append(projects) or ("", "")
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_dump_only(
            SimpleNamespace(
                app="example",
                source_env="bar_production",
                output_dir=str(tmp_path / "out"),
                snapshot="latest",
            )
        )

    assert exc_info.value.code == 1
    assert touched == [], "no container lookup may happen for an unknown env"
    assert not (tmp_path / "out").exists(), "no output directory may be created either"
    assert "--source-env must be one of" in capsys.readouterr().out


def test_inf_33_s3_env_alias_values_are_all_keys_too():
    """INF-33 S3 (delta review): require_known_env tests membership against the
    KEYS of ENV_ALIASES. That is deliberately the stricter side -- widening it to
    the values would let an underscore-bearing alias back in and reopen the
    collision. It is correct only while every alias value is also a key, and
    nothing else enforces that. Adding an alias value without its own key would
    silently start rejecting a legitimate environment; this names the reason."""
    for key, aliases in restore.ENV_ALIASES.items():
        for alias in aliases:
            assert alias in restore.ENV_ALIASES, (
                f"'{alias}' is an alias of '{key}' but not a key of ENV_ALIASES, "
                f"so require_known_env would reject it"
            )
