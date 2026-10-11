# Public deployment

The Colors client and wire protocol are unchanged. No token/header is required.
Use one server instance and one local SQLite volume. Do not place the live SQLite
files on NFS/SMB or run multiple replicas sharing a network filesystem.

Build/start on a Linux VPS with Docker Compose:

```sh
docker compose up -d --build
docker compose logs --tail=100 colors
curl http://127.0.0.1:8080/health
```

The container runs as uid 10001 with a read-only root, dropped capabilities,
limited memory/CPU/processes and a persistent named volume. SQLite and tiles
are under /data. Existing deployments must preserve their database and unchanged
world origin. This new named volume does not automatically import existing data.
Never use `docker compose down -v` when preserving a world.

The host publishes port 8080 only on loopback. Install an HTTPS reverse proxy;
nginx.conf.example provides a starting configuration for the http context.
Replace the hostname and certificate paths, provision valid TLS certificates,
and validate with `nginx -t`. Open only HTTPS and your SSH administrative access
in the firewall. Keep Python's port private. Configure Colors with the HTTPS
origin and retain both existing command paths. No POST caching or automatic POST
retry. The rate limit (10/s, burst 60) and 16 connections per IP are deliberately
permissive for normal polling/shared-IP players; tune using real traffic.
The proxy supplies rate protection; starting the Python server directly on a
public interface bypasses it. TLS termination is performed by the host proxy.

The tile cache defaults to 256 MiB and 4096 PNG entries plus associated metadata,
pruned oldest first. Individual downloads are capped at 1 MiB; dimensions are
checked before image decoding. Map fetches have a 30s budget (one active network
operation can add its socket timeout); cache lock acquisition waits at most 5s.
Use a tile provider appropriate for public traffic and a contact-bearing agent.
Updates to the provider URL remain administrative configuration only.

SIGTERM gracefully stops accepting connections and joins bounded request threads.
Compose allows 120s before forced stop. SQL commands use BEGIN IMMEDIATE with
15s busy timeout; a failed BEGIN no longer attempts a spurious ROLLBACK. Overload
can produce HTTP 503/429; it cannot be made invisible to an unchanged client.

Online backup inside the container:

```sh
docker compose exec colors python /app/backup_database.py /data/colors.sqlite3 /data/backup.sqlite3
docker compose cp colors:/data/backup.sqlite3 ./colors-backup.sqlite3
```

Store backups off the VPS, rotate them and test restoration. The helper uses
SQLite's online backup API, integrity-checks the result and atomically replaces
the destination. Do not back up a live WAL database by copying only its main file.
Monitor free disk space, request errors and volume growth; the database itself
is not quota-limited because deleting player/territory data would break gameplay.
Keep the OS, Docker and dependencies updated. This is resource hardening, not
strong player authentication, anti-cheat or a claim of DDoS immunity.

Validation: protocol/gameplay regressions and hardening unit tests. Full Docker,
nginx, VPS deployment and physical game validation must be done on the target.
