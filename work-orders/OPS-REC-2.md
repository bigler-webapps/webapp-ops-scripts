# OPS-REC-2: Keep recovery SSH transport binary-safe from a Windows console

# A. Envelope

## Goal and outcome

Correct the OPS-REC-1 recovery helper so an interactive Windows invocation transfers exact
file bytes regardless of terminal preferences on the existing SSH aliases. Every SSH command
must explicitly disable pseudo-terminal allocation (`-T`). Preserve all existing recovery guards.

Root cause verified on 2026-10-15 with synthetic bytes only: under a real Windows console,
the existing source invocation changes two LF bytes into CRLF (34 bytes become 36). Disabling
the terminal returns the exact original 34 bytes. The preflight uses piped stdin and therefore
does not expose this console-only difference. The reported first-file checksum failure occurred
before any destination write; the operator's log reports copied 0, skipped 0, failures 1.

## Scope, non-goals and risks

- Add explicit terminal suppression to the central SSH command construction so inventory,
  preflight, source transfer and destination publication all inherit it.
- Add regression coverage that fails if terminal suppression is removed and exercises actual
  transfer orchestration with LF/CRLF, NUL and non-ASCII binary bytes through a simulated
  terminal-sensitive transport. Check exact bytes and hash, not merely a successful exit.
- Document the console-specific failure and the corrected rerun procedure.
- Keep default dry-run, strict checksums, source/target containment, no-overwrite publication,
  command quoting, timeouts, fresh missing-file selection and stop-on-first-failure unchanged.
- Non-goals: no actual production recovery, SSH configuration changes, secrets, dependencies,
  DB/schema/service changes, retry framework or disabling checksum verification.
- Tier 3: production data-recovery tooling. Risk is silent transport corruption; disabling the
  terminal must not weaken path validation, hash validation or atomic target publication.

## Required tests to write

- Central SSH builder disables terminal allocation for both fixed aliases while retaining
  BatchMode, connection timeout and RemoteCommand override.
- A transfer using a simulated console/PTY-sensitive transport preserves mixed binary data
  because of the actual command options, and fails if the terminal-suppression flag is removed.
  Exercise both source stdout and destination stdin, preserving checksum/no-write behavior.

# B. Implementation map — ADDRESSED TO IMPLEMENTER

Target: `C:/Users/Micha Bigler/Documents/webapps/webapp-ops-scripts`.
Own only `recover_missing_media.py`, `test_recover_missing_media.py`, `recover_missing_media.md`.
You are not alone in the checkout: preserve other edits and the existing untracked __pycache__.

`recover_missing_media.py:29` defines SSH_BASE; `ssh_command()` is the shared builder used by
query_target, preflight_source, read_source and target_publish_command. Source payload uses
raw stdout bytes; target publication pipes raw bytes to docker exec -i. Do not transform payloads.
The existing tests execute shared remote helpers in temporary Linux directories and have a
CommandConstructionTests class plus mocked source/target transport flow. Keep those tests intact.
The usage document already describes dry-run/apply/no-overwrite behavior. Work from these seams;
do not explore other repos or attempt server access.

PREAMBLE: This file is the COMPLETE spec. Use explicit UTF-8 I/O. Tools may only read these named
files, edit owned source/test/doc files and run your newly written tests ONCE. No SSH/network,
Docker, dependencies, git add/commit/push, register/memory edits, agents or self-review. Do not
read .env or SSH configuration. Leave changes uncommitted. Emit PLAN, progress before edits and
at least every two minutes, then RESULT: DONE or BLOCKED. Your test run is not the gate.

# C. Orchestrator only — STOP: not addressed to the implementer

Gate #1: operator explicitly requested this diagnosed correction and tests in this chat on
2026-10-15. No production mutation authorized. Initial implementation codex/gpt-6-luna, fresh
native worker, workspace-root bootstrap and explicit target above. Independent readonly review
batch: envelope/regression/duplication/tests, configured gpt-6-luna. Parent runs all recovery
tests in Linux plus focused Windows transport tests and a synthetic-only actual SSH probe from
a real Windows console, using the corrected helper. Review required before source commit/push.
Record this regression of a156810 (previous four-lens Luna review missed interactive SSH/PTY
coverage). Publish the corrected script and usage, not a production copy; operator reruns apply.
