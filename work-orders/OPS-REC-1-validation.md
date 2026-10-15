# OPS-REC-1 validation - 2026-10-15

## Authorship and execution boundary

Initial implementation: fresh native worker `/root/recover_media_initial`, explicitly requested
codex/gpt-6-luna/high, with the committed WO and three-file ownership. Effective native routing
was not exposed. The parent integrated filesystem hardening and review/test corrections.
No production SSH, Docker execution, file recovery, secrets access or workflow dispatch occurred
during implementation or validation. Script readiness does not close Innoservice INNO-OPS-2.

## Authoritative local tests

Windows `python recover_missing_media.py --help`: exit 0.
Linux tests: local WSL Ubuntu, Python 3.12.3, an isolated `/tmp/ops-rec-1-*` temporary copy of
the script and test module; `python3 -m unittest -v test_recover_missing_media`:
**23 passed, zero skips, 0.080 s**. This includes the actual embedded remote helper code,
real Linux hardlinks/symlinks, fake Django FileField discovery and simulated SSH transport.
No network connection is made by these tests. The initial Windows filesystem-test failure was
a platform/sandbox issue; Linux is the remote helpers' actual target platform.

Coverage includes: dry-run/no writes; fresh deduplicated DB references on successive runs;
whole-source preflight; missing source; successful full apply with two files and final hashes;
existing/racing file preservation; repeat-run no-op; source changes; malformed metadata;
separate size/checksum errors; interruption cleanup; traversal, secret paths and symlinks at
root ancestors, parents and final paths; descriptor pinning during parent replacement; encoded
hostile path preservation. Shared filesystem functions are embedded from the tested source,
rather than maintaining a separate, untested implementation for the server.

## Independent static review

Four lenses in each concurrent batch: envelope, regression, duplication, tests. Separate
`codex exec --sandbox read-only --skip-git-repo-check -m gpt-6-luna` processes, medium effort,
inline frozen staged diff and scoped context, no approval/sandbox bypass. Final session metadata
confirmed all four effective models as `gpt-6-luna`, effort medium and sandbox read-only; all
processes exited 0. Final review session IDs:

- envelope: `01a13ec6-a500-7200-b357-7fad675f47ff`
- regression: 01a13ec6-a733-7d83-bbe4-ef1d27ad9edb
- duplication: `01a13ec6-a992-79f3-bebf-c676ba70c565`
- tests: `01a13ec6-ac4a-7b60-803a-9f10b7ad4440`

The parent logs under workspace `scratchpad/ops-rec-1` retain every pass. Duplication's optional
read-only shell check could not start in the child sandbox; its completed review used the
supplied capability inventory and existing DB-restore context. No capability-level duplicate
was identified. No runtime or independent code-review pass was omitted.

## Consolidated dispositions

Ten distinct concerns were raised across passes; seven accepted/fixed, three rejected with
evidence, zero open. Worst accepted severity: defect (P2).

1. Missing final-target-symlink test: accepted, actual helper rejection/referent preservation added.
2. Missing size-only mismatch test: accepted, independent actual-helper negative case added.
3. Fresh DB-query test gap: accepted, actual embedded query exercised on two changed datasets.
4. Missing real source-preflight failure path: accepted, actual helper fails before any writes.
5. Malformed response traceback: accepted, schemas validated and sanitized-error tests added.
6. Vacuous encoded-path assertion: accepted, decoded metadata must reproduce the entire path.
7. Destination execution-user concern: rejected. `innoservice` production `origin/main` Dockerfile
   declares `USER deploy` and directly runs Daphne; Compose has no user/command/entrypoint override.
   Docker exec uses that same configured user. This boundary is documented, not guessed.
8. No successful main apply proof: accepted, actual helpers through simulated transport prove
   successful copy counts, exact bytes/hashes, existing data and a second no-op run.
9. Negative tests could pass an always-failing main stub: rejected. Negative/error tests pin
   their own contracts; the companion successful-apply and successful dry-run tests fail such a
   stub. A negative test is not defective merely because it is complemented by positive coverage.
10. Encoded-command unit test does not execute SSH/a shell: rejected as an unsupported demand
    for broader evidence. The builder's contract is to preserve data outside shell syntax; the
    test decodes and compares the exact payload and the end-to-end simulated transport executes
    the actual encoded helpers. Existing OpenSSH execution is not reimplemented by this script.

No source changed after the final review. Only this evidence document, the WO execution record
and register closing metadata were added afterwards.
