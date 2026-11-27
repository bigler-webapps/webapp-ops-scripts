import builtins
import json
import os
import subprocess
import types

import pytest
import backup
import staging_set_snapshot as staging
import volume_selection as volumes


def install_commands(monkeypatch, *, answers=None, size=123, failure=False):
    calls = []
    answers = answers or {"infrastructure": ["build-2", "build-1"], "runs": ["run-1"]}
    container = {"Id": "backend-id", "Config": {"Labels": {
        "com.docker.compose.project": "hram_production", "com.docker.compose.service": "backend"}},
        "Mounts": [{"Type": "volume", "Name": staging.VOLUME, "Destination": staging.DESTINATION,
                    "Source": "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data"}]}

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "ps" in cmd:
            out = "backend-id"
        elif "inspect" in cmd:
            out = json.dumps([container])
        elif "staging_artifact_selection" in cmd:
            if failure:
                raise subprocess.TimeoutExpired(cmd, 120)
            out = json.dumps(answers[cmd[-1]])
        elif "du" in cmd:
            out = f"{size}\ttotal\n"
        else:
            out = ""
        return types.SimpleNamespace(stdout=out, returncode=0)

    monkeypatch.setattr(staging.subprocess, "run", fake_run)
    return calls


def test_mapping_and_bounded_retention(monkeypatch):
    calls = install_commands(monkeypatch)
    monkeypatch.setattr(staging, "safe_paths", lambda *_: True)
    written = []

    class PathFile:
        name = "/tmp/staging-paths"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, data):
            written.append(data)

        def flush(self):
            pass

    monkeypatch.setattr(staging.tempfile, "NamedTemporaryFile", lambda **kwargs: PathFile())
    selected = staging.selection(["docker"])
    assert selected == ("backend-id", "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data",
                        ["infrastructure/build-1", "infrastructure/build-2", "runs/run-1",
                         "jobs", "scenarios", "data"])
    assert staging.backup(["docker"], "restic", "repo", "research-prod") == "ok bytes=123"
    assert written == [
        "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data/infrastructure/build-1\n"
        "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data/infrastructure/build-2\n"
        "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data/runs/run-1\n"
        "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data/jobs\n"
        "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data/scenarios\n"
        "/var/lib/docker/volumes/hram_production_pipeline_artifacts_volume/_data/data\n"
    ]
    restic = [(cmd, kw) for cmd, kw in calls if cmd[0] == "restic"]
    assert [cmd for cmd, _ in restic] == [
        ["restic", "backup", "--host", "research-prod", "--tag", "hram-staging-set",
         "--files-from-raw", "/tmp/staging-paths"],
        ["restic", "forget", "--prune", "--host", "research-prod", "--tag", "hram-staging-set",
         "--group-by", "host,tag", "--keep-last", "3"],
    ]
    assert all(kw["env"]["RESTIC_REPOSITORY"] == "repo" for _, kw in restic)
    assert [kw["timeout"] for _, kw in restic] == [3600, 1800]
    du = next(cmd for cmd, _ in calls if "du" in cmd)
    assert not any("pipeline_cache" in p or "calibration_outputs" in p for p in du)
    assert all(kw.get("timeout") == staging.TIMEOUT_SECONDS for cmd, kw in calls if cmd[0] == "docker")


def test_skip_codes_do_not_run_restic(monkeypatch):
    monkeypatch.setattr(staging, "safe_paths", lambda *_: True)  # path safety has its own tests
    for options, expected in [({"failure": True}, staging.SELECTOR_FAILURE),
                              ({"answers": {"infrastructure": [], "runs": ["x"]}}, staging.EMPTY_SELECTION),
                              ({"size": staging.MAX_BYTES + 1}, staging.SIZE_EXCEEDED),
                              ({"answers": {"infrastructure": ["../escape"], "runs": ["x"]}}, staging.SELECTOR_FAILURE)]:
        calls = install_commands(monkeypatch, **options)
        assert staging.backup(["docker"], "restic", "repo", "research-prod") == expected
        assert not any(cmd[0] == "restic" for cmd, _ in calls)


def test_dr_selection_and_gate_unchanged():
    cfg = volumes.load_config()
    names = ["hram_production_excel_volume", "hram_production_assistant_cli_sessions",
             "hram_production_pipeline_artifacts_volume", "hram_production_calibration_outputs_volume"]
    assert volumes.select_volumes(names, cfg) == sorted(names[:2])
    assert cfg["max_total_bytes_per_host"] == 17_000_000_000
    assert "*pipeline_artifacts*" in cfg["exclude"] and "*calibration_outputs*" in cfg["exclude"]


def test_dr_retention_is_tagged(monkeypatch):
    calls = []
    monkeypatch.setattr(backup, "RESTIC_PASSWORD", "pw")
    monkeypatch.setattr(backup, "restic_repo_ready", lambda *args: None)
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename="research-prod"), raising=False)
    monkeypatch.setattr(backup, "run_cmd", lambda cmd, **kw: (calls.append(cmd) or True, "snapshot abcdef12 saved", ""))
    monkeypatch.setattr(backup, "require_b2_version", lambda *_: None)
    assert backup.run_restic("repo", "B2")[0]
    forget = next(c for c in calls if "forget" in c)
    assert forget[forget.index("--tag") + 1] == "daily"


@pytest.mark.parametrize("code", [staging.SELECTOR_FAILURE, staging.EMPTY_SELECTION, staging.SIZE_EXCEEDED])
def test_skip_is_in_verdict_after_both_regular_backups(monkeypatch, capsys, code):
    calls = []
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename="research-prod"), raising=False)
    monkeypatch.setattr(backup, "RESTIC_REPO_LOCAL", "local")
    monkeypatch.setattr(backup, "RESTIC_REPO_B2", "b2")
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: [])
    monkeypatch.setattr(backup, "record_b2_snapshot", lambda *_: None)
    monkeypatch.setattr(backup, "record_run_dumps", lambda *_: None)
    monkeypatch.setattr(backup, "run_restic", lambda url, name, **kw: (calls.append(name) or True, "snapshot"))
    monkeypatch.setattr(staging, "backup", lambda *args: calls.append("staging") or code)
    backup.main()
    out = capsys.readouterr().out
    assert calls == ["Local", "B2", "staging"]
    assert f"STAGING_SET={code}" in out and "Backup Success" in out


@pytest.mark.parametrize("step,timeout", [("backup", False), ("forget", False),
                                           ("backup", True), ("forget", True)])
def test_restic_failure_and_timeout(monkeypatch, tmp_path, step, timeout):
    calls = install_commands(monkeypatch)
    monkeypatch.setattr(staging, "safe_paths", lambda *_: True)
    fake_run = staging.subprocess.run
    named_tempfile = staging.tempfile.NamedTemporaryFile
    monkeypatch.setattr(staging.tempfile, "NamedTemporaryFile",
                        lambda **kwargs: named_tempfile(dir=tmp_path, **kwargs))

    def failing_run(cmd, **kwargs):
        if cmd[0] == "restic" and cmd[1] == step:
            calls.append((cmd, kwargs))
            if timeout:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return types.SimpleNamespace(returncode=1)
        return fake_run(cmd, **kwargs)

    monkeypatch.setattr(staging.subprocess, "run", failing_run)
    assert staging.backup(["docker"], "restic", "repo", "research-prod") == "snapshot_failure"
    assert [cmd[1] for cmd, _ in calls if cmd[0] == "restic"] == (
        ["backup"] if step == "backup" else ["backup", "forget"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("host,repository", [("other-host", "b2"), ("research-prod", None)])
def test_staging_step_skipped_without_host_and_repository(monkeypatch, capsys, host, repository):
    calls = []
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename=host), raising=False)
    monkeypatch.setattr(backup, "RESTIC_REPO_LOCAL", "local")
    monkeypatch.setattr(backup, "RESTIC_REPO_B2", repository)
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: [])
    monkeypatch.setattr(backup, "record_b2_snapshot", lambda *_: None)
    monkeypatch.setattr(backup, "record_run_dumps", lambda *_: None)
    monkeypatch.setattr(backup, "run_restic", lambda url, name, **kw: (calls.append(name) or True, "snapshot"))
    monkeypatch.setattr(staging, "backup", lambda *args: calls.append("staging"))
    backup.main()
    assert calls == ["Local", "B2"]
    assert "STAGING_SET=" not in capsys.readouterr().out


@pytest.mark.parametrize("case", ["infrastructure", "entry", "outside", "missing", "clean"])
def test_host_path_guard(monkeypatch, tmp_path, capsys, case):
    root = tmp_path / "volume" / "_data"
    for path in ("infrastructure/build", "runs/run", "jobs", "scenarios", "data"):
        (root / path).mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if case != "clean":
        target = {"infrastructure": root / "infrastructure", "entry": root / "runs" / "run",
                  "outside": root / "jobs", "missing": root / "data"}[case]
        if case == "infrastructure":
            (target / "build").rmdir()
        target.rmdir()
        if case != "missing":
            destination = outside if case == "outside" else root / "scenarios"
            try:
                os.symlink(destination, target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"symlink creation unavailable: {exc}")
    calls = install_commands(monkeypatch)
    monkeypatch.setattr(staging, "selection", lambda _: ("backend-id", str(root),
                        ["infrastructure/build", "runs/run", "jobs", "scenarios", "data"]))
    result = staging.backup(["docker"], "restic", "repo", "research-prod")
    assert result == ("ok bytes=123" if case == "clean" else staging.UNSAFE_PATH)
    assert bool([cmd for cmd, _ in calls if cmd[0] == "restic"]) == (case == "clean")
    assert bool([cmd for cmd, _ in calls if "du" in cmd]) == (case == "clean")
    assert str(root) not in capsys.readouterr().out


@pytest.mark.parametrize("case", ["two", "dbrecv", "pg", "volume", "destination", "source"])
def test_selector_rejections_report_failure(monkeypatch, case):
    container = {"Id": "backend", "Config": {"Labels": {
        "com.docker.compose.project": "hram_production", "com.docker.compose.service": "backend"}},
        "Mounts": [{"Type": "volume", "Name": staging.VOLUME, "Destination": staging.DESTINATION,
                    "Source": "/var/lib/docker/volumes/volume/_data"}]}
    labels = container["Config"]["Labels"]
    mount = container["Mounts"][0]
    if case == "dbrecv":
        labels["dbrecv.enable"] = "true"
    elif case == "pg":
        labels["backup.pg.user"] = "user"
    elif case == "volume":
        mount["Name"] = "wrong"
    elif case == "destination":
        mount["Destination"] = "/wrong"
    elif case == "source":
        mount["Source"] = "/outside/_data"
    containers = [container, container] if case == "two" else [container]
    calls = []
    def command(args):
        calls.append(args)
        return "backend" if "ps" in args else json.dumps(containers)
    monkeypatch.setattr(staging, "command", command)
    with pytest.raises(ValueError):
        staging.selection(["docker"])
    assert staging.backup(["docker"], "restic", "repo", "research-prod") == staging.SELECTOR_FAILURE
    assert all("du" not in call for call in calls)


@pytest.mark.parametrize("import_failure", [True, False])
def test_staging_snapshot_failure_preserves_success(monkeypatch, capsys, import_failure):
    calls = []
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename="research-prod"), raising=False)
    monkeypatch.setattr(backup, "RESTIC_REPO_LOCAL", "local")
    monkeypatch.setattr(backup, "RESTIC_REPO_B2", "b2")
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: [])
    monkeypatch.setattr(backup, "record_b2_snapshot", lambda *_: None)
    monkeypatch.setattr(backup, "record_run_dumps", lambda *_: None)
    monkeypatch.setattr(backup, "run_restic", lambda url, name, **kw: (calls.append(name) or True, "snapshot"))
    monkeypatch.setattr(staging, "backup", lambda *args: calls.append("staging") or staging.SNAPSHOT_FAILURE)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if import_failure and name == "staging_set_snapshot":
            raise ImportError("unavailable")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    backup.main()
    assert calls == (["Local", "B2"] if import_failure else ["Local", "B2", "staging"])
    output = capsys.readouterr().out
    assert "STAGING_SET=snapshot_failure" in output
    assert "Backup Success" in output
