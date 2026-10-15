# OPS-REC-1: Recover missing Innoservice media from the old server

# A. Envelope

## Goal and expected outcome

Provide one standalone Python-standard-library operator script, `recover_missing_media.py`,
which can be launched from Windows with the existing OpenSSH aliases. Default to a read-only
dry run. `--apply` explicitly opts into transferring only currently missing, DB-referenced
Innoservice media from `main-prod` to `innoservice-prod`, preserving their exact relative paths.

The 2026-10-15 read-only investigation found 61 unique missing media references, all present
and non-empty in the old source volume. Recompute current missing references on each run;
61 is evidence, not a hardcoded quota. Never copy every file from the old volume indiscriminately.

## Scope and non-goals

- Source alias `main-prod`, Docker volume `innoservice_prod_media_volume`.
- Target alias `innoservice-prod`, backend container `innoservice_production-backend-1`.
- Query the target application's FileFields read-only in measurements, inspection, structures,
  protocols, bills and people. Use its actual MEDIA_ROOT and existing application configuration.
- Before any apply writes, preflight all selected source files; validate paths and compute sizes
  and SHA-256. Missing/unreadable source files, unexpected paths or mismatches abort nonzero.
- Never overwrite an existing target file, including a file created during the run. Publish each
  verified completed file atomically; a failed transfer must not leave a partial final file.
- Preserve content and relative names. Use the destination backend's execution user so restored
  files can be read and maintained by the application. Source files remain unchanged.
- Protect source and target containment: reject traversal, absolute paths, symlinks (including
  parent components), secret/.env paths and shell injection; do not follow links outside media.
- Print a concise plan and copied/skipped/failure summary, return nonzero on failures, support
  safely rerunning after partial progress. No secret values or raw application exception dumps.
- No dependency installation, secrets retrieval, .env inspection, DB writes, schema, services,
  restarts, CI/workflow edits, staging sync or production execution as part of this WO.
- Provide a short companion usage document. Preparation is approved; the actual recovery remains
  an operator action, separate from this task. The script is not a production-access approval.

## Tier and risks

Tier 3: production data recovery tooling. Main risks: replacing newer media, incomplete files,
path/symlink escape, shell interpolation, stale manifests, insufficient permissions and leaks.
The operator approved this concrete scope in chat on 2026-10-15. No production mutation is approved.

## Required tests to write

Use local temporary directories and mocked transports only; tests must never connect to SSH or Docker.
Pin dry-run/no-write default, fresh DB-reference deduplication, only-missing selection, missing
source failing before writes, exact successful copy and hash, checksum/size mismatch with no
published final file, existing-file/racing-file preservation, interrupted copy cleanup/resume,
path traversal/absolute/backslash/secret paths, source/target parent/final symlinks and command
construction with hostile input. Exercise real copy/validation code, not only mocks of itself.

# B. Implementation map - ADDRESSED TO THE IMPLEMENTER

## Target and ownership

Target repo: `C:/Users/Micha Bigler/Documents/webapps/webapp-ops-scripts`.
Own only these NEW files: `recover_missing_media.py`, `test_recover_missing_media.py`,
`recover_missing_media.md`. No other file edits.

## Context package

This repo contains Python-standard-library operations scripts; no packaging or test dependency
is needed. Existing `restore.py::mode_in_place` restores a database and is unsuitable for this
media-only task. `README.md` describes the existing workflow-based deployment of ops scripts;
do not add a new deployment/CI path or invoke the existing DB restore from this script.

Verified production metadata (do not query servers during implementation):
- Source Docker volume Mountpoint `/var/lib/docker/volumes/innoservice_prod_media_volume/_data`
  on main-prod; 148 files. Resolve the named volume through Docker inspection rather than
  assuming the Docker storage root when implementing.
- Target media is mounted at `/app/backend/media` in the named backend container, 90 files.
  Discover MEDIA_ROOT from the application. `python manage.py shell -c` can perform the readonly
  FileField query; startup chatter precedes its result, so frame the expected JSON robustly.
- Relevant apps: measurements, inspection, structures, protocols, bills, people. Enumerate
  actual FileFields, exclude empty references, deduplicate paths. Expected paths are under
  `private/` or `public/`; root legacy files are not current missing references.
- SSH aliases already resolve routing/credentials. Never read SSH config, environment files or
  printenv. `ssh -o RemoteCommand=none <alias> <command>` avoids alias RemoteCommand conflict.
  Use SSH BatchMode and bounded connection errors. Do not disable host-key checking.
- Source Docker inspection is available to deploy; source filesystem reads use `sudo -n`.
  Destination inventory and writes run in the existing backend container through docker exec;
  no docker run/cp, helper containers, service lifecycle or SSH key/config provisioning.
- Windows local orchestration, Linux remote helpers. A single self-contained file may send its
  own bounded helper code over SSH. Transfer file bytes without shell interpolation or unsafe
  archive extraction. No third-party libraries. Keep the design specific to this recovery.

Implement within the Envelope, deciding the code structure yourself. Expose transport seams
so local tests can exercise the remote-helper filesystem logic without actual SSH. Do not
broaden into a general backup platform. If a necessary condition cannot be met, report it.

## Preamble

The above is the COMPLETE spec. You are not alone in the shared checkout: preserve others'
changes, including unrelated __pycache__. Use explicit UTF-8 I/O and the absolute target path.
Read only this WO and README.md/restore.py as needed. Edit only the three owned source/test/doc
files. Your tools may edit these files and run the tests you wrote ONCE; no other operations.
No network, SSH, Docker, installs, package managers, git add/commit/push, register/memory edits,
self-review or agent spawning. Do not read .env or SSH config. Leave the diff uncommitted.
Your own test run is not the authoritative gate. Emit PLAN, then PROGRESS at least every two
minutes, then RESULT: DONE or RESULT: BLOCKED with reason. Report the exact test command/result.

# C. Orchestrator only - NOT ADDRESSED TO THE IMPLEMENTER

> IMPLEMENTER STOP HERE. The following is not your task list.

Gate #1 approved in this chat 2026-10-15: creation/tests only. No production copy or workflow
dispatch. The parent uses orchestrate-codex and requested native codex/gpt-6-luna with fresh
context. Native bootstrap cwd is the workspace root; target operations use the explicit repo
directory above. Effective child model/provider/containment metadata is unverified if not exposed.

Initial worker writes the three owned files. Parent integrates findings, runs local unittest and
CLI-help checks. Review: four configured independent codex/gpt-6-luna lenses concurrently on
the same unmodified diff, static-only, read-only, no bypass; parent additionally checks path,
transport and publication safety. No UI review; no auth/permission logic changes.

Finalize script readiness only on clean independent review and scoped tests. Commit/push own
files to webapp-ops-scripts/main; no deployment is triggered by this source addition. The
production recovery remains unexecuted and needs its separate operator-controlled run.

Execution record, 2026-10-15: initial native worker `/root/recover_media_initial`, requested
gpt-6-luna/high; effective native metadata unexposed. Parent integrated descriptor-based path
hardening and review fixes. Final authoritative local Linux run: 23 tests passed, zero skips;
Windows CLI help exit 0. Four final independent CLI lenses verified as gpt-6-luna/medium with
read-only sandbox and exit 0. Ten distinct review concerns, seven accepted/fixed and three
rejected with evidence, zero open; worst accepted: defect. See
[validation and dispositions](OPS-REC-1-validation.md). No production run performed.
