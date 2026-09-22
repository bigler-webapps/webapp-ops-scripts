# WM-OPS-9 — verification checks the last dump of a run and reports success for the rest

Register row: `webapp-management/WORK_ORDERS.md` (`OPS-*` stream). Repo: `webapp-ops-scripts`.

# A. Envelope

## Goal & expected outcome

- Ziel: a backup run is verified when **every dump it produced** has been checked, not when at
  least one has.
- Expected outcome: `Found N matching dumps` stops being a statistic and becomes an assertion —
  N equals the number of dumps this run wrote, and the target fails when it does not. A run whose
  dumps are all present and intact stays green; a run missing any one of them goes red.

## Why this exists

WM-OPS-7 fixed the producer side: each dump's filename now carries its own completion instant
(`ba06a6f`). Its confirming run, 35715142842 on research-prod (2026-09-22), is green — and the log
says:

```
[10:17:53] Dump OK: hpc-bridge_production-db-1 / hpc_bridge_db (140 KiB)
[10:30:02] Dump OK: hram_production-db-1 / hram_db (1734081 KiB)
-> Snapshot Date:      2026-09-22T10:30:15Z
   Window: 2026-09-22 10:18:15 UTC  ..  2026-09-22 10:35:15 UTC
-> Found 1 matching dumps out of 7 .sql.gz files.
[SUCCESS] Backup is FRESH and VALID
```

That run produced **two** dumps. One was verified. hpc-bridge's finished at 10:17:53, twenty-two
seconds before the window opens, so its gzip integrity was never checked — and the target reported
success.

`verify_backup.py` was not touched by WM-OPS-7, and its window is still anchored to the **snapshot**
(`MAX_SNAPSHOT_WINDOW_HOURS = 0.2`, `min_time = snap_time - 12min`). Before WM-OPS-7 every dump of
a run shared one timestamp, so either all of them matched or none did; on research-prod that meant
none, and the target went loudly red. Per-dump timestamps removed the false red and, in the same
move, made a *partial* verification possible for the first time — and partial verification here is
silent. On any host where one database dump takes longer than the window, every dump that finished
before it is now unverified-but-green, and the count in the log is the only trace.

This is a consequence of WM-OPS-7, not a regression from it. WM-OPS-7's own goal was met.

## Scope + non-goals

- In scope: `verify_backup.py`'s selection of which dumps to verify, and the pass/fail condition
  that follows from it. The selection should be driven by **what this run actually wrote**, not by
  a time window: `backup.py` knows that set, and already writes one piece of run state for the
  verifier to read (`/srv/backups/.last_b2_snapshot_id`), so the same channel can carry the dump
  list.
- In scope: `backup.py`, only as far as recording that list.
- Explicit non-goals / do-not-touch:
  - **Do NOT widen `MAX_SNAPSHOT_WINDOW_HOURS`.** This is the second WO to reject that patch. It
    scales with database growth rather than with correctness, and it would paper over exactly the
    partial-verification case this WO exists to close.
  - Do NOT set `VERIFY_ALL_SQL_GZ=1` as the fix — verifying every `.sql.gz` in the snapshot
    (7 files, two days of retention) removes the freshness property rather than fixing its scope.
  - Do NOT weaken the failure path. A snapshot missing a dump the run wrote must fail the target.
  - Do NOT revisit the per-dump timestamp: it is correct and is what makes a per-run manifest
    meaningful.
  - Do NOT change what is backed up, the restic repositories, retention, or `janitor.sh`.

## Tier · precondition / gate

- Tier: **3** — backup verification. A fault here is silent by construction: it does not break the
  backup, it removes the ability to notice that a backup is incomplete. That is what this row
  records in the first place.
- Precondition: none. WM-OPS-7 has landed and is confirmed.

## Risks

- **A new coupling between the two scripts, and it must fail closed.** If the manifest is missing,
  stale, unreadable, or from a previous run, verification must FAIL — not skip, not fall back to
  the old window. A verifier that silently degrades to "check whatever is nearby" is the current
  defect wearing a new mechanism.
- **The snapshot legitimately contains dumps from earlier runs** (7 files for 2 dumps/run and two
  days of retention). Requiring "every dump in the snapshot" would go red every night; requiring
  "every dump of THIS run" is the correct bound. Do not conflate them.
- A dump that fails mid-write leaves no final file (`backup.py` writes a `.tmp` and renames). The
  manifest must record what was actually completed, or the verifier will demand a file that was
  never meant to exist. The existing dump-failure path already fails the run; do not add a second,
  differently-worded failure for the same event.
- Hosts differ in how many databases they carry (research-prod 2, main-prod more). The assertion
  must be "all of this run's", never a hardcoded count.

## Required tests to WRITE

Beside `test_backup.py` / `test_verify_backup.py`, in this repo's existing style:

1. A run wrote two dumps, both present in the snapshot → both are verified and the run passes.
   **This must fail against the current code**, which verifies only the one inside the window.
2. A dump that completed 30 minutes before the snapshot — well outside the old window — is still
   verified. That is precisely the hpc-bridge case measured above.
3. A run wrote two dumps and one is absent from the snapshot → the run FAILS, naming the missing
   dump.
4. Dumps from an earlier run that are present in the snapshot are neither required nor verified.
5. Manifest missing / unreadable / referring to a different run → FAILS, and specifically does not
   fall back to the time window. Assert the failure reason, not just the exit code.
6. A dump present but corrupt still fails the gzip check, as today — the existing guarantee must
   survive the change in selection.

---

# B. Implementation map — filled by the Orchestrator at dispatch time

PLACEHOLDER — not yet filled. This WO is registered, not dispatched. Do not invoke an implementer
on this file while this line is present.

---

# C. Orchestrator only

> **Implementer: STOP at this line.**

## Review routing

Tier 3: all four `review` lenses. No frontend, no auth surface. The Orchestrator's own pass covers
the fail-closed behaviour of the new manifest coupling — that is the only way this WO can make
things worse than they are now.

## Verification

1. This repo's `test_backup.py`, `test_verify_backup.py` and the new cases — the affected set.
2. The mutation check on test 1: it must go red against the current window-based selection.
3. Read the first real run after the scripts sync to the hosts, on **all five targets** — the
   selection logic applies everywhere and four of the five are green today for reasons this change
   could disturb. The line to read is `Found N matching`: N must equal the number of `Dump OK`
   lines above it in the same log, on every target.
4. `backup.yml` is not on the workflow-dispatch safe-allowlist. A dispatch to confirm needs the
   operator to name workflow and target.
