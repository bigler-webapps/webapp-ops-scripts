# OPS-REC-2 validation — 2026-10-15

## Outcome

All recovery SSH invocations now explicitly disable pseudo-terminal allocation with `-T`.
No production files were copied or modified during validation. The operator can rerun the
existing dry-run/apply commands with the corrected local script; no server installation is needed.

## Authoritative parent verification

- Full recovery unittest set: **25 passed, zero skips**, 0.104 seconds. WSL Ubuntu Python 3.12.3,
  isolated temporary copies of the script/tests; real Linux filesystem helpers and mocked
  transports only. Existing containment, symlink, checksum, atomic publish, no-overwrite and
  resume tests remain green.
- Focused Windows command/transport checks: **3 passed**, 0.006 seconds.
- Mutation check: removing `-T` in process memory makes both new regression cases fail
  (three assertion failures including two alias subtests, zero errors). No source was modified.
- Actual Windows console test, `stdin.isatty() == True`: the unchanged production SSH builder
  transports 261 synthetic bytes, covering all byte values plus LF/CRLF/NUL, exactly. Source
  stdout and piped destination stdin were each verified on both fixed aliases: **4/4 passed**.
  Expected and received SHA-256 in every case:
  `8910e096775d64cae4704b37ed8cff357f9037f3ce85205ac4bf7ce9bb6dc4dc`.
  Remote commands only emitted fixed synthetic bytes or echoed synthetic stdin; no customer
  files were read or written. The original interactive invocation had reproduced LF-to-CRLF
  corruption (34 bytes became 36); disabling the terminal restored the exact 34 bytes.
- Actual corrected script dry-run: **97 references, 61 missing, copied 0, skipped 0, failures 0,
  exit 0**. Only target FileField/path existence and source sizes/hashes were inspected.
  No customer payload was transferred locally or published. A diagnostic wrapper's initial
  UTF-8 decode failed on cp1252 output; rerunning the child with Python UTF-8 mode corrected
  only the wrapper and completed successfully. The script source did not change.

Parent evidence logs: workspace `scratchpad/ops-rec-2/` (`linux-tests.log`, `mutation-test.log`,
`console-transport-result.json`, `dry-run.log`, four independent review logs).

## Independent reviews

Concurrent fresh `codex exec` processes, configured `gpt-6-luna`, medium effort, read-only
sandbox, inline authoritative staged diff and scoped Envelope/evidence. All exited 0 and
reported no findings. Each run's own turn metadata confirmed model, effort and read-only mode:

- envelope: `01a13f81-f441-7fd2-82a8-c758b7f7dde4`
- regression: `01a13f81-f6a7-7e53-8185-1f6b3d59feb8`
- duplication: `01a13f81-f959-7612-9e68-27b3d89fd094`
- tests: `01a13f81-fbd7-70b3-ab1b-5c24a29f7fef`

Consolidated: 0 raised, 0 accepted, worst accepted none, 0 open. Tests lens notes that the
changed-source negative guard is complementary coverage, while the central-option and binary
transport assertions specifically kill the original defect. No runtime/review waiver.
Parent exposure pass: only terminal suppression changes executable behavior; the existing
hash validation and filesystem/overwrite boundaries remain unchanged.

Initial implementation: fresh native `/root/recover_terminal_fix`, requested explicit
`codex/gpt-6-luna/high`, workspace-root bootstrap/explicit repo paths. It changed only the three
assigned source/test/usage files, ran its two new tests once and left the diff uncommitted.
Effective native model metadata was not exposed. The parent ran all authoritative checks.

## Limits

No end-to-end production recovery is claimed. Actual production destination permissions and
customer-file publication are exercised only when the operator runs `--apply`. The existing
strict hash check still stops a real changed source or corrupted transfer before publication.
Original regression: a156810, reviewed by four Luna lenses; mocked transport tests missed the
interactive SSH terminal behavior. This correction adds regression coverage and real synthetic
console evidence for that specific gap.
