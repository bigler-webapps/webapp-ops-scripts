"""Tests for verify_backup.py's snapshot selection logic (INF-17).

All pure decision logic — no live restic, no B2, per the WO's "narrow tests"
requirement. Run: pytest test_verify_backup.py
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402
import verify_backup as vb  # noqa: E402


def test_snapshot_ordering_across_mixed_offsets_uses_parsed_instant():
    # research-prod's snapshot string sorts above main-prod's even though it is
    # the earlier instant (05:18:29+02:00 == 03:18:29 UTC < 03:19:27 UTC). This
    # is the exact shape of the run-31990601185 defect (INF-17 defect 2).
    snapshots = [
        {"id": "research0002", "time": "2026-08-17T05:18:29.000000000+02:00"},
        {"id": "mainprod0001", "time": "2026-08-17T03:19:27.000000000+00:00"},
    ]
    chosen = vb.choose_snapshot(snapshots)
    assert chosen["id"] == "mainprod0001"


def test_explicit_snapshot_id_wins_over_a_newer_one():
    snapshots = [
        {"id": "own_snapshot_id", "time": "2026-08-17T03:19:27+00:00"},
        {"id": "newer_foreign_id", "time": "2026-08-17T05:18:29+02:00"},
    ]
    chosen = vb.choose_snapshot(snapshots, explicit_id="own_snapshot_id")
    assert chosen["id"] == "own_snapshot_id"


def test_explicit_snapshot_id_supports_short_prefix():
    snapshots = [{"id": "abcdef1234567890", "time": "2026-08-17T03:19:27+00:00"}]
    chosen = vb.choose_snapshot(snapshots, explicit_id="abcdef12")
    assert chosen["id"] == "abcdef1234567890"


def test_explicit_snapshot_id_ambiguous_prefix_raises():
    snapshots = [
        {"id": "abcdef1111111111", "time": "2026-08-17T03:19:27+00:00"},
        {"id": "abcdef2222222222", "time": "2026-08-17T03:19:27+00:00"},
    ]
    with pytest.raises(RuntimeError):
        vb.choose_snapshot(snapshots, explicit_id="abcdef")


def test_explicit_snapshot_id_not_found_raises():
    with pytest.raises(RuntimeError):
        vb.choose_snapshot([{"id": "other", "time": "2026-08-17T03:19:27+00:00"}], explicit_id="missing")


def test_empty_snapshot_list_raises_without_silently_passing():
    with pytest.raises(RuntimeError):
        vb.choose_snapshot([], explicit_id=None)


def test_filename_timestamp_from_non_utc_host_falls_inside_freshness_window():
    # A dump named by the *fixed* backup.py on a +02:00 host must land inside
    # the freshness window computed around its own (UTC) snapshot time.
    local_plus2 = datetime(2026, 8, 17, 5, 18, 37, tzinfo=timezone(timedelta(hours=2)))
    filename = f"main-prod_appdb_{backup.utc_timestamp_str(local_plus2)}.sql.gz"

    ts = vb.parse_timestamp_from_filename(filename)
    assert ts is not None

    snap_time = datetime(2026, 8, 17, 3, 18, 40, tzinfo=timezone.utc)
    min_time = snap_time - timedelta(hours=vb.MAX_SNAPSHOT_WINDOW_HOURS)
    max_time = snap_time + timedelta(minutes=5)
    assert min_time <= ts <= max_time


def test_legacy_colon_timestamp_format_still_parses():
    ts = vb.parse_timestamp_from_filename("app_db_2025-11-20T22:06:07Z.sql.gz")
    assert ts == datetime(2025, 11, 20, 22, 6, 7, tzinfo=timezone.utc)


def test_snapshot_with_only_legacy_formats_selects_both(monkeypatch):
    snapshot_time = datetime(2025, 11, 20, 22, 6, 30, tzinfo=timezone.utc)
    files = [
        "app_db_2025-11-20T220600Z.sql.gz",
        "other_db_2025-11-20T22:06:07Z.sql.gz",
    ]
    verified = []

    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb, "read_run_dumps_file", lambda path: files)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: files)
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 0
    assert verified == files


def test_manifest_named_dump_outside_old_window_is_verified(monkeypatch):
    snapshot_time = datetime(2026, 9, 21, 8, 18, 42, tzinfo=timezone.utc)
    stale_file = "research-prod_hram_2026-09-21T070000Z.sql.gz"
    verified = []

    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb, "read_run_dumps_file", lambda path: [stale_file])
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [stale_file])
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 0
    assert verified == [stale_file]


def test_mixed_snapshot_verifies_only_the_fresh_dump_not_the_stale_one(monkeypatch):
    # R1 (review finding, WM-OPS-7): a same-snapshot mix of one fresh and one stale dump is the
    # case that would catch the explicitly REJECTED fix (widening MAX_SNAPSHOT_WINDOW_HOURS) --
    # the existing single-dump stale/fresh tests each pass a widened window just as easily as the
    # correct per-dump-timestamp fix, since there is no other file in the snapshot to wrongly pull
    # in. The stale file here is 20 minutes old: well outside the real 0.2h (12min) default window,
    # but well INSIDE a window someone widened to "fix" WM-OPS-7 the rejected way.
    snapshot_time = datetime(2026, 9, 21, 8, 18, 42, tzinfo=timezone.utc)
    fresh_file = "research-prod_hram_2026-09-21T081500Z.sql.gz"
    stale_file = "research-prod_hram_2026-09-21T075800Z.sql.gz"
    verified = []

    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb, "read_run_dumps_file", lambda path: [fresh_file])
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [fresh_file, stale_file])
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 0
    assert verified == [fresh_file]


def test_read_snapshot_id_file_missing_returns_none(tmp_path):
    assert vb.read_snapshot_id_file(tmp_path / "does-not-exist") is None


def test_read_snapshot_id_file_reads_and_strips(tmp_path):
    f = tmp_path / "snap_id"
    f.write_text("abc123\n", encoding="utf-8")
    assert vb.read_snapshot_id_file(f) == "abc123"


def test_no_matching_dumps_still_exits_non_zero(monkeypatch):
    # Guard against regression while the selection logic was rewritten: an
    # empty candidate set must never silently pass.
    monkeypatch.setenv("RESTIC_REPO_B2", "s3:example/repo")
    monkeypatch.setenv("RESTIC_PASSWORD", "x")
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "x")
    monkeypatch.setattr(vb, "read_run_dumps_file", lambda path: ["missing.sql.gz"])
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb, "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", "2026-08-17T03:19:27+00:00"),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [])

    assert vb.main() == 1


def test_current_run_verifies_every_manifest_dump_even_outside_old_window(monkeypatch, tmp_path):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    old_dump = "hpc-bridge_db_2026-09-22T100000Z.sql.gz"
    recent_dump = "hram_db_2026-09-22T102500Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{old_dump}\n{recent_dump}\n", encoding="utf-8")
    verified = []

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [old_dump, recent_dump])
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 0
    assert verified == [old_dump, recent_dump]


def test_manifest_dump_thirty_minutes_before_snapshot_is_verified(monkeypatch, tmp_path):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    dump = "hpc-bridge_db_2026-09-22T100000Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{dump}\n", encoding="utf-8")
    verified = []

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [dump])
    monkeypatch.setattr(vb, "verify_gzip_stream_from_restic", lambda env, snapshot_id, path: verified.append(path))

    assert vb.main() == 0
    assert verified == [dump]


def test_manifest_dump_missing_from_snapshot_fails_and_names_it(monkeypatch, tmp_path, capsys):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    present = "hram_db_2026-09-22T102500Z.sql.gz"
    missing = "hpc-bridge_db_2026-09-22T100000Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{present}\n{missing}\n", encoding="utf-8")

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [present])

    assert vb.main() == 1
    assert missing in capsys.readouterr().out


def test_previous_run_dump_in_snapshot_is_excluded(monkeypatch, tmp_path):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    previous = "hram_db_2026-09-21T102500Z.sql.gz"
    current = "hram_db_2026-09-22T102500Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{current}\n", encoding="utf-8")
    verified = []

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [previous, current])
    monkeypatch.setattr(vb, "verify_gzip_stream_from_restic", lambda env, snapshot_id, path: verified.append(path))

    assert vb.main() == 0
    assert verified == [current]


def test_missing_manifest_fails_without_time_window_fallback(monkeypatch, tmp_path, capsys):
    # R1 (review finding, WM-OPS-9): assert the fallback path is not merely UNMENTIONED in the
    # output but genuinely never REACHED -- a regression that logs this exact message and then
    # falls back to verifying anyway would satisfy a string-only assertion but not this spy.
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    dump = "hram_db_2026-09-22T102500Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    verified = []

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [dump])
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 1
    output = capsys.readouterr().out
    assert "Run dump manifest is missing" in output
    assert "fall back" in output
    assert verified == []


def test_unreadable_manifest_fails_closed(monkeypatch, tmp_path, capsys):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    dump = "hram_db_2026-09-22T102500Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{dump}\n", encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == manifest:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [dump])
    verified = []
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: verified.append(path),
    )

    assert vb.main() == 1
    assert "Run dump manifest is unreadable" in capsys.readouterr().out
    assert verified == []


def test_manifest_from_different_run_fails_and_names_old_dump(monkeypatch, tmp_path, capsys):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    old_dump = "hram_db_2026-09-21T102500Z.sql.gz"
    current_dump = "hram_db_2026-09-22T102500Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{old_dump}\n", encoding="utf-8")

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [current_dump])

    assert vb.main() == 1
    assert old_dump in capsys.readouterr().out


def test_manifest_named_corrupt_dump_still_fails_gzip_check(monkeypatch, tmp_path, capsys):
    snapshot_time = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    dump = "hram_db_2026-09-22T102500Z.sql.gz"
    manifest = tmp_path / ".last_run_dumps"
    manifest.write_text(f"{dump}\n", encoding="utf-8")

    monkeypatch.setattr(vb, "LAST_RUN_DUMPS_FILE", manifest, raising=False)
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "password")
    monkeypatch.setattr(vb, "VERIFY_ALL_SQL_GZ", False)
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb,
        "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", snapshot_time.isoformat()),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [dump])
    monkeypatch.setattr(
        vb,
        "verify_gzip_stream_from_restic",
        lambda env, snapshot_id, path: (_ for _ in ()).throw(RuntimeError("gzip integrity check failed")),
    )

    assert vb.main() == 1
    assert "gzip integrity check failed" in capsys.readouterr().out
