import builtins
import runpy
import subprocess
from types import SimpleNamespace

import pytest

import restic_version as rv
import restore
import verify_backup


@pytest.fixture(autouse=True)
def clear_version_cache():
    rv.require_b2_version.cache_clear()
    yield
    rv.require_b2_version.cache_clear()


@pytest.mark.parametrize("version", ["0.16.0", "0.16.4", "0.17.1", "1.0.0"])
def test_supported_versions_are_cached(monkeypatch, version):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=f"restic {version} compiled with go1.20\n")
    monkeypatch.setattr(subprocess, "run", run)
    rv.require_b2_version("restic")
    rv.require_b2_version("restic")
    assert calls == [["restic", "version"]]


@pytest.mark.parametrize("output,code", [("restic 0.15.2", 0), ("restic 0.12.1", 0), ("garbage", 0), ("", 0), ("restic 0.17.1", 1)])
def test_invalid_or_old_versions_refused(monkeypatch, output, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(rv.ResticTooOld, match="restic_version_floor:"):
        rv.require_b2_version("restic")


def test_missing_binary_refused(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(rv.ResticTooOld, match="restic_version_floor:"):
        rv.require_b2_version("missing")


@pytest.mark.parametrize("path", ["verify", "in-place", "dump-only"])
@pytest.mark.parametrize("version", ["0.15.2", "0.16.4"])
def test_repository_paths_enforce_floor_before_first_command(monkeypatch, capsys, path, version):
    calls = []

    class FirstRepositoryCommand(BaseException):
        pass

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout=f"restic {version}")
        raise FirstRepositoryCommand()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(verify_backup, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(verify_backup, "REPO_PWD", "password")
    monkeypatch.setattr(verify_backup.os, "uname", lambda: SimpleNamespace(nodename="test"), raising=False)
    monkeypatch.setattr(verify_backup, "read_snapshot_id_file", lambda path: None)
    monkeypatch.setenv("RESTIC_REPOSITORY", "b2:example:repo")
    monkeypatch.setenv("RESTIC_PASSWORD", "password")
    monkeypatch.setattr(restore, "candidate_projects", lambda *args: ["project"])
    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("container", "project"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda container: ("user", "db"))
    args = SimpleNamespace(target_env="staging", source_env="staging", output_dir="unused", app="app", snapshot="latest")
    invoke = {"verify": verify_backup.main, "in-place": lambda: restore.mode_in_place(args),
              "dump-only": lambda: restore.mode_dump_only(args)}[path]
    binary = verify_backup.RESTIC_BIN if path == "verify" else restore.RESTIC_BIN
    if version == "0.15.2":
        assert invoke() == 1
        assert "restic_version_floor:" in capsys.readouterr().out
        assert calls == [[binary, "version"]]
    else:
        with pytest.raises(FirstRepositoryCommand):
            invoke()
        assert calls[0] == [binary, "version"]
        assert len(calls) == 2
        assert "snapshots" in calls[1]


def test_restore_import_without_floor_module_and_remote_refusal(monkeypatch, capsys):
    real_import = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == "restic_version":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    namespace = runpy.run_path(restore.__file__, run_name="restore_import_test")
    assert callable(namespace["mode_import_only"])
    monkeypatch.setenv("RESTIC_REPOSITORY", "b2:example:repo")
    assert namespace["mode_in_place"](SimpleNamespace()) == 1
    assert "restic_version_floor:" in capsys.readouterr().out


@pytest.mark.parametrize("repository", ["s3:example/repo", "S3:example/repo", "b2:example:repo"])
def test_restore_remote_repositories_are_checked(monkeypatch, repository):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="restic 0.15.2"))
    assert restore.check_remote_version(repository) is False


@pytest.mark.parametrize("repository", ["/srv/restic/repo", "", "rest:http://host/repo", "sftp:host:/repo"])
def test_restore_local_repositories_bypass_the_floor(monkeypatch, repository):
    def fail(*args, **kwargs):
        raise AssertionError("version check must not run for a local repository")
    monkeypatch.setattr(subprocess, "run", fail)
    assert restore.check_remote_version(repository) is True


def test_verify_missing_floor_module_refuses(monkeypatch, capsys):
    real_import = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == "restic_version":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(verify_backup, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(verify_backup, "REPO_PWD", "password")
    assert verify_backup.main() == 1
    assert "restic_version_floor:" in capsys.readouterr().out
