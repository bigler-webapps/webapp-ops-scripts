"""WM-INF-129: database swap with a recoverable copy, idempotent rollback and the capacity decline."""
import re
import subprocess
from pathlib import Path

import pytest

import restore

DB, USER, CONTAINER = "app_db", "app", "db-0001"
PREV = "app_db__prev_0123456789ab"


class FakePostgres:
    """The databases of one Postgres instance as a set, driven only through the psql arguments the code sends."""

    def __init__(self, databases, sizes=None, live_content="old"):
        self.databases = set(databases)
        self.contents = {name: (live_content if name == DB else "data") for name in databases}
        self.sizes = sizes or {}
        self.statements = []

    def run(self, args):
        sql = args[-1]
        self.statements.append(sql)
        if m := re.fullmatch(r"SELECT 1 FROM pg_database WHERE datname='([^']+)';", sql):
            return "1\n" if m.group(1) in self.databases else ""
        if m := re.fullmatch(r"SELECT pg_database_size\('([^']+)'\);", sql):
            return f"{self.sizes.get(m.group(1), 0)}\n"
        if sql.startswith("SELECT pg_terminate_backend"):
            return ""
        if m := re.fullmatch(r'ALTER DATABASE "([^"]+)" RENAME TO "([^"]+)";', sql):
            assert m.group(1) in self.databases and m.group(2) not in self.databases
            self.databases.remove(m.group(1))
            self.databases.add(m.group(2))
            self.contents[m.group(2)] = self.contents.pop(m.group(1))
            return ""
        if m := re.fullmatch(r'CREATE DATABASE "([^"]+)";', sql):
            assert m.group(1) not in self.databases
            self.databases.add(m.group(1))
            self.contents[m.group(1)] = "empty"
            return ""
        if m := re.fullmatch(r'DROP DATABASE IF EXISTS "([^"]+)" WITH \(FORCE\);', sql):
            self.databases.discard(m.group(1))
            self.contents.pop(m.group(1), None)
            return ""
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeChild:
    def __init__(self, output):
        self.output = output
        self.returncode = 0

    def wait(self):
        return 0

    def communicate(self):
        return self.output, ""


class FakeTx:
    def __init__(self, postgres):
        self.postgres = postgres
        self.record = {"cutover": {}}
        self.phases = {}

    def exec_psql(self, container_id, *args, **kwargs):
        assert container_id == CONTAINER
        return FakeChild(self.postgres.run(args))

    def mark_database_started(self):
        self.phases["database"] = "importing"

    def update_phase(self, phase, status, error_code=None):
        self.phases[phase] = status

    def update_cutover(self, **fields):
        self.record["cutover"].update(fields)


def swap(tx, previous=PREV, import_ok=True, monkeypatch=None):
    def importer(dump, container, user, name, tx=None):
        assert name in tx.postgres.databases, "the new database exists before the import"
        if not import_ok:
            raise RuntimeError("partial import")
        tx.postgres.contents[name] = "new"

    monkeypatch.setattr(restore, "stream_restore_from_local_file", importer)
    restore.swap_in_database(tx, Path("dump.gz"), CONTAINER, USER, DB, previous)


def test_the_live_database_is_kept_as_a_copy_while_the_new_one_is_imported(monkeypatch):
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    swap(tx, monkeypatch=monkeypatch)
    assert postgres.databases == {DB, "postgres", PREV}
    assert (postgres.contents[DB], postgres.contents[PREV]) == ("new", "old")
    assert tx.record["cutover"] == {"database_previous": PREV, "database_had_live": True, "database_swap": "imported"}
    assert tx.phases["database"] == "completed"
    assert not any(sql.startswith("DROP") for sql in postgres.statements), "nothing is dropped before the new state is verified"


def test_a_partial_import_is_rolled_back_to_the_previous_database(monkeypatch):
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    with pytest.raises(RuntimeError, match="partial import"):
        swap(tx, import_ok=False, monkeypatch=monkeypatch)
    assert postgres.databases == {"postgres", PREV, DB}, "the half-imported database is still in the way"
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {DB, "postgres"}
    assert postgres.contents[DB] == "old", "the live data itself is back, not an empty database under the old name"
    assert tx.record["cutover"]["database_swap"] == "restored"


def test_the_rollback_is_idempotent(monkeypatch):
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    swap(tx, monkeypatch=monkeypatch)
    restore.rollback_database(tx, CONTAINER, USER, DB)
    snapshot = set(postgres.databases)
    restore.rollback_database(tx, CONTAINER, USER, DB)
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == snapshot == {DB, "postgres"}


@pytest.mark.parametrize("interrupted_after", ["renaming", "renamed", "created"])
def test_an_interruption_at_every_boundary_is_restored_to_the_same_state(interrupted_after):
    """The journal field says how far the swap got; the database set says what really happened. Both orders restore."""
    states = {
        "renaming": ({DB, "postgres"}, False),                       # renaming recorded, nothing renamed yet
        "renamed": ({PREV, "postgres"}, True),           # renamed, new one not created yet
        "created": ({PREV, DB, "postgres"}, True),       # new one created, import not finished
    }
    databases, _ = states[interrupted_after]
    postgres = FakePostgres(databases)
    tx = FakeTx(postgres)
    tx.record["cutover"] = {"database_previous": PREV, "database_had_live": True, "database_swap": interrupted_after}
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {DB, "postgres"}


def test_a_missing_previous_copy_after_the_rename_cannot_be_shown_and_keeps_the_barrier():
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    tx.record["cutover"] = {"database_previous": PREV, "database_had_live": True, "database_swap": "imported"}
    with pytest.raises(RuntimeError, match="cannot be shown"):
        restore.rollback_database(tx, CONTAINER, USER, DB)
    assert DB in postgres.databases, "the new database is not dropped when there is nothing to restore"


def test_without_a_swap_the_rollback_changes_nothing():
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {DB, "postgres"} and postgres.statements == []


def test_an_app_without_a_live_database_is_rolled_back_to_none(monkeypatch):
    postgres = FakePostgres({"postgres"})
    tx = FakeTx(postgres)
    with pytest.raises(RuntimeError):
        swap(tx, import_ok=False, monkeypatch=monkeypatch)
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {"postgres"}


def test_a_leftover_copy_of_the_same_operation_refuses_a_second_swap(monkeypatch):
    postgres = FakePostgres({DB, PREV, "postgres"})
    with pytest.raises(RuntimeError, match="already exists"):
        swap(FakeTx(postgres), monkeypatch=monkeypatch)
    assert postgres.databases == {DB, PREV, "postgres"}


def test_the_previous_copy_is_discarded_after_a_verified_cutover(monkeypatch):
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    swap(tx, monkeypatch=monkeypatch)
    restore.discard_previous_database(tx, CONTAINER, USER, PREV)
    assert postgres.databases == {DB, "postgres"} and tx.record["cutover"]["database_previous_dropped"] is True
    restore.discard_previous_database(tx, CONTAINER, USER, PREV)  # repeatable


def test_database_names_are_validated_before_they_reach_sql():
    postgres = FakePostgres({DB})
    tx = FakeTx(postgres)
    for bad in ('x"; DROP DATABASE y; --', "a'b", "", "../x", "x" * 64):
        with pytest.raises(RuntimeError, match="unsafe database name"):
            restore.database_exists(tx, CONTAINER, USER, bad)
    assert postgres.statements == []


def test_previous_database_name_is_stable_per_operation_and_bounded():
    first = restore.previous_database_name("a" * 63, "12345-1")
    assert first == restore.previous_database_name("a" * 63, "12345-1")
    assert first != restore.previous_database_name("a" * 63, "12345-2")
    assert len(first) <= 63 and re.fullmatch(restore.SAFE_DB_NAME_RE.pattern, first)


@pytest.mark.parametrize("free,live,dump,ok", [
    (10 << 30, 2 << 30, 1 << 30, True),
    (3 << 30, 2 << 30, 1 << 30, True),            # 2 GiB (2 x dump = live) + 1 GiB reserve = 3 GiB: exactly enough
    ((3 << 30) - 1, 2 << 30, 1 << 30, False),
    (5 << 30, 1 << 30, 4 << 30, False),            # the new state may be larger than the live one: 2 x 4 + 1 > 5
])
def test_capacity_is_declined_before_anything_changes(free, live, dump, ok):
    if ok:
        restore.require_capacity(free, live, dump)
    else:
        with pytest.raises(restore.InsufficientCapacity):
            restore.require_capacity(free, live, dump)


def test_the_size_probe_without_a_transaction_is_a_plain_read_only_docker_exec(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "12345\n", "")

    monkeypatch.setattr(restore.subprocess, "run", run)
    assert restore.database_size_bytes(None, CONTAINER, USER, DB) == 12345
    assert seen["command"][:4] == ["docker", "exec", CONTAINER, "psql"]


def test_the_kept_copy_name_must_be_one_the_swap_produces_and_the_record_cannot_name_another_database(monkeypatch):
    postgres = FakePostgres({DB, "other_app_db", "postgres"})
    tx = FakeTx(postgres)
    for bad in ("other_app_db", "app_db__prev_nothex", "other__prev_0123456789ab", "app_db__prev_0123456789abc"):
        tx.record["cutover"] = {"database_previous": bad, "database_had_live": True, "database_swap": "renamed"}
        with pytest.raises(RuntimeError, match="unexpected previous database name|unsafe database name"):
            restore.rollback_database(tx, CONTAINER, USER, DB)
        with pytest.raises(RuntimeError):
            restore.discard_previous_database(tx, CONTAINER, USER, bad, DB)
    assert postgres.databases == {DB, "other_app_db", "postgres"}, "no foreign database was renamed or dropped"
    assert not any(sql.startswith(("ALTER", "DROP")) for sql in postgres.statements)
    with pytest.raises(RuntimeError):
        swap(FakeTx(postgres), previous="other_app_db", monkeypatch=monkeypatch)


def test_the_rollback_restores_the_old_data_after_a_completed_import(monkeypatch):
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    swap(tx, monkeypatch=monkeypatch)
    assert postgres.contents[DB] == "new" and postgres.contents[PREV] == "old"
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.contents[DB] == "old" and PREV not in postgres.databases


def test_a_rollback_interrupted_after_the_rename_back_is_recognised_as_done(monkeypatch):
    """The rename back completed, the journal says `restoring`: no previous copy is left but the verified database is live."""
    postgres = FakePostgres({DB, "postgres"})
    tx = FakeTx(postgres)
    tx.record["cutover"] = {"database_previous": PREV, "database_had_live": True, "database_swap": "restoring"}
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {DB, "postgres"} and tx.record["cutover"]["database_swap"] == "restored"
    # without a live database either, the verified state really is gone: the barrier stays
    empty = FakePostgres({"postgres"})
    tx = FakeTx(empty)
    tx.record["cutover"] = {"database_previous": PREV, "database_had_live": True, "database_swap": "restoring"}
    with pytest.raises(RuntimeError, match="cannot be shown"):
        restore.rollback_database(tx, CONTAINER, USER, DB)


def test_without_a_live_database_a_create_the_journal_lagged_behind_is_removed_again():
    postgres = FakePostgres({DB, "postgres"}, live_content="created-after-the-last-journal-write")
    tx = FakeTx(postgres)
    tx.record["cutover"] = {"database_previous": PREV, "database_had_live": False, "database_swap": "renamed"}
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {"postgres"}, "the pre-refresh state (no database) is restored"


def test_a_rename_that_completed_while_the_journal_still_said_renaming_is_rolled_back(monkeypatch):
    """The window the journal cannot cover: the swap recorded `renaming`, the rename happened, nothing else was recorded."""
    postgres = FakePostgres({PREV, "postgres"})
    postgres.contents[PREV] = "old"
    tx = FakeTx(postgres)
    tx.record["cutover"] = {"database_previous": PREV, "database_had_live": True, "database_swap": "renaming"}
    restore.rollback_database(tx, CONTAINER, USER, DB)
    assert postgres.databases == {DB, "postgres"} and postgres.contents[DB] == "old"
