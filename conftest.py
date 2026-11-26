"""Keep unrelated repository tests independent of the installed restic version."""

import pytest


@pytest.fixture(autouse=True)
def supported_restic_for_repository_tests(request, monkeypatch):
    if request.node.path.name in {"test_restore.py", "test_verify_backup.py", "test_volume_backup.py"}:
        import backup
        import restic_version

        monkeypatch.setattr(backup, "require_b2_version", lambda binary: None)
        monkeypatch.setattr(restic_version, "require_b2_version", lambda binary: None)
