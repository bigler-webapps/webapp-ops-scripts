# webapp-ops-scripts

Server-side operational scripts for the bigler-webapps infrastructure.

## Scripts

| Script | Purpose |
|---|---|
| `backup.py` | Postgres dumps + Restic backup to local + B2 |
| `verify_backup.py` | Smoke-test: latest snapshot contains fresh gzip-valid dumps |
| `restore.py` | Restore a named app's DB to staging from Restic |
| `generate_paths.py` | Build the restic include-list from Docker volumes + infra paths |
| `janitor.sh` | Monthly Docker prune + disk usage report |

## Deployment

These scripts are pushed to the server by the backup and janitor workflows in
[bigler-webapps/workflow-templates](https://github.com/bigler-webapps/workflow-templates).

The backup workflow bundles `backup/infra_paths.txt` from the calling infra repo
alongside these scripts at `/srv/infrastructure/ops-scripts/` on the server.

## infra_paths.txt

Each infra repo maintains its own `backup/infra_paths.txt` — a list of
server-specific static paths included in every restic backup (e.g. `acme.json`).
The workflow copies this file to `ops-scripts/infra_paths.txt` before rsyncing
to the server, so `backup.py` can find it alongside `generate_paths.py`.
