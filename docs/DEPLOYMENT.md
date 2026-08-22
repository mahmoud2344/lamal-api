<!-- Deploying lamal-api to a VPS: the short path, and exactly what to change. -->

# Deploying to a VPS

From a bare Ubuntu server to `https://api.example.ch/v1/premiums` in about fifteen minutes.

Works on anything with 1 vCPU and 1 GB RAM — Hetzner CX22, DigitalOcean, Infomaniak, Exoscale.
The database is SQLite and the dataset is ~220 000 rows, so this needs far less machine than
people expect. Storage: about 250 MB for the image plus 100 MB for the data.

---

## The five-minute version

```bash
# on the server, as a sudo user
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/mahmoud2344/lamal-api.git
cd lamal-api
docker compose up -d
```

That is a working API on port 8000. Everything below is about making it a *public* one:
a domain, HTTPS, and not letting the internet hammer it.

---

## What you actually have to change

Nothing in the code. All of it is environment variables in `docker-compose.yml`.

| Setting | Default | Change it to | Why |
|---|---|---|---|
| `CORS_ORIGINS` | `*` | `https://yourapp.ch` | `*` lets **any** website call your API from a browser. Fine while testing, careless in public. |
| `USER_AGENT` | `lamal-api/0.1 (+github…)` | your own contact URL | The federal servers see this. If your instance ever misbehaves, this is how they reach you. |
| `AUTH_ENABLED` | `false` | `true` if not fully public | Then mint keys — see below. |
| `RATE_LIMIT_PER_MINUTE` | `0` | `60`–`300` | `0` means unlimited. On a public box, set something. |
| `SYNC_CRON` | `17 3 * * 1` | keep, or stagger it | Weekly is plenty; new premium years land in late September. Pick an odd minute so you are not hitting admin.ch on the hour with everyone else. |
| `SYNC_ON_STARTUP` | `true` | keep | Makes a fresh server populate itself. |
| `PORT` | `8000` | keep | Bind it to localhost and let the proxy face the internet — see below. |

There is **no API URL to configure inside the project**. The service does not know or care what
domain it is behind. The only place a URL matters is *your client*, which points at
`https://api.example.ch`. The one exception is `ROOT_PATH`, needed only if you serve it on a
sub-path like `example.ch/lamal` rather than its own subdomain.

---

## Making it public properly

### 1. Point DNS at the server

An `A` record for `api.example.ch` → your server's IPv4. If you have IPv6, add `AAAA` too.
Wait for it to resolve before the next step, or the certificate request will fail:

```bash
dig +short api.example.ch
```

### 2. Stop exposing the port directly

In `docker-compose.yml`, bind the published port to loopback so only the proxy can reach it:

```yaml
    ports:
      - "127.0.0.1:8000:8000"   # was "8000:8000"
```

Without this, people can bypass your proxy — and your HTTPS, and your rate limits — by hitting
`http://your-ip:8000` directly.

### 3. Put Caddy in front

Caddy is the least-effort option: it obtains and renews Let's Encrypt certificates on its own,
with no certbot cron to forget about. Create `/etc/caddy/Caddyfile`:

```caddyfile
api.example.ch {
    reverse_proxy 127.0.0.1:8000

    # Caddy handles TLS automatically. This is a read-only public API,
    # so a generous but finite cap is sensible at the edge.
    encode gzip

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        -Server
    }
}
```

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
sudo systemctl reload caddy
```

Certificates appear within seconds. `https://api.example.ch/health` should answer.

### 4. Close the firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

Port 8000 is deliberately not opened — it is only reachable from the proxy on loopback.

### 5. Turn on keys, if you want them

```bash
docker compose exec api lamal-api key create --name "my frontend" --rate-limit 300
```

Copy the key — it is shown once. Then set `AUTH_ENABLED: "true"` in `docker-compose.yml` and
`docker compose up -d`. Callers now send `X-API-Key: lam_…` or `Authorization: Bearer lam_…`.

`/health` stays public on purpose so uptime monitors and container healthchecks keep working.

> Remember what keys are for here. The data is public federal open data, so this is not
> secrecy — it is so you can throttle and identify clients. See the README.

---

## Verify

```bash
curl -s https://api.example.ch/health
curl -s "https://api.example.ch/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false" | head -c 400
curl -s https://api.example.ch/v1/meta | grep -o '"premium_row_count":[0-9]*'
```

`premium_row_count` around **217 000** means the sync worked. If it is `0`, the first sync is
still running or failed — `docker compose logs -f` will say which.

---

## Keeping it running

**Updating to a new version:**

```bash
cd lamal-api && git pull && docker compose up -d --build
```

The `--build` matters. Without it Docker reuses the existing image and you will be running the
old code while reading the new source, which is a genuinely confusing hour to lose.

**Data refresh** happens on the `SYNC_CRON` schedule, in-process. Nothing to set up. New premium
years are detected automatically — the year is read out of the downloaded file, not configured.
To force one:

```bash
docker compose exec api lamal-api sync
docker compose exec api lamal-api info     # what is loaded right now
```

**Backups.** Strictly speaking you need none: every byte can be re-fetched from the
Confederation with one `sync`. The fastest disaster recovery is a fresh `docker compose up -d`
on a new box. If you still want the volume:

```bash
docker run --rm -v lamal-api_lamal-data:/data -v $PWD:/backup alpine \
  tar czf /backup/lamal-data.tgz -C /data .
```

**Logs:** `docker compose logs -f --tail=100`.

---

## If your host inspects TLS

Corporate networks and some antivirus products re-sign HTTPS with their own root, which makes
both the image build and the federal downloads fail with `CERTIFICATE_VERIFY_FAILED`. Rare on a
VPS, common on an office machine. Drop the root into `certs/` and uncomment `CA_BUNDLE` in
`docker-compose.yml` — see [certs/README.md](../certs/README.md).

## Scaling, honestly

You almost certainly do not need to. This is a read-only SQLite lookup with an index built for
the exact query shape; a single container on the smallest VPS handles far more than a comparison
site will send it.

If you do outgrow it, in order of usefulness:

1. **Cache at the proxy.** Premiums change once a year. A `Cache-Control` header and Caddy's
   cache do more than any application change.
2. **Add uvicorn workers** (`--workers 4`). Note the built-in rate limiter is per process, so
   the effective cap becomes `RATE_LIMIT_PER_MINUTE × workers` — move the limit to Caddy at that
   point.
3. **Switch to PostgreSQL** via `DATABASE_URL` and the `postgres` extra, if you want several
   API containers sharing one database.

Reach for these when a measurement says to, not before.
