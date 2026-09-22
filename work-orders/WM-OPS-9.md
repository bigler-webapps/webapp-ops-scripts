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

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Context package

**Named files to change:** `backup.py` (repo root) and `verify_backup.py` (repo root).

**`backup.py` — existing precedent to extend, not replace.** `record_b2_snapshot()`
(lines ~364-386) already writes one marker file for cross-script hand-off:

```python
LAST_B2_SNAPSHOT_ID_FILE = Path("/srv/backups/.last_b2_snapshot_id")
...
def record_b2_snapshot(snap_id: Optional[str]) -> None:
    if snap_id:
        try:
            LAST_B2_SNAPSHOT_ID_FILE.write_text(snap_id + "\n", encoding="utf-8")
        except OSError as exc:
            log(f"WARN: could not write {LAST_B2_SNAPSHOT_ID_FILE} ({exc})")
        log(f"B2_SNAPSHOT_CREATED={snap_id}")
    ...
main():
    ...
    perform_db_dumps()
    ok_local, snap_local = run_restic(RESTIC_REPO_LOCAL, "Local", unlock_stale=True)
    ok_b2, snap_b2 = run_restic(RESTIC_REPO_B2, "B2")
    record_b2_snapshot(snap_b2)
```

`perform_db_dumps()` (the function WM-OPS-7 just changed, `ba06a6f`) already builds each
successful dump's final `outfile` name inside its loop (`outfile = DUMP_DIR /
f"{safe_name}_{t['db']}_{completion_timestamp}.sql.gz"`, right before `os.replace(tmpfile,
outfile)`) but does not currently return or expose that list anywhere. It needs to.

**What to add, concretely:**

1. Have `perform_db_dumps()` collect `outfile.name` for every dump that actually succeeds (i.e.
   inside the `success_count += 1` path, not before) and return that list (or otherwise expose it
   to `main()` — your call, but `main()`'s current flat top-level structure suggests a simple return
   value is the least invasive).
2. Write a new marker file analogous to `LAST_B2_SNAPSHOT_ID_FILE` — e.g.
   `LAST_RUN_DUMPS_FILE = Path("/srv/backups/.last_run_dumps")` — containing that run's dump
   filenames, one per line (or JSON; match whatever is simplest to read back reliably — this file
   never needs to be human-diffed the way `.last_b2_snapshot_id` sort of is). Write it unconditionally
   once `perform_db_dumps()` returns (whether or not the dump list is empty — an empty list is itself
   meaningful information for the verifier, not an error at this point), analogous to how
   `record_b2_snapshot` is called unconditionally before the ok_local/ok_b2 gate.
3. **Do not tie this new marker's write to `record_b2_snapshot`'s success/failure semantics** — it
   records what dumps THIS RUN wrote, independent of whether the B2 snapshot itself later succeeds.
   If the run fails after dumping but before a B2 snapshot exists, the manifest correctly still
   describes "what was dumped"; `verify_backup.py` already has no snapshot to check against in that
   case and won't be misled.

**`verify_backup.py` — the selection logic to replace.** Current `main()` (~lines 219-270):

```python
explicit_id = read_snapshot_id_file(LAST_B2_SNAPSHOT_ID_FILE)
...
latest_id, snap_time_str = get_target_snapshot(env, host, explicit_id)
...
all_files = list_sql_gz_files(env, latest_id)
...
candidates = []
for f in all_files:
    ts = parse_timestamp_from_filename(f)
    if ts and (min_time <= ts <= max_time):
        candidates.append(f)
```

Replace the time-window filter with: read the new manifest file (same host path as the one
`backup.py` just wrote, since `verify_backup.py` already runs on the same host right after
`backup.py` in the same workflow step sequence — confirm this is still true by reading the calling
workflow if unsure); the candidates are then **exactly the manifest's filenames that are also
present in `all_files`** — not a time window, not "manifest ∩ everything close enough".

**Fail-closed requirements (the whole point of this WO):**

- Manifest file missing, unreadable, or empty when it names dumps that should exist → **verification
  fails** (return 1), with a message naming the problem. Do NOT fall back to `MAX_SNAPSHOT_WINDOW_HOURS`
  or to "verify everything in the snapshot" — both are explicitly rejected fixes (Part A).
- A filename the manifest names but that is **not** found in `all_files` (the dump never made it into
  the snapshot, or was renamed/lost) → verification fails, naming the missing filename specifically —
  do not silently drop it from the candidate set.
- A file present in `all_files` but NOT named by the manifest (an earlier run's dump, still inside the
  snapshot's retention) → correctly excluded from `candidates`, exactly as intended; this is the normal
  case (7 files for 2 dumps/run per Part A).
- Every manifest-named file that IS found still goes through the existing `gzip -t` integrity stream —
  do not weaken that check, only change which files reach it.
- Keep `VERIFY_ALL_SQL_GZ=1` working exactly as it already does (bypasses selection entirely, verifies
  every file) — it is an existing escape hatch, out of this WO's scope to touch.

**Invariants / do-not-touch / pitfalls:**

- Do not touch `parse_timestamp_from_filename`, `MAX_SNAPSHOT_WINDOW_HOURS`, `choose_snapshot`, or
  `get_target_snapshot` — the snapshot-selection machinery is unaffected; only the DUMP-selection
  step within an already-chosen snapshot changes.
- Do not touch `janitor.sh`, the restic repos, retention policy, or `generate_paths.py`.
- The manifest is per-HOST (same directory as `LAST_B2_SNAPSHOT_ID_FILE`, which is host-local, not
  synced) — do not try to make it travel with the snapshot itself or embed it inside the B2 repo.
- A dump that fails mid-write never reaches the `success_count += 1` line (see WM-OPS-7's diff) —
  it will correctly never appear in the manifest; do not add separate failure-handling for this,
  the existing `fail("Database dump failed...")` path already aborts the whole run.

## Required tests to WRITE

Part A's six cases are the complete spec, in this repo's existing `pytest` style (see
`test_backup.py`/`test_verify_backup.py` — NOT `unittest`, despite what a sibling repo's convention
might suggest; check the existing files' own imports/fixtures, e.g. `monkeypatch`, before writing).
Case 1 and case 5 are load-bearing (case 1 must fail against today's window-based code — confirm
this before your fix, not after).

Work from this package; do not explore broadly beyond reading `test_backup.py`/`test_verify_backup.py`
for fixture conventions and the calling workflow (if needed) to confirm host-locality of the marker
files.

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\webapp-ops-scripts`

## Preamble

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine;
> there is no separate plan file. Read the nearest `AGENTS.md` and the relevant
> `.codex/skills/<role>/SKILL.md` ONLY for conventions. Stay in scope; do not touch `janitor.sh`,
> `generate_paths.py`, `restore.py`, restic config, retention policy, or any file in
> `webapp-management`; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md`** (it lives in
> `webapp-management`, not this repo). **Your tools are for editing source and test files and for
> running the tests you wrote — nothing else.** Do NOT install dependencies, touch a lockfile, run
> a package manager, or tidy up stray files; if something in the repo state blocks you, stop and
> report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT `git add`/`commit`/`push` —
> leave every change uncommitted in the working tree for the orchestrator's independent review.
> WRITE the tests the `Required tests` section calls for AND **RUN the tests you just wrote**
> (`pytest`, matching this repo's existing convention) to confirm they execute and pass, and that
> case 1 genuinely FAILS against the pre-change window-based selection — that is the ONLY test run
> you do (NOT the repo's other tests, NOT any review). The orchestrator re-runs the authoritative
> set and does the independent review after you finish — those are the gate; your own run does not
> count.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

# C2. Orchestrator only — execution directive

> **If you are the implementer reading this as your specification: STOP, this section is not
> yours.**

`codex exec` in the background, invoked directly via Bash with both `--skip-git-repo-check` and
`--dangerously-bypass-approvals-and-sandbox`, `-m` per `.claude/models.local.json` →
`implementation`, WO passed on stdin. Fallback to direct Claude implementation only on Codex
quota/rate-limit/non-zero exit; the fallback flips authorship, so the independent review becomes
mandatory either way (it already is, Tier 3).

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
