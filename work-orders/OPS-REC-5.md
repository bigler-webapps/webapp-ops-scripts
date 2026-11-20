# OPS-REC-5 — backend health probe without curl (follow-up of OPS-REC-3 / WM-INF-129)

Pointer stub. Evidence and rollout live in `webapp-management/work-orders/WM-INF-129.md` -> Completion (Tier 3, infra lane).

# A. Envelope (this repo's part)

Why: the first real HRAM refresh (webapp-management run 44824436847) rolled back in the health phase. The in-container backend probe in
`stack_recovery.py` runs `curl`, and the hram backend image has none, so the probe can never answer 200 and every cutover on hram fails
and is rolled back (the same pattern ended the manual refresh of 2026-11-20 at 15:11).

- `stack_recovery.py`: the probe runs the container's own Python (`http.client`, no redirects, `X-Forwarded-Proto: https`) and prints the
  HTTP status, like the curl call it replaces. The host-side public-root check keeps using the host's curl.
- Tests: the probe command shape, and the probe script run against a local server (200, 302, 503) and against a closed port.
- Out of scope: the hram image (no curl added), the receiver, the pins and rollout (webapp-management).

Tier 3 (infra lane). Gate #1 approved by the operator 2026-11-20 (in session, "Yes, fix it": replace the in-container curl probe with the container's Python).

# C. Orchestrator only

Evidence and review: see `webapp-management/work-orders/WM-INF-129.md` -> Completion.
