<!-- Hostinger VPS, no custom domain, API key + HTTPS. Follow top to bottom. -->

# Hostinger VPS: HTTPS API on the free hostname

A start-to-finish runbook for the simplest useful setup: a Hostinger VPS, **no custom domain**,
HTTPS on the hostname Hostinger gives you, and an API key required on every endpoint.

Roughly twenty minutes, most of it waiting for the first data sync.

> **The one fact that makes this work:** `hstgr.cloud` is on the
> [Public Suffix List](https://publicsuffix.org/list/), submitted by Hostinger. So your
> `srv123456.hstgr.cloud` counts as its own registered domain, gets its own Let's Encrypt
> rate limit, and can hold a real certificate. You do **not** need to buy a domain.

---

## 1. Create the VPS

In hPanel, order the smallest KVM plan — 1 vCPU / 4 GB is far more than this needs. For the
operating system template choose **plain Ubuntu 24.04**, or **Ubuntu with Docker** if offered,
which saves you step 2.

Do *not* pick a template with a control panel (CloudPanel, Plesk, CyberPanel). They install
their own nginx/Apache on ports 80 and 443, which then fights Caddy for them. If you already
have one, either use its own reverse-proxy UI or start from a clean Ubuntu image.

Note two things from the VPS overview page:

- the **IP address**
- the **hostname**, which looks like `srv123456.hstgr.cloud`

Everywhere below, replace `srv123456.hstgr.cloud` with yours.

## 2. Log in and install Docker

```bash
ssh root@YOUR_VPS_IP
```

```bash
apt update && apt -y upgrade
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

## 3. Check the hostname resolves — before anything else

This is the step people skip and then spend an hour on. Let's Encrypt proves you own the name
by connecting to it over port 80. If the name does not resolve to *this* server, the
certificate request fails and Caddy retries in a way that looks like it is broken.

```bash
hostname -f                              # what the box thinks it is called
dig +short srv123456.hstgr.cloud         # what the world thinks it points at
curl -s https://ifconfig.me; echo        # this server's public IP
```

The last two must match. If `dig` returns nothing, set the hostname in hPanel
(VPS → your server → the hostname setting) and wait a few minutes for DNS.

## 4. Get the code onto the server

```bash
git clone https://github.com/mahmoud2344/lamal-api.git
cd lamal-api
```

Nothing is fetched from anywhere else at this point — the premium data is downloaded later, by
the service itself, straight from the federal servers.

## 5. Configure it

Edit `docker-compose.yml`. Four changes:

```yaml
    ports:
      - "127.0.0.1:8000:8000"        # was "8000:8000"

    environment:
      AUTH_ENABLED: "true"           # was "false"
      RATE_LIMIT_PER_MINUTE: "120"   # was "0"
      CORS_ORIGINS: "*"              # tighten in step 9, once the landing page exists
      USER_AGENT: "lamal-api/0.1 (+https://github.com/mahmoud2344/lamal-api)"
```

**Why the port binding matters.** Left as `"8000:8000"`, Docker opens port 8000 to the whole
internet and people can reach `http://YOUR_IP:8000` directly — bypassing your HTTPS, your API
key, and your rate limit, because none of those live in Docker. Binding to `127.0.0.1` means
only the reverse proxy on the same machine can reach it.

You can ignore `CA_BUNDLE` and the `certs/` directory entirely — those exist for machines
behind a TLS-inspecting antivirus or corporate proxy, which a VPS is not.

## 6. Start it

```bash
docker compose up -d
docker compose logs -f          # Ctrl-C once you see the sync finish
```

First boot downloads ~23 MB of federal data and loads about 217 000 rows. One to three minutes.

```bash
curl -s localhost:8000/health
```

Wait for `"data_loaded":true`. It is normal for this to say `false` for the first minute.

## 7. Put Caddy in front for HTTPS

Caddy gets and renews the certificate itself. There is no certbot timer to forget about.

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Replace `/etc/caddy/Caddyfile` entirely with this — **your hostname on the first line**:

```caddyfile
srv123456.hstgr.cloud {
    reverse_proxy 127.0.0.1:8000
    encode gzip

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        -Server
    }
}
```

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl restart caddy
journalctl -u caddy -n 30 --no-pager        # look for "certificate obtained successfully"
```

The certificate arrives within about thirty seconds. If it does not, go back to step 3 — it is
almost always DNS.

## 8. Turn on the firewall and mint a key

```bash
ufw allow OpenSSH
ufw allow 80,443/tcp
ufw --force enable
ufw status
```

Port 8000 stays closed. That is deliberate: it is only reachable from Caddy over loopback.

> Hostinger also has its own firewall in hPanel, applied *before* traffic reaches the server.
> If it is enabled, make sure 22, 80 and 443 are allowed there too, or you will lock yourself
> out and the certificate will never be issued.

```bash
cd ~/lamal-api
docker compose exec api lamal-api key create --name "landing page" --rate-limit 120
```

**Copy the key now — it is shown once and stored only as a hash.**

## 9. Verify

```bash
curl -s https://srv123456.hstgr.cloud/health

curl -s -H "X-API-Key: lam_YOURKEY" \
  "https://srv123456.hstgr.cloud/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false" \
  | head -c 300
```

Then confirm the protection is real:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://srv123456.hstgr.cloud/v1/premiums   # 401
curl -s -o /dev/null -w '%{http_code}\n' http://YOUR_VPS_IP:8000/health              # should fail/timeout
```

`401` and a failed direct connection mean the key and the port binding are both doing their job.

Your API base URL, for the landing page, is:

```
https://srv123456.hstgr.cloud
```

Interactive docs are at `/docs` — behind the key, so open it after adding the header, or
temporarily set `AUTH_ENABLED: "false"` while exploring.

---

## Before the landing page goes live: read this

**An API key in a browser is not secret.** If your landing page calls this API from JavaScript,
the key ships in the page source and anyone can read it in devtools. That is not a flaw in the
key system — it is what happens to any credential you put in a browser.

For this project it is a manageable trade-off, because the data is public federal open data and
there is nothing to steal. What someone *can* do is spend your rate limit. So:

1. **Lock down CORS** once you know the landing page's origin. Change
   `CORS_ORIGINS: "*"` to `CORS_ORIGINS: "https://your-landing-page.ch"` and
   `docker compose up -d`. This stops other *websites* using your key from a browser. It does
   not stop `curl` — CORS is a browser rule, not a server-side gate.
2. **Give the public key a modest limit** — 60–120 a minute is plenty for a landing page.
3. **Rotate if abused.** `lamal-api key revoke lam_xxxx`, mint a new one, redeploy the page.
   Revocation takes effect on the next request with no restart.

If you later want the key genuinely hidden, the landing page needs a small server-side proxy
(a Netlify/Vercel function, or an Astro SSR route) that holds the key and forwards the call.
Worth doing when the site has a backend anyway; overkill before then.

---

## Running it day to day

**Update to a new version:**

```bash
cd ~/lamal-api && git pull && docker compose up -d --build
```

The `--build` is not optional. Without it Docker reuses the existing image and you run the old
code while reading the new source.

**Data refreshes itself** every Monday at 03:17 UTC via `SYNC_CRON`. New premium years appear
in late September and are picked up automatically — the year is read out of the downloaded file,
not configured. Force one with:

```bash
docker compose exec api lamal-api sync
docker compose exec api lamal-api info          # what is loaded
docker compose exec api lamal-api key list      # usage per key
```

**Logs:** `docker compose logs -f --tail=100` for the API, `journalctl -u caddy -f` for TLS.

**Backups: none needed.** Every byte is re-fetchable from the Confederation. If the server dies,
a fresh `docker compose up -d` on a new one rebuilds everything.

## When something is wrong

| Symptom | Cause |
| --- | --- |
| Caddy never gets a certificate | Hostname does not resolve to this server (step 3), or port 80 is blocked by ufw or the hPanel firewall |
| `curl https://…` refused, `localhost:8000` fine | Caddy is not running, or the Caddyfile has the wrong hostname |
| Everything returns `401` | Working as intended — send `X-API-Key` |
| `/health` returns `"data_loaded":false` | First sync still running, or it failed — `docker compose logs` |
| New code not taking effect | You forgot `--build` |
| Port 8000 reachable from outside | The `ports:` line is still `"8000:8000"` |
