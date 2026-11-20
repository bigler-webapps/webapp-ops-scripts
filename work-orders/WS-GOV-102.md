# WS-GOV-102 · Orchestrator tooling (pointer)

Pointer stub. The work order, its Envelope and completion live in the workspace root: `webapps/work-orders/WS-GOV-102.md` (Tier 2, infra lane).

Effect in this repo: a new `project.yaml` with a `tests:` block, scope `ops-scripts` under WSL (`python3 -m pytest -q .`), run with `python -I .claude/scripts/tests.py --repo webapp-ops-scripts`. No other change here.
