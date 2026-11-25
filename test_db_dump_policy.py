"""Tests for fail-closed per-target database dump exclusions."""

import io
import json
from pathlib import Path

import pytest

import backup


def test_dump_argv_excludes_only_declared_target(monkeypatch, tmp_path, capsys):
    targets = [
        {"id": "one", "name": "hram_production-db-1", "user": "u", "db": "hram_db"},
        {"id": "two", "name": "another", "user": "u", "db": "hram_db"},
        {"id": "three", "name": "hram_production-db-1", "user": "u", "db": "other"},
    ]
    commands = []
    monkeypatch.setattr(backup, "DUMP_DIR", tmp_path)
    monkeypatch.setattr(backup, "discover_docker_targets", lambda: targets)
    monkeypatch.setattr(backup.os, "chmod", lambda *args: None)
    monkeypatch.setattr(backup, "run_cmd", lambda *args, **kwargs: (True, "", ""))

    class Process:
        def __init__(self, cmd, **kwargs):
            commands.append(cmd)
            self.stdout = io.BytesIO(b"SQL")

        def wait(self):
            return 0

    monkeypatch.setattr(backup.subprocess, "Popen", Process)
    backup.perform_db_dumps()
    assert [arg for arg in commands[0] if arg.startswith("--exclude-table-data=")] == [
        "--exclude-table-data=public.results_teachingreferralflow",
        "--exclude-table-data=public.results_teachingtreatmentoutcome",
    ]
    assert all(not any(arg.startswith("--exclude-table-data=") for arg in cmd) for cmd in commands[1:])
    output = capsys.readouterr().out
    assert "public.results_teachingreferralflow, public.results_teachingtreatmentoutcome" in output
    assert output.count("Dump exclusions:") == 3


def test_dump_argv_uses_supplied_policy_for_each_target(monkeypatch, tmp_path):
    policy = {
        "exclude_table_data": {
            "custom-db": {
                "alpha": ["reports.events", "archive.entries"],
                "beta": ["public.sessions"],
            },
            "other-db": {"alpha": ["analytics.metrics"]},
        }
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    targets = [
        {"id": "one", "name": "custom-db", "user": "u1", "db": "alpha"},
        {"id": "two", "name": "custom-db", "user": "u2", "db": "beta"},
        {"id": "three", "name": "other-db", "user": "u3", "db": "alpha"},
        {"id": "four", "name": "unlisted-db", "user": "u4", "db": "alpha"},
    ]
    commands = []
    monkeypatch.setattr(backup, "DB_DUMP_POLICY_FILE", path)
    monkeypatch.setattr(backup, "DUMP_DIR", tmp_path)
    monkeypatch.setattr(backup, "discover_docker_targets", lambda: targets)
    monkeypatch.setattr(backup.os, "chmod", lambda *args: None)
    monkeypatch.setattr(backup, "run_cmd", lambda *args, **kwargs: (True, "", ""))

    class Process:
        def __init__(self, cmd, **kwargs):
            commands.append(cmd)
            self.stdout = io.BytesIO(b"SQL")

        def wait(self):
            return 0

    monkeypatch.setattr(backup.subprocess, "Popen", Process)
    backup.perform_db_dumps()
    assert commands == [
        backup.DOCKER_CMD + ["exec", "-i", "one", "pg_dump", "-U", "u1",
                             "--exclude-table-data=reports.events",
                             "--exclude-table-data=archive.entries", "alpha"],
        backup.DOCKER_CMD + ["exec", "-i", "two", "pg_dump", "-U", "u2",
                             "--exclude-table-data=public.sessions", "beta"],
        backup.DOCKER_CMD + ["exec", "-i", "three", "pg_dump", "-U", "u3",
                             "--exclude-table-data=analytics.metrics", "alpha"],
        backup.DOCKER_CMD + ["exec", "-i", "four", "pg_dump", "-U", "u4", "alpha"],
    ]


@pytest.mark.parametrize("policy", [
    [], {}, {"exclude_table_data": [], "extra": 1},
    {"exclude_table_data": {"container": []}},
    {"exclude_table_data": {"container": {"db": "public.table"}}},
    {"exclude_table_data": {"container": {"db": ["--exclude-table-data=public.table"]}}},
    {"exclude_table_data": {"container": {"db": ["public.*"]}}},
    {"exclude_table_data": {"container": {"db": [42]}}},
])
def test_invalid_policy_stops_before_dump(monkeypatch, tmp_path, capsys, policy):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(backup, "DB_DUMP_POLICY_FILE", path)
    monkeypatch.setattr(backup, "discover_docker_targets", lambda: pytest.fail("started dump"))
    with pytest.raises(SystemExit):
        backup.perform_db_dumps()
    assert "Cannot load database dump policy" in capsys.readouterr().out


@pytest.mark.parametrize("contents", ["{", '{"exclude_table_data": {}, "exclude_table_data": {}}'])
def test_malformed_or_duplicate_policy_fails(monkeypatch, tmp_path, contents):
    path = tmp_path / "policy.json"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(backup, "DB_DUMP_POLICY_FILE", path)
    with pytest.raises(SystemExit):
        backup.perform_db_dumps()


def test_missing_policy_stops_before_dump(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(backup, "DB_DUMP_POLICY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(backup, "discover_docker_targets", lambda: pytest.fail("started dump"))
    with pytest.raises(SystemExit):
        backup.perform_db_dumps()
    assert "Cannot load database dump policy" in capsys.readouterr().out
