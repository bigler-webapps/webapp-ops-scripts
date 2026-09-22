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
  - Do NOT grant the `deploy` user broad sudo. If option 1 is chosen, the sudoers entry must be
    narrowed to this one command.

## Operator decision required before dispatch

1. **Give it root, narrowly.** Run it as `sudo -n unattended-upgrades --dry-run --debug`, plus a
   single NOPASSWD sudoers entry for exactly that command, provisioned through Ansible (the boxes
   are Infrastructure-as-Code; an imperative `visudo` on a managed host is out per AGENTS.md).
   Cost: a sudoers change across five hosts, in the `webapp-management` ansible role — so this
   option makes the WO cross-repo and pulls in a second, gated change.
2. **Drop the step.** Delete it from `janitor.sh`. Pending-update visibility then rests on
   `maintenance.yml` / `Maintenance - Reboot If Needed`, and the janitor stops implying a check it
   never performed. Cost: whatever visibility this was meant to give is formally given up — which
   is honest, since it was never actually given.

Recommendation: **2**, unless pending-update reporting is wanted, in which case the honest form is
1 and it should be scoped as its own ansible WO rather than smuggled in here.

## Tier · precondition / gate

- Tier: **3** — prod infrastructure, and specifically a monitor of it.
- Precondition: the decision above. Option 1 additionally blocks on the ansible change landing
  first, or the janitor will simply print a sudo error instead of a root error.

## Risks

- Option 1 adds a sudo grant on five production hosts. Narrow or not, it widens what the `deploy`
  user can do, and `deploy` is the account CI authenticates as. `unattended-upgrades --dry-run`
  is not a shell, but the entry must not be written in a form that admits extra arguments.
- Option 2 removes a log line some future reader may expect. Say so in the commit message rather
  than deleting quietly.
- `test_janitor.py` exists in this repo and `test_janitor_config.py` in `webapp-management`.
  Either may assert on the step's presence; check both before removing anything.

## Required tests to WRITE

For option 2: adjust the existing janitor tests so they assert the step is **absent**, and check
whether `webapp-management/.github/scripts/test_janitor_config.py` references it — if it does,
that repo needs a matching change and this WO becomes cross-repo. Report rather than edit the
other repo.

For option 1: a test that the invocation is `sudo -n` and non-interactive, so a missing sudoers
entry surfaces as a clean failure rather than a password prompt that hangs the SSH session.

Either way the authoritative evidence is the next janitor run's log, read by the Orchestrator.

---

# B. Implementation map — filled by the Orchestrator at dispatch time

PLACEHOLDER — not yet filled, and blocked on the operator decision above. Do not invoke an
implementer on this file while this line is present.

---

# C. Orchestrator only

> **Implementer: STOP at this line.**

## Review routing

Tier 3: all four `review` lenses. Option 1 additionally requires `sec_reviewer` — it is a sudo
grant on production hosts for the account CI logs in as.

## Verification

Read the next scheduled janitor run on all five targets and confirm the step either reports real
package information or is absent. A green janitor is not evidence here; a green janitor is what
this defect has been producing all along.
