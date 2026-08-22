# lamal-api

**A self-hostable REST API for Swiss mandatory health insurance (LAMal/KVG) premiums.**

[![CI](https://github.com/mahmoud2344/lamal-api/actions/workflows/ci.yml/badge.svg)](https://github.com/mahmoud2344/lamal-api/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Switzerland publishes every approved health insurance premium as open data — the same
data behind the federal comparator at [priminfo.admin.ch](https://www.priminfo.admin.ch).
But it ships as CSV and XLSX files. **There is no official API.**

This project is that API. One Docker command on any VPS gives you a REST endpoint for
Swiss health insurance premiums, kept in sync with the official FOPH/BAG open data.

```bash
curl "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false"
```

> Verified against priminfo.admin.ch: for an adult in Lausanne with a CHF 2,500 franchise
> and no accident cover, this returns the same 105 offers, in the same order, **to the
> centime**. The test suite pins that, and [docs/VERIFICATION.md](docs/VERIFICATION.md)
> gives you 13 scenarios with the official query and the API call side by side so you can
> check it yourself.

---

## Quickstart

```bash
git clone https://github.com/mahmoud2344/lamal-api.git
cd lamal-api
docker compose up -d
```

That is the whole setup. The container serves immediately and downloads the official data
in the background (~23 MB, typically one to three minutes — the federal servers occasionally
answer 503 and the fetcher retries with backoff). Watch it land:

```bash
docker compose logs -f
```

Check when it is ready:

```bash
curl -s http://localhost:8000/health
```

```json
{ "status": "ok", "database": "ok", "data_loaded": true, "premium_years": [2026] }
```

Interactive API docs are at <http://localhost:8000/docs>.

> **Windows PowerShell:** use `curl.exe`, not `curl` — the bare name is an alias for
> `Invoke-WebRequest` and will fail with *"A drive with the name 'http' does not exist"*.
> `(Invoke-RestMethod "<url>").results` works too. Git Bash, cmd and WSL are fine as written.

---

## Examples

### Find premiums for a person

An adult born in 1990 living in Lausanne (postal code 1003), CHF 2,500 franchise, without
accident cover (they are employed and covered by their employer):

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false&limit=3"
```

```jsonc
{
  "query": {
    "premium_year": 2026,
    "birth_year": 1990,
    "age_class": "AKL-ERW",
    "age_subgroup": "",
    "franchise_chf": 2500,
    "accident_coverage": false,
    "location": {
      "postal_code": 1003,
      "resolved": {
        "bfs_number": 5586,
        "name": "Lausanne",
        "canton": "VD",
        "district": "Lausanne",
        "region": "PR-REG CH1",
        "region_number": 1
      },
      "ambiguous": false
    }
  },
  "pagination": { "limit": 3, "offset": 0, "total": 105 },
  "results": [
    {
      "insurer": { "bag_number": 1509, "name": "Sanitas" },
      "tariff": {
        "code": "TelMed (Compact One)",
        "type": "TAR-DIV",
        "label": "TelMed (Compact One)",
        "name_de": "TelMed (Compact One)",
        "name_fr": "TelMed (Compact One)",
        "name_it": "TelMed (Compact One)"
      },
      "premium_chf": 412.2,
      "premium_centimes": 41220,
      "franchise_chf": 2500,
      "franchise_level": "FRAST6",
      "canton": "VD",
      "region": "PR-REG CH1",
      "age_class": "AKL-ERW",
      "age_subgroup": "",
      "accident_coverage": false,
      "is_standard_franchise": false,
      "premium_year": 2026
    },
    {
      "insurer": { "bag_number": 8, "name": "CSS" },
      "tariff": { "code": "01_170", "type": "TAR-HAM", "label": "Multimed", "name_fr": "Multimed" },
      "premium_chf": 416.2,
      "premium_centimes": 41620
      // ...
    },
    {
      "insurer": { "bag_number": 1542, "name": "Assura-Basis SA" },
      "tariff": { "code": "RPH", "type": "TAR-HAM", "label": "PharMed", "name_fr": "PharMed" },
      "premium_chf": 419.1,
      "premium_centimes": 41910
      // ...
    }
  ],
  "notes": []
}
```

Narrow it to Telmed and other alternative models, cheapest first:

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false&tariff_type=DIV"
```

### Price a whole family

One address, one insurer per bundle. Repeat `person=birth_year:franchise:accident_coverage`
for each member; leave the franchise empty (`2015::true`) to take the standard one.

```bash
curl -s "http://localhost:8000/v1/households?postal_code=8001\
&person=1985:300:false&person=1988:300:false\
&person=2015:0:true&person=2017:0:true&person=2019:0:true&tariff_type=BASE"
```

```jsonc
{
  "query": { "premium_year": 2026, "child_count": 3, "people": [ /* ... */ ] },
  "results": [
    {
      "insurer": { "bag_number": 194, "name": "Sumiswalder" },
      "tariff": { "code": "BASE", "type": "TAR-BASE", "label": "Grundversicherung" },
      "total_chf": 1489.2,
      "total_centimes": 148920,
      "people": [
        { "index": 1, "birth_year": 1985, "age_class": "AKL-ERW", "age_subgroup": "",   "premium_chf": 567.6 },
        { "index": 2, "birth_year": 1988, "age_class": "AKL-ERW", "age_subgroup": "",   "premium_chf": 567.6 },
        { "index": 3, "birth_year": 2015, "age_class": "AKL-KIN", "age_subgroup": "K1", "child_rank": 1, "premium_chf": 141.6 },
        { "index": 4, "birth_year": 2017, "age_class": "AKL-KIN", "age_subgroup": "K1", "child_rank": 2, "premium_chf": 141.6 },
        { "index": 5, "birth_year": 2019, "age_class": "AKL-KIN", "age_subgroup": "K3", "child_rank": 3, "premium_chf": 70.8 }
      ]
    }
  ]
}
```

Note the third child on `K3` at half the rate — see the sibling-discount rule below.

### Resolve a postal code to a premium region

```bash
curl -s "http://localhost:8000/v1/regions?postal_code=1003"
```

```jsonc
{
  "premium_year": 2026,
  "region_year": 2026,
  "query": {
    "postal_code": 1003,
    "resolved": {
      "bfs_number": 5586, "name": "Lausanne", "canton": "VD",
      "district": "Lausanne", "region": "PR-REG CH1", "region_number": 1
    },
    "candidates": [ /* one entry per (postal code, locality, commune) */ ],
    "ambiguous": false
  }
}
```

### Valid franchises for a person

```bash
curl -s "http://localhost:8000/v1/franchises?birth_year=2015"
```

```jsonc
{
  "premium_year": 2026,
  "birth_year": 2015,
  "age_at_year_end": 11,
  "age_class": "AKL-KIN",
  "age_class_label": "Children (0–18)",
  "age_subgroup_default": "K1",
  "options": [
    { "franchise_chf": 0,   "franchise_level": "FRAST1", "is_standard": true  },
    { "franchise_chf": 100, "franchise_level": "FRAST2", "is_standard": false },
    { "franchise_chf": 200, "franchise_level": "FRAST3", "is_standard": false },
    { "franchise_chf": 300, "franchise_level": "FRAST4", "is_standard": false },
    { "franchise_chf": 400, "franchise_level": "FRAST5", "is_standard": false },
    { "franchise_chf": 500, "franchise_level": "FRAST6", "is_standard": false },
    { "franchise_chf": 600, "franchise_level": "FRAST7", "is_standard": false }
  ]
}
```

### Data provenance

```bash
curl -s "http://localhost:8000/v1/meta"
```

```jsonc
{
  "service": "lamal-api",
  "version": "0.1.0",
  "premium_years": [2026],
  "current_premium_year": 2026,
  "region_years": [2026],
  "last_sync_at": "2026-08-18T23:12:28.967217",
  "premium_row_count": 217472,
  "sources": [
    {
      "kind": "premiums_ch",
      "premium_year": 2026,
      "file_name": "Praemien_CH.csv",
      "source_url": "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL1Byw6RtaWVuX0NILmNzdg%3D%3D",
      "content_sha256": "1c6022adc79cff55e67562840e32754e434edebce419cc7c257cbba77e142ce5",
      "byte_size": 22545492,
      "row_count": 217472,
      "fetched_at": "2026-08-18T23:12:27.835001"
    }
  ],
  "dataset_page": "https://opendata.swiss/en/dataset/health-insurance-premiums"
}
```

### Errors say what to do next

Asking for an adult franchise on behalf of a child:

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2015&franchise=2500&accident_coverage=true"
```

```jsonc
// HTTP 422
{
  "error": "invalid_franchise",
  "message": "Franchise 2500 is not available for children (age class AKL-KIN). Valid options: 0, 100, 200, 300, 400, 500, 600.",
  "details": {
    "franchise": 2500,
    "age_class": "AKL-KIN",
    "valid_franchises": [0, 100, 200, 300, 400, 500, 600]
  }
}
```

A postal code that straddles several premium regions is **not** guessed at:

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=2814&birth_year=1990&accident_coverage=false"
```

```jsonc
// HTTP 409
{
  "error": "ambiguous_postal_code",
  "message": "Postal code 2814 spans 4 communes in 3 different premium regions, which have different premiums. Repeat the request with one of the bfs_number values below.",
  "details": {
    "candidates": [
      { "bfs_number": 6713, "name": "Ederswiler",  "canton": "JU", "region": "PR-REG CH0", "locality": "Roggenburg" },
      { "bfs_number": 2619, "name": "Kleinlützel", "canton": "SO", "region": "PR-REG CH0", "locality": "Roggenburg" }
      // ...
    ]
  }
}
```

---

## Endpoints

All JSON, all versioned under `/v1`. OpenAPI schema at `/openapi.json`, Swagger UI at `/docs`.

| Endpoint | Description |
| --- | --- |
| `GET /v1/premiums` | **Main endpoint.** Approved premiums for a person and a place. |
| `GET /v1/households` | A whole family priced together, per insurer, with sibling discounts applied. |
| `GET /v1/premiums/eu` | Cross-border premiums for people insured in Switzerland but resident in the EU/EFTA/UK. |
| `GET /v1/regions` | Resolve a postal code or commune to its premium region. |
| `GET /v1/insurers` | Insurers offering basic insurance, with the cantons they operate in. |
| `GET /v1/franchises` | Franchise options valid for a given birth year. |
| `GET /v1/meta` | Premium years loaded, last sync, source URLs and file hashes. |
| `GET /health` | Liveness/readiness probe. |

### `GET /v1/premiums` parameters

| Parameter | Required | Description |
| --- | --- | --- |
| `birth_year` | yes | Year of birth. The age class follows from the age reached *during* the premium year. |
| `accident_coverage` | yes | `true`/`false`. Employed ≥ 8 h/week? Your employer covers accidents — pass `false`. |
| `postal_code` | one of | Swiss postal code (NPA/PLZ). |
| `bfs_number` | one of | Official BFS/OFS commune number. Authoritative; use it to resolve an ambiguous postal code. |
| `franchise` | no | Franchise in CHF. Defaults to the standard franchise for the age class (0 for children, 300 otherwise). |
| `year` | no | Premium year. Defaults to the most recent one loaded. |
| `tariff_type` | no | `BASE`, `HAM`, `HMO`, `DIV`. Repeatable. `TAR-` prefixed spellings also accepted. |
| `insurer` | no | FOPH/BAG insurer number. Repeatable. |
| `age_subgroup` | no | Override the age subgroup. Children default to `K1`. |
| `limit`, `offset` | no | Pagination. Default `limit=50`, capped by `MAX_PAGE_SIZE`. |
| `sort` | no | `premium_asc` (default), `premium_desc`, `insurer`. |

---

## Correctness: the rules that matter

Premiums are **looked up, never calculated**. Every figure returned is a value the FOPH
approved and published. The hard part is returning the *right rows*, and these are the
rules that decide it. Each one is covered by tests in
[`tests/test_golden_priminfo.py`](tests/test_golden_priminfo.py), and reproducible by hand
with the scenarios in [docs/VERIFICATION.md](docs/VERIFICATION.md).

**Premium region, not canton.** Prices vary by premium region (`PR-REG CH0`–`CH3`), not by
canton. A postal code is resolved to a commune (BFS number), and the commune to a region.
The official region file is explicit that only the BFS number is authoritative — *"Die PLZ
ist somit nicht entscheidend"* / *"Seul le N° OFS fait foi"*. When a postal code covers
communes in different regions the API returns **409 with the candidates** rather than
guessing.

**Age class is birth-year based.** `AKL-KIN` (0–18), `AKL-JUG` (19–25), `AKL-ERW` (26+),
determined by the age the person *reaches during* the premium year — not their age on the
day of the query. Someone born in 2008 is a child for all of premium year 2026, including
on 31 December.

**Children default to age subgroup `K1`.** `K3`/`K4`/`K5` are sibling discount tiers that
only apply when several children are insured together. They are materially cheaper, so
defaulting to one of them would quote prices a household is not entitled to.

**Sibling discounts work in two different ways, and `/v1/households` implements both.**
This is why a family cannot be priced by looking each member up separately and adding the
results. Insurers publishing `K3` (*"ab 3. Kind"*) are **rank-based**: only the third and
later children get the cheaper rate, the first two keep paying `K1`. Insurers publishing
`K4`/`K5` (*"3 und mehr Kinder"*) are **count-based**: once the family reaches a given size,
*every* child moves to the cheaper band — Assura has three of them. Both were established
against priminfo across two cantons, two tariff types and households of one to five, and are
pinned in [`tests/test_household.py`](tests/test_household.py). Adding single-person lookups
instead overcharges a three-child family by CHF 70.80/month with Sumiswalder alone.

**Franchise scales differ by age class.** Children 0/100/200/300/400/500/600; adults and
young adults 300/500/1000/1500/2000/2500. Both contain 300 and 500, which is why the age
class must be resolved before a franchise can be validated at all.

**Commune-restricted models are filtered out.** A handful of HMO and family-doctor models
are sold only in a named list of communes (`Einzugsgebiete.csv`, `Eingeschränkt = Y`).
Visana's *HMO plus*, for instance, is available in Aefligen (BFS 401) but not in Aarberg
(BFS 301) — same canton, same premium region. priminfo honours this and so does lamal-api;
ignoring it quotes products the person cannot buy.

**Never de-duplicate per insurer.** One insurer can sell several named tariffs of the same
type at different prices. Helsana offers both *BeneFit PLUS Hausarzt R1* and *BeneFit PLUS
Flexmed R1* at CHF 420.20 in Lausanne. Both are returned; they are different products.

**`isBaseP` is a trap — this API does not use it.** In `Prämien_CH.csv` the `isBaseP` flag
is set to `1` only on the *with-accident* half of the standard-model rows. Filtering on it
silently drops every without-accident standard premium. The tariff type carries that fact
instead.

**Empty results are normal.** Not every insurer operates in every canton and region.

**Money never becomes a float.** Premiums are parsed with `Decimal` and stored as integer
centimes. `premium_centimes` is the authoritative value; `premium_chf` is a convenience.

### One known difference from priminfo

priminfo's results table subtracts a **`Vergütung` of CHF 5.15/month** (the redistribution
of the federal environmental incentive levies) to show a net figure. That amount is not
part of the open data. lamal-api returns the **gross approved premium**, which is
priminfo's `Prämie` column — so a comparison should be made against that column, not
against `Total`.

---

## Protecting your instance

**Off by default.** A fresh `docker compose up -d` serves openly, which is the point of the
quickstart. Turn protection on only if you expose the instance publicly.

### API keys

```bash
# 1. mint a key (shown once — store it now)
docker compose exec api lamal-api key create --name "mobile app" --rate-limit 300

# 2. enforce it
#    set AUTH_ENABLED=true in docker-compose.yml, then:
docker compose up -d

# 3. call with the key
curl -s -H "X-API-Key: lam_7Kq2xR9f..." "http://localhost:8000/v1/premiums?..."
#    Authorization: Bearer <key> works too
```

```bash
lamal-api key list             # prefixes, limits, last use, request counts
lamal-api key revoke lam_7Kq2  # effective on the next request, no restart
```

**Be clear about what this buys you.** This API serves *public federal open data* — nothing
behind a key is confidential, and there are no writes and no user accounts. Keys are not a
secrecy boundary. They exist so you can protect your bandwidth, see which client is
generating load, and rate-limit per *client* instead of per IP. That last one matters:
IP-based limits punish everyone behind a shared NAT for one caller's traffic.

Keys are stored as SHA-256 hashes and displayed once at creation, so a leaked database
yields no working key.

`key list` shows request counts and last use. `last_used_at` is written the first time a key
is seen, so it is accurate immediately; the counts are batched and written about once a
minute, and on shutdown. They are usage statistics rather than billing records — a hard kill
can lose up to a minute of counts, which is the price of not doing a database write on every
read request.

**`/health` is always reachable without a key.** The container `HEALTHCHECK` calls it — if it
required a key, the container would report unhealthy and restart forever. Everything else,
including `/docs` and `/openapi.json`, needs one when `AUTH_ENABLED=true`.

### Rate limiting

`RATE_LIMIT_PER_MINUTE` applies per API key when auth is on and per client IP otherwise. A
key created with `--rate-limit` overrides it for that client. Exceeding it returns **429**
with a `Retry-After` header.

The limiter is **in-process**: with several uvicorn workers each holds its own counters, so
the effective limit is roughly `RATE_LIMIT_PER_MINUTE × workers`. For a single-container
deployment that is fine. If you need a hard global limit, or you are running more than one
replica, put the limit in a reverse proxy (Caddy, nginx, Cloudflare) instead — that is the
right layer for it, and this setting can stay at `0`.

## Configuration

Everything is environment variables; see [`.env.example`](.env.example) for the annotated
list.

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/lamal.db` | SQLite by default. PostgreSQL via `postgresql+psycopg://…` (needs the `postgres` extra). |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address. |
| `ROOT_PATH` | *(empty)* | Set when served behind a proxy on a sub-path. |
| `SYNC_CRON` | *(empty)* | Five-field cron (UTC) for the built-in scheduler. Empty disables it — use your own cron. |
| `SYNC_ON_STARTUP` | `false` | Sync once at boot, on a worker thread. |
| `SYNC_YEARS` | *(empty)* | `2026`, `2024,2026` or `2024-2026`. Empty = the year currently published. |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins. |
| `AUTH_ENABLED` | `false` | Require an API key everywhere except `/health`. |
| `API_KEY_HEADER` | `X-API-Key` | Header carrying the key. `Authorization: Bearer` always works too. |
| `RATE_LIMIT_PER_MINUTE` | `0` | Cap per key (auth on) or per IP (auth off). `0` disables it. |
| `MAX_PAGE_SIZE` | `500` | Largest page a client may request. |
| `HTTP_TIMEOUT_SECONDS` | `180` | Download timeout. |
| `USER_AGENT` | `lamal-api/0.1 …` | Please set your own contact URL if you run this publicly. |
| `CA_BUNDLE` | *(empty)* | PEM bundle of trusted CAs. Only needed behind a TLS-inspecting corporate proxy. |
| `LOG_LEVEL` | `INFO` | |

### Keeping data fresh

New premium years are published in **late September** for the following year. The fetcher
reads the premium year out of the downloaded file, so a new year is picked up with no code
or config change.

The sync is **idempotent**: every ingested file is recorded with its SHA-256, and a run
against unchanged upstream files touches nothing (about 3 seconds). Each year is replaced
inside a single transaction, so readers never see a half-loaded table.

With Docker Compose the built-in scheduler is on (`SYNC_CRON=17 3 * * 1`, weekly). To drive
it externally instead, leave `SYNC_CRON` empty and run:

```bash
docker compose exec api lamal-api sync
```

---

## Behind a TLS-inspecting proxy or antivirus

If `docker compose build` fails with `CERTIFICATE_VERIFY_FAILED` while fetching from PyPI, or
the sync fails the same way against the federal servers, something is inspecting your HTTPS
traffic and re-signing it with its own root — a corporate middlebox, or antivirus with an
HTTPS/web shield (Norton, Kaspersky, ESET). Your OS trusts that root; the container does not.

Export the interceptor's root certificate into `./certs/` — [certs/README.md](certs/README.md)
has the one-liner for Windows, macOS and Linux — then:

* **the build** picks it up automatically; and
* **the running service** needs `CA_BUNDLE` uncommented in `docker-compose.yml`, which is
  already wired to the mounted `./certs` directory.

TLS verification stays **on** in both cases; you are supplying the root that is actually
signing the traffic, not disabling the check. The certificate is used only in the build stage
and is not present in the finished image. Certificates in `certs/` are git-ignored.

## Deploying to a VPS

[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) takes a bare Ubuntu server to a public HTTPS endpoint
in about fifteen minutes: Docker, Caddy with automatic certificates, firewall, API keys, and a
table of exactly which settings to change. Short version — nothing in the code needs editing,
because the service has no notion of the domain it is served from.

## Running without Docker

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
lamal-api sync
lamal-api serve
```

CLI:

```bash
lamal-api sync                      # load the currently published premium year
lamal-api sync --years 2024-2026    # backfill from the official yearly archives
lamal-api sync --force              # reload even if the upstream hash is unchanged
lamal-api info                      # what is loaded, and what upstream currently offers
lamal-api serve --port 8000
```

---

## Data sources

All data is Swiss federal open data. **Nothing is committed to this repository** — it is
always fetched from source at sync time.

| What | Source |
| --- | --- |
| Premiums (`Prämien_CH.csv`, `Prämien_EU.csv`), tariffs, catchment areas | [opendata.swiss — Krankenversicherungsprämien](https://opendata.swiss/en/dataset/health-insurance-premiums), published by the **Federal Office of Public Health (FOPH / BAG / OFSP / UFSP)** |
| Premium regions per commune and postal code | [priminfo.admin.ch — Prämienregionen](https://www.priminfo.admin.ch/downloads/praemienregionen.xlsx) |
| Register of approved insurers | [priminfo.admin.ch — Downloads](https://www.priminfo.admin.ch/de/downloads/aktuell) |

Download URLs are discovered at runtime through the **opendata.swiss CKAN API** rather than
hard-coded, so the fetcher survives the annual reshuffle. (A quirk worth knowing: the CKAN
resources for this dataset have empty titles, and the real file name is only recoverable by
base64-decoding the `path` query parameter of the download URL.)

## Disclaimer

**This is an unofficial project.** It is not affiliated with, endorsed by, or supported by
the Swiss Confederation, the Federal Office of Public Health, or any health insurer.

The data is served as published, but bugs are possible and the upstream files change.
**Always verify a premium with the official comparator at
[priminfo.admin.ch](https://www.priminfo.admin.ch) before making an insurance decision.**
Nothing here is insurance advice.

The premium data itself remains subject to the terms of its publisher; the MIT license
covers this software only.

---

## Development

```bash
pip install -e ".[dev]"
pytest                 # 135 tests, no network required
ruff check src tests
ruff format --check src tests
mypy
```

The test suite runs offline against small fixtures carved out of the real federal files, so
the values under test are genuinely the ones the FOPH published. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Scope

**In scope:** mandatory basic insurance (LAMal/KVG) premiums, premium regions, insurers,
franchise validation, EU/EFTA cross-border premiums.

**Not in scope (v1):** supplementary insurance (LCA/VVG), premium subsidies
(*Prämienverbilligung* — cantonal), any web UI, user accounts.

## License

[MIT](LICENSE)
