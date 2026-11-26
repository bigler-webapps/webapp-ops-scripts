# OPS-BAK-3 — Safe stale-lock removal for B2 (part of WM-INF-147)

Script and workflow part of `webapp-management/work-orders/WM-INF-147.md`; the Envelope, risks, tests and Gate record live there.

## A. Envelope

In this repository: `backup.py` runs plain `restic unlock` (never `--remove-all`) on the B2 repository before its B2 work, and every script here that opens a B2 repository refuses a restic below 0.16 before opening it. The workflows live in webapp-management (WM-INF-147).

**Tier 3** (production backups, infra lane).

### Required tests

- B2 unlock is plain `restic unlock`, before B2 work, never `--remove-all`.
- restic below 0.16 is refused before any B2 repository is opened, in `backup.py` and every other script here that opens B2.

## B. Implementation map

- `backup.py` (`run_restic` `:381`, `:393-403`, calls `:500-501`), `test_backup.py`; any other script here that runs restic against B2 (`stack_recovery.py` runs restic).

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this Envelope, WM-INF-147 and its Gate record, the nearest governance and the relevant role skill.
> Edit only the named scripts and their tests and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

Coordinate with WM-INF-147.
