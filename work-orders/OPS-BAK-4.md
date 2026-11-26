# OPS-BAK-4 — research-prod restore paths for its own B2 repository (part of WM-INF-146)

Script and workflow part of `webapp-management/work-orders/WM-INF-146.md`; the Envelope, risks, tests and Gate record live there.

## A. Envelope

In this repository: research-prod's backup and every script here that opens its B2 repository use the new repository for snapshots after the switch and can still reach its old snapshots in the shared repository; main-prod's paths stay unchanged. The restore, check and init workflows live in webapp-management (WM-INF-146).

**Tier 3** (production backups, credentials, infra lane).

### Required tests

- research-prod resolves to the new repository after the switch and to the shared one before it; main-prod unchanged.
- A missing new credential refuses before restic opens a repository.

## B. Implementation map

- `backup.py` (`RESTIC_REPO_B2` `:28`), `test_backup.py`; `stack_recovery.py` if it opens research-prod's B2 repository.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this Envelope, WM-INF-146 and its Gate record, the nearest governance and the relevant role skill.
> Edit only the named scripts and their tests and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

Coordinate with WM-INF-146.
