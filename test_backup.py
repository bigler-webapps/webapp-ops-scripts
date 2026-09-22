"""Tests for backup.py's UTC timestamp generation (INF-17).

Run: pytest test_backup.py
"""

import sys
import types
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402
import verify_backup as vb  # noqa: E402


def _perform_mock_dumps(monkeypatch, tmp_path, targets, run_start, completion_times):
    clock = {"now": run_start}
    completions = iter(completion_times)
    gzip_checks = []

    monkeypatch.setattr(backup, "DUMP_DIR", tmp_path)
    monkeypatch.setattr(backup, "discover_docker_targets", lambda: targets)
    monkeypatch.setattr(backup.os, "chmod", lambda path, mode: None)
    monkeypatch.setattr(
        backup,
        "utc_timestamp_str",
        lambda: clock["now"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
    )

    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"database dump")

        def wait(self):
            clock["now"] = next(completions)
            return 0

    monkeypatch.setattr(backup.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    def fake_run_cmd(cmd, env=None, cwd=None, show_on_success=False):
        gzip_checks.append(cmd)
        return True, "", ""

    monkeypatch.setattr(backup, "run_cmd", fake_run_cmd)
    dump_names = backup.perform_db_dumps()
    return sorted(tmp_path.glob("*.sql.gz")), gzip_checks, dump_names


def _run_verification(monkeypatch, files, snapshot_time):
    verified = []
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb, "read_run_dumps_file", lambda path: [path.name for path in files])
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [str(path) for path in files])
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 0
    return verified


def test_utc_timestamp_str_converts_non_utc_local_time():
    # Wall-clock 05:18:37 on a host whose local zone is +02:00 is 03:18:37 UTC.
    # Against the pre-fix code (naive `datetime.now()`, no conversion) this
    # would have produced "051837" instead of "031837".
    local_plus2 = datetime(2026, 8, 17, 5, 18, 37, tzinfo=timezone(timedelta(hours=2)))
    assert backup.utc_timestamp_str(local_plus2) == "2026-08-17T031837Z"


def test_utc_timestamp_str_defaults_to_real_utc_now():
    result = backup.utc_timestamp_str()
    parsed = datetime.strptime(result, "%Y-%m-%dT%H%M%SZ")
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now_utc_naive - parsed).total_seconds()) < 5


def test_dump_completed_after_long_run_is_fresh_and_selected(monkeypatch, tmp_path):
    run_start = datetime(2026, 9, 21, 8, 6, tzinfo=timezone.utc)
    completion = run_start + timedelta(minutes=12, seconds=30)
    snapshot_time = completion + timedelta(seconds=38)
    targets = [{"id": "research1", "name": "research-prod", "user": "postgres", "db": "hram"}]

    files, gzip_checks, dump_names = _perform_mock_dumps(monkeypatch, tmp_path, targets, run_start, [completion])

    assert len(files) == 1
    assert dump_names == [files[0].name]
    assert vb.parse_timestamp_from_filename(str(files[0])) == completion
    assert gzip_checks == [["gzip", "-t", str(tmp_path / "research-prod_hram_research1.sql.gz.tmp")]]
    assert _run_verification(monkeypatch, files, snapshot_time) == [str(files[0])]


def test_dumps_from_one_run_keep_their_individual_completion_times(monkeypatch, tmp_path):
    run_start = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    completions = [run_start + timedelta(minutes=10), run_start + timedelta(minutes=18)]
    snapshot_time = completions[-1] + timedelta(seconds=20)
    targets = [
        {"id": "target1", "name": "first", "user": "postgres", "db": "app"},
        {"id": "target2", "name": "second", "user": "postgres", "db": "app"},
    ]

    files, _, dump_names = _perform_mock_dumps(monkeypatch, tmp_path, targets, run_start, completions)

    assert [vb.parse_timestamp_from_filename(str(path)) for path in files] == completions
    assert dump_names == [path.name for path in files]
    assert _run_verification(monkeypatch, files, snapshot_time) == [str(path) for path in files]


def test_backup_filename_round_trips_through_verify_parser(monkeypatch, tmp_path):
    completion = datetime(2026, 9, 21, 8, 18, 30, tzinfo=timezone.utc)
    targets = [{"id": "target1", "name": "main-prod", "user": "postgres", "db": "appdb"}]

    files, _, _ = _perform_mock_dumps(monkeypatch, tmp_path, targets, completion, [completion])

    assert len(files) == 1
    assert vb.parse_timestamp_from_filename(str(files[0])) == completion


# --- B2 snapshot marker survives a retention failure (INF-24) -----------------

def test_run_restic_preserves_snapshot_id_when_retention_fails(monkeypatch):
    # Reproduces the observed collision verbatim: backup succeeds, forget
    # --prune then hits another target's live lock on the shared B2 repo.
    # run_restic must still report the snapshot id it already created --
    # losing it here is what let the marker go stale on a retention failure.
    calls = []

    def fake_run_cmd(cmd, env=None, cwd=None, show_on_success=False):
        calls.append(cmd)
        if "backup" in cmd:
            return True, "snapshot abc12345 saved", ""
        if "forget" in cmd:
            return False, "", (
                "unable to create lock in backend: repository is already "
                "locked by PID 699822 on research-prod by deploy"
            )
        return True, "", ""  # snapshots (repo-ready check)

    monkeypatch.setattr(backup, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(backup, "RESTIC_PASSWORD", "x")
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)

    ok, snap_id = backup.run_restic("s3:example/repo", "B2")

    assert ok is False
    assert snap_id == "abc12345"


def test_record_b2_snapshot_writes_marker_and_prints_greppable_line(tmp_path, monkeypatch, capsys):
    marker = tmp_path / ".last_b2_snapshot_id"
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", marker)

    backup.record_b2_snapshot("abc12345")

    assert marker.read_text(encoding="utf-8") == "abc12345\n"
    assert "B2_SNAPSHOT_CREATED=abc12345" in capsys.readouterr().out


def test_record_b2_snapshot_prints_marker_even_if_the_file_write_fails(tmp_path, monkeypatch, capsys):
    # A workflow-level consumer (INF-24) reads the stdout marker, not the
    # file -- the file write is a best-effort convenience for verify_backup.py
    # and must not be a single point of failure for the workflow-level signal.
    unwritable_dir = tmp_path / "no-such-dir"  # parent doesn't exist -> OSError
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", unwritable_dir / "marker")

    backup.record_b2_snapshot("abc12345")

    assert "B2_SNAPSHOT_CREATED=abc12345" in capsys.readouterr().out


def test_record_b2_snapshot_no_marker_line_when_no_snapshot_was_created(capsys):
    backup.record_b2_snapshot(None)

    out = capsys.readouterr().out
    assert "B2_SNAPSHOT_CREATED=" not in out
    assert "WARN: no B2 snapshot id captured" in out


def test_record_run_dumps_writes_one_filename_per_line(tmp_path, monkeypatch):
    marker = tmp_path / ".last_run_dumps"
    monkeypatch.setattr(backup, "LAST_RUN_DUMPS_FILE", marker)

    backup.record_run_dumps(["first.sql.gz", "second.sql.gz"])

    assert marker.read_text(encoding="utf-8") == "first.sql.gz\nsecond.sql.gz\n"


def test_main_records_marker_before_the_retention_failure_exit(monkeypatch, tmp_path):
    # End-to-end ordering guard: main() must call record_b2_snapshot() before
    # the ok_local/ok_b2 fail() gate. Against the pre-INF-24 ordering, fail()'s
    # sys.exit(1) would abort main() before this ever ran -- the marker would
    # never update on a retention-only failure. This test simulates that exact
    # shape end to end (no live restic/B2/docker).
    marker = tmp_path / ".last_b2_snapshot_id"
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", marker)
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    dump_marker = tmp_path / ".last_run_dumps"
    monkeypatch.setattr(backup, "LAST_RUN_DUMPS_FILE", dump_marker)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: ["run.sql.gz"])

    def fake_run_restic(repo_url, repo_name, *, unlock_stale=False):
        if repo_name == "Local":
            return True, "local0001"
        return False, "b2snap001"  # B2 backup succeeded, retention failed

    monkeypatch.setattr(backup, "run_restic", fake_run_restic)

    with pytest.raises(SystemExit):
        backup.main()

    assert marker.read_text(encoding="utf-8") == "b2snap001\n"
    assert dump_marker.read_text(encoding="utf-8") == "run.sql.gz\n"


def test_dump_manifest_is_not_updated_when_b2_snapshot_creation_fails_entirely(monkeypatch, tmp_path):
    # R1 (review finding, WM-OPS-9): the dump manifest must go stale IN LOCKSTEP with the
    # snapshot-id marker, never ahead of it -- otherwise a B2 failure could leave
    # LAST_B2_SNAPSHOT_ID_FILE pointing at an OLDER snapshot while LAST_RUN_DUMPS_FILE already
    # names THIS run's (different) dumps, a desync verify_backup.py cannot detect on its own.
    snap_marker = tmp_path / ".last_b2_snapshot_id"
    snap_marker.write_text("previous_snapshot_id\n", encoding="utf-8")
    dump_marker = tmp_path / ".last_run_dumps"
    dump_marker.write_text("previous_run.sql.gz\n", encoding="utf-8")
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", snap_marker)
    monkeypatch.setattr(backup, "LAST_RUN_DUMPS_FILE", dump_marker)
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: ["this_run.sql.gz"])

    def fake_run_restic(repo_url, repo_name, *, unlock_stale=False):
        if repo_name == "Local":
            return True, "local0001"
        return False, None  # B2 backup failed outright -- no snapshot was created at all

    monkeypatch.setattr(backup, "run_restic", fake_run_restic)

    with pytest.raises(SystemExit):
        backup.main()

    # Both markers stay at their PREVIOUS values -- neither updates without the other.
    assert snap_marker.read_text(encoding="utf-8") == "previous_snapshot_id\n"
    assert dump_marker.read_text(encoding="utf-8") == "previous_run.sql.gz\n"
