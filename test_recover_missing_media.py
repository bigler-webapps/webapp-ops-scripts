import os
import io
import json
import sys
import tempfile
import contextlib
import shlex
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import recover_missing_media as recovery

# The sandbox's default Windows temp location is outside writable roots.
tempfile.tempdir = str(Path(__file__).resolve().parent)


def execute_remote_helper(code, argv, stdin, *, docker_mount=None):
    class CapturedOutput(io.StringIO):
        def __init__(self):
            super().__init__()
            self.buffer = io.BytesIO()

    output = CapturedOutput()
    if isinstance(stdin, bytes):
        input_stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
    else:
        input_stream = io.BytesIO(stdin)
    with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stdin", input_stream), contextlib.redirect_stdout(output):
        try:
            if docker_mount is None:
                exec(compile(code, "<embedded-helper>", "exec"), {})
            else:
                docker_result = SimpleNamespace(stdout=json.dumps([{"Mountpoint": str(docker_mount)}]))
                with mock.patch.object(recovery.subprocess, "run", return_value=docker_result):
                    exec(compile(code, "<embedded-helper>", "exec"), {})
        except Exception as exc:
            if type(exc).__name__ == "RecoveryError":
                raise recovery.RecoveryError(str(exc)) from exc
            raise
    return output.buffer.getvalue() if output.buffer.tell() else output.getvalue()


class PathValidationTests(unittest.TestCase):
    def test_rejects_traversal_absolute_backslash_and_secret_paths(self):
        for value in ("../x", "private/../../x", "/etc/passwd", "C:/media/x", "private\\x", ".env", "private/.env", "secret/key", "secrets/token"):
            with self.subTest(value=value), self.assertRaises(recovery.RecoveryError):
                recovery.validate_relative_path(value)

    def test_rejects_source_and_target_parent_or_final_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            (outside / "file.jpg").write_bytes(b"media")
            media = root / "media"
            media.mkdir()
            (media / "private").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(recovery.RecoveryError):
                recovery.checked_path(media, "private/file.jpg")
            (media / "private").unlink()
            (media / "private").mkdir()
            (media / "private" / "link.jpg").symlink_to(outside / "file.jpg")
            with self.assertRaises(recovery.RecoveryError):
                recovery.checked_path(media, "private/link.jpg")


class SelectionAndPreflightTests(unittest.TestCase):
    def test_fresh_references_are_deduplicated_and_only_missing_are_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            (root / "private" / "present.jpg").write_bytes(b"already here")
            missing, skipped = recovery.select_missing(
                ["private/present.jpg", "private/missing.jpg", "private/missing.jpg", ""], root
            )
            self.assertEqual(missing, ["private/missing.jpg"])
            self.assertEqual(skipped, 1)

    def test_source_preflight_fails_for_missing_source_before_any_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(recovery.RecoveryError):
                recovery.source_preflight(tmp, ["private/not-there.jpg"])
            with mock.patch.object(recovery, "query_target", return_value={
                "media_root": "/media", "references": 1, "missing": ["private/missing.jpg"]
            }), mock.patch.object(recovery, "preflight_source", side_effect=recovery.RecoveryError("missing")), mock.patch.object(recovery, "publish_target") as publish:
                self.assertEqual(recovery.main([]), 1)
                publish.assert_not_called()


class AtomicPublishTests(unittest.TestCase):
    def test_exact_copy_hash_and_resume_after_interrupted_write(self):
        data = b"verified media contents"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("os.fsync", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    recovery.publish_verified(root, "private/a.jpg", data, len(data), recovery.sha256_bytes(data))
            self.assertFalse((root / "private" / "a.jpg").exists())
            self.assertEqual(list(root.rglob("*.part")), [])
            self.assertEqual(recovery.publish_verified(root, "private/a.jpg", data, len(data), recovery.sha256_bytes(data)), "copied")
            self.assertEqual((root / "private" / "a.jpg").read_bytes(), data)

    def test_checksum_or_size_mismatch_never_publishes_final_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(recovery.RecoveryError):
                recovery.publish_verified(root, "private/a.jpg", b"changed", 7, recovery.sha256_bytes(b"expected"))
            self.assertFalse((root / "private" / "a.jpg").exists())

    def test_existing_and_racing_files_are_never_overwritten(self):
        data = b"new"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "private" / "existing.jpg"
            existing.parent.mkdir()
            existing.write_bytes(b"old")
            self.assertEqual(recovery.publish_verified(root, "private/existing.jpg", data, len(data), recovery.sha256_bytes(data)), "skipped")
            self.assertEqual(existing.read_bytes(), b"old")

            racing = root / "private" / "racing.jpg"
            def create_racer_and_lose(source, destination, **kwargs):
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=kwargs["dst_dir_fd"])
                os.write(fd, b"racer")
                os.close(fd)
                raise FileExistsError()

            with mock.patch("os.link", side_effect=create_racer_and_lose):
                self.assertEqual(recovery.publish_verified(root, "private/racing.jpg", data, len(data), recovery.sha256_bytes(data)), "skipped")
            self.assertEqual(racing.read_bytes(), b"racer")
            self.assertEqual(list(root.rglob(".recover-media-*.part")), [])

    def test_embedded_source_helpers_preflight_and_stream_real_local_file(self):
        data = b"source media bytes"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            (root / "private" / "a.jpg").write_bytes(data)
            paths = ["private/a.jpg"]
            output = execute_remote_helper(
                recovery.SOURCE_PREFLIGHT_CODE,
                ["helper", recovery.SOURCE_VOLUME],
                json.dumps(paths).encode("utf-8"),
                docker_mount=root,
            )
            manifest = json.loads(output)
            self.assertEqual(manifest, [{"path": paths[0], "size": len(data), "sha256": recovery.sha256_bytes(data)}])
            streamed = execute_remote_helper(
                recovery.SOURCE_READ_CODE,
                ["helper", recovery.SOURCE_VOLUME, paths[0]],
                b"",
                docker_mount=root,
            )
            self.assertEqual(streamed, data)

    def test_embedded_destination_helper_verifies_publishes_and_skips_existing(self):
        data = b"destination media bytes"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = {"media_root": str(root), "path": "private/a.jpg", "size": len(data), "sha256": recovery.sha256_bytes(data)}
            encoded = recovery.base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")
            output = execute_remote_helper(
                recovery.TARGET_PUBLISH_CODE, ["helper", encoded], data
            )
            self.assertEqual(output.strip(), "copied")
            final = root / "private" / "a.jpg"
            self.assertEqual(final.read_bytes(), data)
            bad_meta = {"media_root": str(root), "path": "private/b.jpg", "size": 9, "sha256": "0" * 64}
            wrong = recovery.base64.b64encode(json.dumps(bad_meta).encode("utf-8")).decode("ascii")
            with self.assertRaises(recovery.RecoveryError):
                execute_remote_helper(recovery.TARGET_PUBLISH_CODE, ["helper", wrong], b"new bytes")
            self.assertEqual(final.read_bytes(), data)
            self.assertFalse((root / "private" / "b.jpg").exists())
            self.assertEqual(list(root.rglob("*.part")), [])


class CommandConstructionTests(unittest.TestCase):
    def test_ssh_command_disables_terminal_for_both_fixed_aliases(self):
        for alias in (recovery.SOURCE_ALIAS, recovery.TARGET_ALIAS):
            with self.subTest(alias=alias):
                command = recovery.ssh_command(alias, "printf remote-output")
                self.assertEqual(command[:2], ["ssh", "-T"])
                self.assertEqual(command[-2:], [alias, "printf remote-output"])
                self.assertIn("BatchMode=yes", command)
                self.assertIn("ConnectTimeout=10", command)
                self.assertIn("RemoteCommand=none", command)

    def test_hostile_path_is_data_and_never_in_remote_command(self):
        hostile = "private/x'; touch /tmp/pwned; ' .jpg"
        cmd = recovery.target_publish_command(
            {"path": hostile, "size": 1, "sha256": "0" * 64}, "/app/backend/media"
        )
        self.assertNotIn(hostile, " ".join(cmd))
        self.assertIn("docker exec -i", cmd[-1])
        encoded = shlex.split(cmd[-1])[-1]
        metadata = json.loads(recovery.base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(metadata, {"media_root": "/app/backend/media", "path": hostile, "size": 1, "sha256": "0" * 64})


class RecoveryFlowTests(unittest.TestCase):
    def test_binary_transfer_stays_exact_through_terminal_sensitive_transport(self):
        payload = b"lf\ncrlf\r\nnull\x00high\xff\xc3\xa9\n"
        item = {
            "path": "private/binary.dat",
            "size": len(payload),
            "sha256": recovery.sha256_bytes(payload),
        }
        inventory = {
            "media_root": "/media",
            "references": 1,
            "missing": [item["path"]],
        }
        transferred = {"source_stdout": None, "destination_stdin": None}

        def terminal_sensitive_transport(command, **kwargs):
            # Model a PTY's text-mode line-ending conversion. The selected
            # behavior comes from the actual SSH arguments under test.
            pty_requested = "-T" not in command
            if command[-2] == recovery.SOURCE_ALIAS:
                received = payload.replace(b"\n", b"\r\n") if pty_requested else payload
                transferred["source_stdout"] = received
                return SimpleNamespace(returncode=0, stdout=received, stderr=b"")
            self.assertEqual(command[-2], recovery.TARGET_ALIAS)
            received = kwargs["input"]
            transferred["destination_stdin"] = (
                received.replace(b"\n", b"\r\n") if pty_requested else received
            )
            return SimpleNamespace(returncode=0, stdout=b"copied\n", stderr=b"")

        with mock.patch.object(recovery, "query_target", return_value=inventory), \
                mock.patch.object(recovery, "preflight_source", return_value=[item]), \
                mock.patch.object(recovery.subprocess, "run", side_effect=terminal_sensitive_transport), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recovery.main(["--apply"]), 0)

        self.assertEqual(transferred["source_stdout"], payload)
        self.assertEqual(transferred["destination_stdin"], payload)
        self.assertEqual(recovery.sha256_bytes(transferred["source_stdout"]), item["sha256"])
        self.assertEqual(recovery.sha256_bytes(transferred["destination_stdin"]), item["sha256"])

        # A changed source is rejected before the destination subprocess runs.
        changed = dict(item, sha256=recovery.sha256_bytes(b"different"))
        with mock.patch.object(recovery, "read_source", return_value=payload), \
                mock.patch.object(recovery.subprocess, "run") as run:
            with self.assertRaisesRegex(recovery.RecoveryError, "source changed after preflight"):
                recovery.publish_target(changed, "/media")
            run.assert_not_called()

    def test_successful_apply_uses_real_helpers_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source", root / "target"
            source.mkdir()
            target.mkdir()
            (target / "private").mkdir()
            existing = target / "private/present.jpg"
            existing.write_bytes(b"preserve newer media")
            contents = {"private/a.jpg": b"first image", "public/measurement_types/b.png": b"second image"}
            for name, data in contents.items():
                file = source / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
            references = ["private/present.jpg", *contents]

            def inventory():
                missing, _ = recovery.select_missing(references, target)
                return {"media_root": str(target), "references": len(references), "missing": missing}

            calls = []
            def transport(command, **kwargs):
                calls.append(command)
                args = shlex.split(command[-1])
                if command[-2] == recovery.SOURCE_ALIAS:
                    output = execute_remote_helper(args[4], ["helper", *args[5:]], kwargs.get("input", b""), docker_mount=source)
                else:
                    self.assertEqual(command[-2], recovery.TARGET_ALIAS)
                    output = execute_remote_helper(args[-2], ["helper", args[-1]], kwargs["input"])
                return SimpleNamespace(returncode=0, stdout=output if isinstance(output, bytes) else output.encode("utf-8"), stderr=b"")

            with mock.patch.object(recovery, "query_target", side_effect=inventory), mock.patch.object(recovery.subprocess, "run", side_effect=transport), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(recovery.main(["--apply"]), 0)
                self.assertIn("Copied: 2; skipped: 0; failures: 0", output.getvalue())
                call_count = len(calls)
                self.assertEqual(recovery.main(["--apply"]), 0)
                self.assertEqual(len(calls), call_count)
            self.assertEqual(len(calls), 5)
            self.assertEqual(existing.read_bytes(), b"preserve newer media")
            for name, data in contents.items():
                self.assertEqual((target / name).read_bytes(), data)
                self.assertEqual(recovery.sha256_bytes((target / name).read_bytes()), recovery.sha256_bytes(data))
            self.assertEqual(list(target.rglob("*.part")), [])

    def test_invalid_target_inventory_is_sanitized_before_any_source_or_write(self):
        inventories = [None, {}, {"media_root": "/media", "references": 1, "missing": None}, {"media_root": "/media", "references": 0, "missing": ["private/a.jpg"]}]
        for value in inventories:
            with self.subTest(value=value), mock.patch.object(recovery, "query_target", return_value=value), mock.patch.object(recovery, "preflight_source") as source, mock.patch.object(recovery, "publish_target") as publish, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(recovery.main(["--apply"]), 1)
                source.assert_not_called()
                publish.assert_not_called()
                self.assertIn("ERROR:", error.getvalue())
                self.assertNotIn("Traceback", error.getvalue())

    def test_invalid_source_manifest_is_sanitized_before_any_write(self):
        manifests = [None, [None], [{"path": "private/a.jpg"}], [{"path": "private/a.jpg", "size": 1, "sha256": "bad"}]]
        target = {"media_root": "/media", "references": 1, "missing": ["private/a.jpg"]}
        for value in manifests:
            with self.subTest(value=value), mock.patch.object(recovery, "query_target", return_value=target), mock.patch.object(recovery, "preflight_source", return_value=value), mock.patch.object(recovery, "publish_target") as publish, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(recovery.main(["--apply"]), 1)
                publish.assert_not_called()
                self.assertIn("ERROR:", error.getvalue())
                self.assertNotIn("Traceback", error.getvalue())

    def test_real_target_discovery_is_fresh_on_each_dry_run(self):
        class FileField:
            def __init__(self, name):
                self.name = name

        class Manager:
            def __init__(self, values):
                self.values = values
                self.calls = []

            def values_list(self, name, flat=False):
                self.calls.append((name, flat))
                return list(self.values)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media, source = root / "media", root / "source"
            media.mkdir()
            (media / "private").mkdir()
            (media / "private/present.jpg").write_bytes(b"present")
            (source / "private").mkdir(parents=True)
            (source / "private/a.jpg").write_bytes(b"a")
            (source / "private/b.jpg").write_bytes(b"b")
            values = ["private/present.jpg", "private/a.jpg", "private/a.jpg", "", None]
            manager = Manager(values)
            model = SimpleNamespace(_meta=SimpleNamespace(fields=[FileField("image"), object()]), objects=manager)
            configs = []

            def get_config(label):
                configs.append(label)
                return SimpleNamespace(get_models=lambda: [model] if label == "inspection" else [])

            modules = {
                "django": SimpleNamespace(),
                "django.apps": SimpleNamespace(apps=SimpleNamespace(get_app_config=get_config)),
                "django.conf": SimpleNamespace(settings=SimpleNamespace(MEDIA_ROOT=str(media))),
                "django.db": SimpleNamespace(),
                "django.db.models": SimpleNamespace(FileField=FileField),
            }

            def transport(command, **kwargs):
                self.assertEqual(command[-2], recovery.TARGET_ALIAS)
                self.assertIn("manage.py", command[-1])
                code = shlex.split(command[-1])[-1]
                result = execute_remote_helper(code, ["helper"], b"")
                return SimpleNamespace(returncode=0, stdout=result.encode("utf-8"), stderr=b"")

            plans = []
            def preflight(paths):
                plans.append(list(paths))
                return recovery.source_preflight(source, paths)

            with mock.patch.dict(sys.modules, modules), mock.patch.object(recovery.subprocess, "run", side_effect=transport) as run, mock.patch.object(recovery, "preflight_source", side_effect=preflight), mock.patch.object(recovery, "publish_target") as publish:
                self.assertEqual(recovery.main([]), 0)
                values.append("private/b.jpg")
                self.assertEqual(recovery.main([]), 0)
                self.assertEqual(run.call_count, 2)
                publish.assert_not_called()
            self.assertEqual(plans, [["private/a.jpg"], ["private/a.jpg", "private/b.jpg"]])
            self.assertEqual(manager.calls, [("image", True), ("image", True)])
            self.assertEqual(configs, list(recovery.APPS) * 2)

    def test_real_embedded_missing_source_aborts_apply_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            (root / "private/a.jpg").write_bytes(b"a")
            paths = ["private/a.jpg", "private/missing.jpg"]
            executions = []

            def transport(command, **kwargs):
                self.assertEqual(command[-2], recovery.SOURCE_ALIAS)
                args = shlex.split(command[-1])
                executions.append(args)
                try:
                    result = execute_remote_helper(args[4], ["helper", recovery.SOURCE_VOLUME], kwargs["input"], docker_mount=root)
                    return SimpleNamespace(returncode=0, stdout=result.encode("utf-8"), stderr=b"")
                except recovery.RecoveryError:
                    return SimpleNamespace(returncode=2, stdout=b"", stderr=b"source unavailable")

            with mock.patch.object(recovery, "query_target", return_value={"media_root": "/media", "references": 2, "missing": paths}), mock.patch.object(recovery.subprocess, "run", side_effect=transport), mock.patch.object(recovery, "publish_target") as publish:
                self.assertEqual(recovery.main(["--apply"]), 1)
                publish.assert_not_called()
            self.assertEqual(len(executions), 1)
            self.assertEqual(sorted(p.name for p in (root / "private").iterdir()), ["a.jpg"])

    def test_embedded_destination_rejects_size_only_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"abc"
            meta = {"media_root": str(root), "path": "private/a.jpg", "size": 4, "sha256": recovery.sha256_bytes(data)}
            encoded = recovery.base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")
            with self.assertRaisesRegex(recovery.RecoveryError, "size or checksum"):
                execute_remote_helper(recovery.TARGET_PUBLISH_CODE, ["helper", encoded], data)
            self.assertFalse((root / "private").exists())

    def test_embedded_destination_rejects_final_symlink_and_preserves_referent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.jpg"
            outside.write_bytes(b"preserve")
            (root / "private").mkdir()
            final = root / "private/a.jpg"
            final.symlink_to(outside)
            data = b"abc"
            meta = {"media_root": str(root), "path": "private/a.jpg", "size": 3, "sha256": recovery.sha256_bytes(data)}
            encoded = recovery.base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")
            with self.assertRaisesRegex(recovery.RecoveryError, "not a regular file"):
                execute_remote_helper(recovery.TARGET_PUBLISH_CODE, ["helper", encoded], data)
            self.assertTrue(final.is_symlink())
            self.assertEqual(outside.read_bytes(), b"preserve")
            self.assertEqual(list(root.rglob("*.part")), [])

    def test_dry_run_preflights_every_selected_file_and_never_publishes(self):
        paths = ["private/a.jpg", "public/measurement_types/b.png"]
        manifest = [{"path": p, "size": 3, "sha256": recovery.sha256_bytes(b"abc")} for p in paths]
        with mock.patch.object(recovery, "query_target", return_value={"media_root": "/media", "references": 3, "missing": paths}), mock.patch.object(recovery, "preflight_source", return_value=manifest) as preflight, mock.patch.object(recovery, "publish_target") as publish:
            self.assertEqual(recovery.main([]), 0)
            preflight.assert_called_once_with(paths)
            publish.assert_not_called()

    def test_apply_aborts_on_changed_source_without_copying_later_files(self):
        paths = ["private/a.jpg", "private/b.jpg"]
        manifest = [{"path": p, "size": 3, "sha256": recovery.sha256_bytes(b"abc")} for p in paths]
        with mock.patch.object(recovery, "query_target", return_value={"media_root": "/media", "references": 2, "missing": paths}), mock.patch.object(recovery, "preflight_source", return_value=manifest), mock.patch.object(recovery, "publish_target", side_effect=recovery.RecoveryError("source changed")) as publish:
            self.assertEqual(recovery.main(["--apply"]), 1)
            self.assertEqual(publish.call_count, 1)

    def test_source_changed_after_preflight_never_reaches_destination(self):
        item = {"path": "private/a.jpg", "size": 3, "sha256": recovery.sha256_bytes(b"abc")}
        with mock.patch.object(recovery, "read_source", return_value=b"xyz"), mock.patch.object(recovery.subprocess, "run") as run:
            with self.assertRaises(recovery.RecoveryError):
                recovery.publish_target(item, "/media")
            run.assert_not_called()

    def test_source_symlink_swap_before_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            target = root / "private/a.jpg"
            target.write_bytes(b"safe")
            outside = root / "outside.jpg"
            outside.write_bytes(b"outside")
            real_open = os.open

            def swap(name, flags, *args, **kwargs):
                if name == "a.jpg" and flags & os.O_RDONLY == os.O_RDONLY:
                    target.unlink()
                    target.symlink_to(outside)
                return real_open(name, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=swap), self.assertRaises(recovery.RecoveryError):
                recovery.read_source_file(root, "private/a.jpg")

    def test_destination_parent_swap_cannot_write_outside_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            root.mkdir()
            private = root / "private"
            private.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            real_open = os.open

            def swap(name, flags, *args, **kwargs):
                if name.startswith(".recover-media-"):
                    private.rename(root / "pinned")
                    private.symlink_to(outside, target_is_directory=True)
                return real_open(name, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=swap):
                recovery.publish_verified(root, "private/a.jpg", b"abc", 3, recovery.sha256_bytes(b"abc"))
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual((root / "pinned/a.jpg").read_bytes(), b"abc")

    def test_source_root_ancestor_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real/media/private").mkdir(parents=True)
            (root / "real/media/private/a.jpg").write_bytes(b"abc")
            (root / "alias").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(recovery.RecoveryError):
                recovery.source_preflight(root / "alias/media", ["private/a.jpg"])


if __name__ == "__main__":
    unittest.main()
