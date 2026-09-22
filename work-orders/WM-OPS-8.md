# WM-OPS-8 — the janitor's unattended-upgrades check has never produced output on any host

Register row: `webapp-management/WORK_ORDERS.md` (`OPS-*` stream). Repo: `webapp-ops-scripts`.

# A. Envelope

## Goal & expected outcome

- Ziel: the janitor's pending-security-updates diagnostic must either report real information or
  not claim to exist.
- Expected outcome: every janitor run either lists what `unattended-upgrades` would install, or
  the step is gone. No run prints a section heading followed by a permission error.

## Why this is not cosmetic

In **every** janitor run, on **all five** hosts, the step produces exactly one line:

```
== Unattended-upgrades dry-run ==
You need to be root to run this application
```

Measured across the full matrix on 2026-09-22 (run 35677632522): research-prod, main-prod,
staging, innoservice-prod, mhaas-prod — identical on all five.

`janitor.sh:221-226` runs `unattended-upgrades --dry-run --debug` as the `deploy` user over SSH.
The command requires root. The output is piped through `head -n 80` and `|| true`, so the failure
is swallowed and the janitor continues and reports success.

The result is a heading in the log that reads as though the check ran. Nothing has ever been
reported about pending package updates on any host in this estate through this path. It does not
break the janitor — it removes the ability to notice that updates are outstanding, which is the
same silent-monitor class as WM-CI-21's container lookup and the WM-OPS-7 verification gap.

## Scope + non-goals

- In scope: `janitor.sh:221-226`. Two honest outcomes, and the choice between them is the
  operator's, recorded below.
- Explicit non-goals / do-not-touch:
  - Do NOT leave the step in place with the error swallowed. A diagnostic that cannot produce
    output is worse than an absent one, because the heading implies coverage.
  - Do NOT make the janitor **install** updates. This is a dry-run diagnostic; patching is
    `maintenance.yml`'s business and has its own reboot handling.
  - Do NOT make a failed dry-run fail the janitor. It is diagnostic; the disk threshold is the
    janitor's actual gate and must stay the only one.
  - Do NOT touch the disk-threshold logic, the prune steps, or the image retention — those are
    WM-INF-74's surface and are working as intended.
  - Do NOT touch sudoers, Ansible, or any file outside this repo — the rejected option (giving the
    step root) would have made this cross-repo; the chosen option does not.

## Operator decision — answered 2026-09-22

**Option 2: drop the step.** Delete it from `janitor.sh`. Pending-update visibility then rests on
`maintenance.yml` / `Maintenance - Reboot If Needed`, and the janitor stops implying a check it
never performed. The visibility this step was meant to give is formally given up — honest, since it
was never actually given (measured: identical "You need to be root" on all five hosts, every run).

## Tier · precondition / gate

- Tier: **3** — prod infrastructure, and specifically a monitor of it.
- Precondition: none remaining — the decision above is final, single-repo, ready to dispatch.

## Risks

- Removing the step deletes a log line some future reader may expect. Say so in the commit message
  rather than deleting quietly.
- `test_janitor.py` exists in this repo. **Already checked by the Orchestrator:**
  `webapp-management/.github/scripts/test_janitor_config.py` does NOT reference
  `unattended-upgrades` in any form — confirmed by grep on 2026-09-22, this WO stays single-repo.
  `test_janitor.py`'s generic `_make_stub_env`/`_stub_bin` helpers stub a fake `unattended-upgrades`
  binary on `PATH` for OTHER tests' hermeticity, but no existing test asserts on this step's output
  or presence — that stub becomes dead weight once the step is removed, not a test that breaks.

## Required tests to WRITE

Adjust/add to `test_janitor.py` so a test asserts the `== Unattended-upgrades dry-run ==` step is
**absent** from `janitor.sh`'s source — parse the script text and assert neither the heading string
nor an `unattended-upgrades` invocation remains. **Show this test failing against the CURRENT
(pre-change) script** (it currently contains both) before removing the step, so the mutation check
is real.

The authoritative evidence is the next scheduled janitor run's log on all five targets, read by the
Orchestrator — confirming the heading and the permission-error line are both gone.

---

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Context package

**Named file to change:** `janitor.sh` (repo root), lines ~219-226:

```bash
echo "== Unattended-upgrades dry-run =="
if command -v unattended-upgrades >/dev/null; then
  unattended-upgrades --dry-run --debug | head -n 80 || true
else
  echo "unattended-upgrades not installed"
fi
```

**What to change:** delete this block entirely (the `echo "== ... =="` heading and the
`if command -v ... fi` block both go — leave nothing that implies the check still runs). The
line immediately before it (`docker images --format ... | head -n 50 || true`) and the line
immediately after (`echo "== Disk threshold check ..."`) must remain exactly as they are, simply
now adjacent to each other.

**Test-side cleanup:** `test_janitor.py` stubs a fake `unattended-upgrades` binary in its shared
`_make_stub_env`/`_stub_bin`-style helpers (grep the file for `unattended-upgrades` — there are at
least two stub sites, one in a test-specific PATH setup and one in the shared `_make_stub_env`
helper) purely so the now-deleted step wouldn't touch the real system's apt state during tests.
Once the step is gone these stubs are dead weight, not load-bearing for anything else — remove them
if doing so is a clean, mechanical deletion; if a stub site is entangled with something else the
helper does, leave it and note why rather than risk breaking an unrelated test.

**Invariants / do-not-touch / pitfalls:**

- Do NOT touch the disk-threshold check, the prune steps, image retention, or any other diagnostic
  in this script — only this one block.
- Do NOT touch `webapp-management` — already confirmed nothing there references this step.
- Do NOT make the deletion conditional or flag-gated; this is a straight removal.

## Required tests to WRITE

See Part A's "Required tests to WRITE" above: one test asserting the heading/invocation is absent
from `janitor.sh`'s source, shown failing against the current (pre-change) script first.

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\webapp-ops-scripts`

## Preamble

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine;
> there is no separate plan file. Read the nearest `AGENTS.md` and the relevant
> `.codex/skills/<role>/SKILL.md` ONLY for conventions. Stay in scope; do not touch anything outside
> `janitor.sh` and `test_janitor.py`; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md`**
> (it lives in `webapp-management`, not this repo). **Your tools are for editing source and test
> files and for running the tests you wrote — nothing else.** Do NOT install dependencies, touch a
> lockfile, run a package manager, or tidy up stray files; if something in the repo state blocks
> you, stop and report it as `RESULT: BLOCKED <reason>` instead of fixing it. Do NOT `git add`/
> `commit`/`push` — leave every change uncommitted in the working tree for the orchestrator's
> independent review. WRITE the test the `Required tests` section calls for AND **RUN it** to
> confirm it executes, passes after your change, and FAILS against the pre-change script (mutation
> check) — that is the ONLY test run you do (NOT the repo's other tests, NOT any review). The
> orchestrator re-runs the authoritative set and does the independent review after you finish —
> those are the gate; your own run does not count.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

# C. Orchestrator only

> **Implementer: STOP at this line.**

## Execution directive

`codex exec` in the background, invoked directly via Bash with both `--skip-git-repo-check` and
`--dangerously-bypass-approvals-and-sandbox`, `-m` per `.claude/models.local.json` →
`implementation`, WO passed on stdin. Fallback to direct Claude implementation only on Codex
quota/rate-limit/non-zero exit; the fallback flips authorship, so the independent review becomes
mandatory either way (it already is, Tier 3).

## Review routing

Tier 3: all four `review` lenses. No `sec_reviewer` — the chosen option (drop the step) grants
nothing and touches no auth surface.

## Verification

Read the next scheduled janitor run on all five targets and confirm the heading and permission
error are both gone. A green janitor is not evidence here; a green janitor is what this defect has
been producing all along.
