# cdr.pdhc — multi-instance deploy

Five demonstrator CDRs share this codebase. Each instance is a distinct
compose project with its own port pair, container namespace, and pgdata
volume. Per platform-plan execution §3.

## Port plan

| Instance | App port | DB port | Compose project    | Subdomain          |
|----------|----------|---------|--------------------|--------------------|
| CDR1     | 9046     | 9045    | `cdr_pdhc_1`       | `cdr1.pdhc.se`     |
| CDR2     | 9146     | 9145    | `cdr_pdhc_2`       | `cdr2.pdhc.se`     |
| CDR3     | 9246     | 9245    | `cdr_pdhc_3`       | `cdr3.pdhc.se`     |
| CDR4     | 9346     | 9345    | `cdr_pdhc_4`       | `cdr4.pdhc.se`     |
| CDR5     | 9446     | 9445    | `cdr_pdhc_5`       | `cdr5.pdhc.se`     |

Each block separated by 100 so a Rule-16 kill-own-ports script can
never reach a sibling (Rule 22).

## Stamping a new instance

```bash
deploy/stamp.sh 3
# wrote deploy/instances/cdr3/.env
# next steps: fill secrets, SSO operator, nginx operator, ship tarball
```

## Operator-owned steps (per platform-plan §3.2 / §3.3)

These are not Claude actions — every one needs the operator on the
macmini console or in a CI pipeline.

1. **SSO registration.** In `sso.pdhc/.env`, add:
   - `SSO_CLIENT_ID_CDR{N}=...`
   - `SSO_CLIENT_SECRET_CDR{N}=...`
   - `ALLOWED_CALLBACK_URLS` += `https://cdr{N}.pdhc.se/auth/callback`
   Restart sso.pdhc gracefully so the new client pair becomes valid.
2. **Reverse-proxy server block.** Add an nginx server block for
   `cdr{N}.pdhc.se` proxying to `127.0.0.1:{APP_PORT}`. Same TLS chain
   as the rest of `pdhc.se`. After reload, verify with
   `curl -Iv https://cdr{N}.pdhc.se/healthz`.
3. **Tarball deploy.** Build a release tarball locally (excluding
   `venv/`, `.env`, `logs/`), `scp` to miserver, extract under
   `/usr/local/www/cdr{N}.pdhc/releases/<ISO-UTC-timestamp>/`, drop
   the stamped `.env` next to `docker-compose.yml`, repoint the
   `current` symlink, then `docker compose -f .../current/cdr_app/docker-compose.yml up -d`.
4. **First DB password.** Compose creates the DB on first init using
   the `.env` password. **Do not change it later** — Postgres only
   reads `POSTGRES_PASSWORD` on first init; subsequent compose-up runs
   ignore changes (the silent killer documented in `CLAUDE.md` §9).
   If the password ever needs rotation, `ALTER USER ... WITH PASSWORD`
   via the trust-rule side door, then update `.env`.

## Local-dev backwards compatibility

The original local-dev cdr.pdhc DB binds to port 9047 (not 9045).
Defaults in `docker-compose.yml` honour this when env is empty so
existing local workflows keep working. Stamped instances 1..5 always
go to the new 9046/9045 pair (and 9146/9145, etc).

## Per-instance shared dir

When the deploys land on miserver, each instance gets its own
`shared/` directory next to `current` per CLAUDE.md §7:
```
/usr/local/www/cdr{N}.pdhc/
├── current -> releases/<ts>
├── releases/...
└── shared/
    ├── gunicorn.pid
    └── logs/
```

## Backups

`server_backup_all.sh` should pg_dump all five CDR DBs after Phase 3.5
lands. See `cdr.pdhc/deploy/server_backup_all.diff` for the suggested
diff (operator applies on miserver).
