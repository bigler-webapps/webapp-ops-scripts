# Recover missing Innoservice media

`recover_missing_media.py` compares current FileField references in the Innoservice
production database with files under the application's configured `MEDIA_ROOT`. It
selects only referenced files that are currently absent, then checks those paths in
the `innoservice_prod_media_volume` on `main-prod`.

Run from Windows where the existing OpenSSH aliases and Python 3 are available:

```cmd
python recover_missing_media.py
```

The default is read-only. It prints the current reference and missing counts and
preflights each selected source file's size and SHA-256. Startup output from Django
is ignored unless the framed query result is present and valid. A failed or unsafe
preflight exits nonzero before any destination write.

To explicitly transfer the preflighted files to `innoservice-prod`, preserving
their relative paths, run:

```cmd
python recover_missing_media.py --apply
```

The script rechecks the transferred bytes and publishes each file atomically. It
does not replace an existing destination file, and an interrupted transfer leaves
no partial final file. A later run recalculates the missing set, so completed files
are skipped. The script does not restart services, change the database, or access
any other media files. `--apply` performs production file writes; its availability
does not itself authorize an operator to run the recovery.

The local command runs on Windows or Linux; filesystem helpers run on the two Linux
servers. They resolve paths using pinned directory descriptors with `O_NOFOLLOW`,
so symlink replacement cannot redirect the writes outside the media tree. A changed
source or failed destination operation stops the run; files already completed remain
valid and are skipped on the next run.

Every SSH invocation disables pseudo-terminal allocation with `-T`. This matters when
the script runs from a Windows console: a PTY can convert LF bytes in the source stream
to CRLF, causing the size or SHA-256 check to fail before destination publication.
The recovery attempt reported on 2026-10-15 stopped at that source checksum check, with
zero files copied. After installing this correction, rerun the default read-only
preflight and inspect its current missing-file list and hashes. If that output is
expected and the operator has authorized production writes, run the command again with
`--apply`; the script recalculates the missing set and verifies every transferred file
before publishing it.

Destination writes use Docker's configured execution user. Innoservice's deployed
image declares `USER deploy` and launches Daphne directly as that user, so the
helper and application use the same identity; the script does not override it to root.

For a local-only test of the Linux helpers, run `python3 -m unittest -v
test_recover_missing_media` in a Linux checkout or temporary copy of these two files.
The tests use only temporary directories and mocked transports, with no remote access.
