"""Focused restore-integrity regression tests for CI-10.

Run: pytest test_restore.py
"""

import gzip
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import restore  # noqa: E402


def completed_process(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


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


def test_import_only_propagates_schema_assertion_failure(tmp_path, monkeypatch):
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"truncated-dump-placeholder")

    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "app_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda container_id: ("app-user", "app-db"))
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
