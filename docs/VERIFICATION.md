<!-- Central verification sheet: every scenario has the official query and the API query side by side. -->

# Verification scenarios

Eleven scenarios that check `lamal-api` against the federal comparator at
priminfo.admin.ch. Each one gives you **the exact priminfo query** and **the exact API call**,
so you can run both and compare without re-deriving anything.

All figures below are **premium year 2026**, captured **19 August 2026**. Every scenario in
this sheet was run against both systems and matched.

> **Once premium year 2027 is loaded**, the API defaults to it while these figures stay 2026.
> Add `&year=2026` to each API call (and pick 2026 on priminfo, if it still offers it) to
> reproduce them. The FOPH also reclassified tariff types for 2027, so a `tariff_type` of
> `HAM`, `HMO` or `DIV` only matches 2026 data; `BASE` works in both years.

Assumes the API is at `http://localhost:8000` (`docker compose up -d`).

> **On Windows PowerShell, use `curl.exe`, not `curl`.** Bare `curl` is an alias for
> `Invoke-WebRequest`, which reads `-s` as `-Uri` and then fails with
> *"A drive with the name 'http' does not exist"*. Either of these works:
>
> ```powershell
> curl.exe -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false"
> (Invoke-RestMethod "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false").results
> ```
>
> Git Bash, cmd and WSL take the `curl` commands below unchanged.

---

## Read this before comparing

Three things differ cosmetically between the two systems. None of them is a defect, and all
three will bite you if you don't know them.

**1. priminfo subtracts CHF 5.15/month.** Its results table has `Prämie`, `Vergütung` and
`Total` columns. `Vergütung` is the redistribution of the federal environmental incentive
levies — it is *not* part of the open data. **Compare against priminfo's `Prämie` column**,
never `Total`.

**2. priminfo pads its table with unpriced rows.** If an insurer doesn't sell the franchise
you asked for, priminfo still lists it with an em-dash `—` instead of a price. `lamal-api`
omits those rows entirely, because they aren't products you can buy. So priminfo's row count
can exceed the API's `total`. **Count only priced rows.** Scenario 4a is the worked example:
priminfo shows 27 rows, 6 of them empty, and the API returns 21.

**3. Insurer names are shortened on priminfo.** It shows `Assura`, `Swica`, `Galenos`,
`KKLH`; the API returns the official register names `Assura-Basis SA`, `SWICA`, `GALENOS AG`,
`Luzerner Hinterland`. Match on the BAG number or the tariff name, not the insurer string.

---

## Scenario index

| # | Scenario | What it proves |
|---|---|---|
| [1](#1--baseline-adult) | Lausanne adult, franchise 2500, no accident | Baseline: region resolution, sorting, all models |
| [2](#2--standard-model-only) | Same person, standard model only | `tariff_type` filter |
| [3](#3--child-default-subgroup) | Zürich child, franchise 0, with accident | Child age class, default subgroup `K1` |
| [4](#4--age-class-boundary-child--young-adult) | Born 2008 vs 2007 | Child→young-adult boundary |
| [5](#5--age-class-boundary-young-adult--adult) | Born 2001 vs 2000 | Young-adult→adult boundary |
| [6](#6--accident-cover-on-and-off) | Same person, accident on/off | `accident_coverage` |
| [7](#7--premium-region-inside-one-canton) | Bern vs Thun vs Interlaken | Region, not canton, sets the price |
| [8](#8--commune-restricted-model) | Aefligen vs Aarberg | Catchment-area filtering |
| [9](#9--ambiguous-postal-code) | Postal code 2814 | 409 instead of a wrong guess |
| [10](#10--eu--efta-cross-border) | Resident in France | EU premium table |
| [11](#11--household-sibling-discounts) | Families of 1–5 children | Sibling discounts, both mechanisms |

---

## 1 — Baseline adult

Adult born 1990 in Lausanne, franchise CHF 2,500, no accident cover, all models.

**priminfo** — [open this query](https://www.priminfo.admin.ch/de/praemien?location_id=1003776233811&yob%5B0%5D=1990&franchise%5B0%5D=2500&coverage%5B0%5D=0&models%5B%5D=BASE&models%5B%5D=HAM&models%5B%5D=HMO&models%5B%5D=DIV&display=savings)
or fill the form with: Wohnort `1003 Lausanne` · Jahrgang `1990` · Franchise `2500` ·
Unfalldeckung `Nein` · Modelle: all four ticked.

**API**

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=2500&accident_coverage=false"
```

**Expected** — header reads `Gemeinde Lausanne, Region 1, Kanton Waadt`; API resolves
`bfs_number 5586`, `PR-REG CH1`. **105 offers**, cheapest first:

| CHF | Insurer | Tariff |
|---:|---|---|
| 412.20 | Sanitas | TelMed (Compact One) |
| 416.20 | CSS | Multimed |
| 419.10 | Assura | PharMed |
| 419.70 | Atupri | HMO |
| 419.90 | Vivao Sympany | FlexHelp 24 |

Helsana appears twice at **420.20** — *BeneFit PLUS Hausarzt R1* and *BeneFit PLUS Flexmed R1*.
Two different products at the same price; neither system de-duplicates them.

---

## 2 — Standard model only

Same person, franchise CHF 300, standard model (`TAR-BASE`) only.

**priminfo** — [open this query](https://www.priminfo.admin.ch/de/praemien?location_id=1003776233811&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings)
· tick **only** `Standard` under Modelle.

**API**

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=300&accident_coverage=false&tariff_type=BASE"
```

**Expected** — **21 offers**: Galenos `605.40`, ÖKK `613.80`, Atupri `616.80`, all
*Grundversicherung*.

---

## 3 — Child, default subgroup

Child born 2015 in Zürich, franchise CHF 0, **with** accident cover.

**priminfo** — [open this query](https://www.priminfo.admin.ch/de/praemien?location_id=80011450512497&yob%5B0%5D=2015&franchise%5B0%5D=0&coverage%5B0%5D=1&models%5B%5D=BASE&models%5B%5D=HAM&models%5B%5D=HMO&models%5B%5D=DIV&display=savings)
· Wohnort `8001 Zürich` · Jahrgang `2015` · Franchise `0` · Unfalldeckung `Ja`.

**API**

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2015&franchise=0&accident_coverage=true"
```

**Expected** — **128 offers**: Assura *Qualimed* `120.30`, CSS
*Gesundheitspraxisversicherung* `122.00`, ÖKK *Gesundheitszentrum* `122.40`.

The API echoes `"age_class": "AKL-KIN"` and `"age_subgroup": "K1"`. This is the rule most
worth watching: `K3`/`K4`/`K5` are sibling discount tiers and are far cheaper — the cheapest
`K3` row here is `57.90`. If you ever see a price near that for a single child, the subgroup
filter has broken. To see the tier deliberately:

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2015&franchise=0&accident_coverage=true&age_subgroup=K3"
```

---

## 4 — Age class boundary: child → young adult

Same place, same franchise (300 is valid for both age classes, which is what isolates the
effect), no accident cover, standard model only. Only the birth year changes.

| | Born **2008** (turns 18) | Born **2007** (turns 19) |
|---|---|---|
| priminfo | [open](https://www.priminfo.admin.ch/de/praemien?location_id=80011450512497&yob%5B0%5D=2008&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) | [open](https://www.priminfo.admin.ch/de/praemien?location_id=80011450512497&yob%5B0%5D=2007&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) |
| Age class | `AKL-KIN` | `AKL-JUG` |
| Cheapest | Wädenswil **113.20** | Sanitas **375.00** |
| Then | Sumiswalder 115.50, CSS 116.90 | Avenir 414.90, Assura 415.90 |
| API `total` | **21** | **27** |
| priminfo rows | 27 (**6 unpriced**) | 27 |

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2008&franchise=300&accident_coverage=false&tariff_type=BASE"
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2007&franchise=300&accident_coverage=false&tariff_type=BASE"
```

**This is the worked example of gotcha 2.** Six insurers — Atupri, KPT, ÖKK, Vivao Sympany,
SWICA, Helsana — don't sell a *child* franchise-300 product at all (they offer 0/200/400/600
only). priminfo lists them with `—`; the API omits them. 21 priced rows on both sides.

A one-year change in birth year moves the cheapest offer from 113.20 to 375.00. Nothing else
in the query changed.

---

## 5 — Age class boundary: young adult → adult

Same setup, crossing the 25/26 line.

| | Born **2001** (turns 25) | Born **2000** (turns 26) |
|---|---|---|
| priminfo | [open](https://www.priminfo.admin.ch/de/praemien?location_id=80011450512497&yob%5B0%5D=2001&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) | [open](https://www.priminfo.admin.ch/de/praemien?location_id=80011450512497&yob%5B0%5D=2000&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) |
| Age class | `AKL-JUG` | `AKL-ERW` |
| Cheapest | Sanitas **375.00** | Sumiswalder **567.60** |
| Then | Avenir 414.90, Assura 415.90 | SLKK 568.90, Luzerner Hinterland 573.95 |
| Total | 27 | 27 |

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2001&franchise=300&accident_coverage=false&tariff_type=BASE"
curl -s "http://localhost:8000/v1/premiums?postal_code=8001&birth_year=2000&franchise=300&accident_coverage=false&tariff_type=BASE"
```

Born 2001 and born 2007 return **identical** results — both are young adults for premium
year 2026. That's the band working.

> priminfo prints `KKLH` where the API returns `Luzerner Hinterland` (BAG number 360).

---

## 6 — Accident cover on and off

Adult born 1990 in Lausanne, franchise CHF 300, standard model. Only `Unfalldeckung` changes.

| | `accident_coverage=false` (Nein) | `accident_coverage=true` (Ja) |
|---|---|---|
| priminfo | [open](https://www.priminfo.admin.ch/de/praemien?location_id=1003776233811&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) | [open](https://www.priminfo.admin.ch/de/praemien?location_id=1003776233811&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=1&models%5B%5D=BASE&display=savings) |
| Cheapest | Galenos **605.40** | Galenos **647.80** |
| Then | ÖKK 613.80, Atupri 616.80 | Atupri 649.30, ÖKK 660.00 |
| Total | 21 | 21 |

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=300&accident_coverage=false&tariff_type=BASE"
curl -s "http://localhost:8000/v1/premiums?postal_code=1003&birth_year=1990&franchise=300&accident_coverage=true&tariff_type=BASE"
```

Note the ranking flips between ÖKK and Atupri — accident cover isn't a flat surcharge.
`accident_coverage` is a **required** parameter precisely because a default here would
silently return the wrong product. Anyone employed 8 h/week or more is covered by their
employer and wants `false`.

---

## 7 — Premium region inside one canton

Three Bernese communes, one per premium region. Same person: born 1990, franchise CHF 300,
no accident, standard model. Same canton, same product, three prices.

| | Bern (R1) | Thun (R2) | Interlaken (R3) |
|---|---|---|---|
| priminfo | [open](https://www.priminfo.admin.ch/de/praemien?location_id=30042147406347&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) | [open](https://www.priminfo.admin.ch/de/praemien?location_id=36002333324808&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) | [open](https://www.priminfo.admin.ch/de/praemien?location_id=380091729432&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&display=savings) |
| Region | `PR-REG CH1` | `PR-REG CH2` | `PR-REG CH3` |
| **Visana** *Grundversicherung* | **621.60** | **549.10** | **507.00** |
| Cheapest | Vivao Sympany 586.90 | Sumiswalder 526.50 | Sumiswalder 486.50 |
| Total | 26 | 26 | 26 |

```bash
curl -s "http://localhost:8000/v1/premiums?postal_code=3004&birth_year=1990&franchise=300&accident_coverage=false&tariff_type=BASE&insurer=1555"
curl -s "http://localhost:8000/v1/premiums?bfs_number=942&birth_year=1990&franchise=300&accident_coverage=false&tariff_type=BASE&insurer=1555"
curl -s "http://localhost:8000/v1/premiums?bfs_number=581&birth_year=1990&franchise=300&accident_coverage=false&tariff_type=BASE&insurer=1555"
```

The clearest demonstration that **the premium region sets the price, not the canton**: one
insurer, one product, CHF 114.60/month apart within Bern.

> Postal codes 3600 and 3800 each cover several communes. They all sit in the same region, so
> the API answers normally and flags `"ambiguous": true` with a note. `bfs_number` is used
> above to name the commune exactly — 942 Thun, 581 Interlaken.

---

## 8 — Commune-restricted model

Visana's *HMO plus* is sold only in a named list of 60 Bernese communes. Aefligen is on that
list; Aarberg is not. **Same canton, same premium region (`PR-REG CH2`), same person.**

| | Aefligen (BFS 401) — inside | Aarberg (BFS 301) — outside |
|---|---|---|
| priminfo | [open](https://www.priminfo.admin.ch/de/praemien?location_id=34262514047463&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&models%5B%5D=HAM&models%5B%5D=HMO&models%5B%5D=DIV&display=savings) | [open](https://www.priminfo.admin.ch/de/praemien?location_id=32503847370148&yob%5B0%5D=1990&franchise%5B0%5D=300&coverage%5B0%5D=0&models%5B%5D=BASE&models%5B%5D=HAM&models%5B%5D=HMO&models%5B%5D=DIV&display=savings) |
| Total offers | **128** | **127** |
| Visana *HMO plus* | **469.00** | **not offered** |
| Visana tariffs | 8 | 7 |

```bash
curl -s "http://localhost:8000/v1/premiums?bfs_number=401&birth_year=1990&franchise=300&accident_coverage=false&insurer=1555"
curl -s "http://localhost:8000/v1/premiums?bfs_number=301&birth_year=1990&franchise=300&accident_coverage=false&insurer=1555"
```

The two result sets differ by **exactly one tariff**. Everything else — *Combi Care* 472.30,
*Tel Doc* 483.20, *Managed Care* 487.60, *Tel Care* 492.90, *Med Direct* 493.10, *Med Call*
543.60, *Grundversicherung* 549.10 — is identical.

This is the rule an implementation reading only `Prämien_CH.csv` gets wrong: that file has an
*HMO plus* row for all of BE region 2. The restriction lives in `Einzugsgebiete.csv`.

> On priminfo, Aarberg is reached through postal code `3250 Lyss` — pick
> *"3250 Lyss (Gemeinde Aarberg)"* from the suggestions.

---

## 9 — Ambiguous postal code

Postal code **2814 Roggenburg** covers four communes across **three** premium regions.

**priminfo** — type `2814` and the autocomplete offers four separate entries:
*Gemeinde Ederswiler*, *Gemeinde Kleinlützel*, *Gemeinde Roggenburg*, *Gemeinde Pleigne*.
It makes you choose before it will quote anything.

**API**

```bash
curl -s -i "http://localhost:8000/v1/premiums?postal_code=2814&birth_year=1990&accident_coverage=false"
```

**Expected — HTTP 409**, not a guess:

```json
{
  "error": "ambiguous_postal_code",
  "message": "Postal code 2814 spans 4 communes in 3 different premium regions, which have different premiums. Repeat the request with one of the bfs_number values below.",
  "details": { "candidates": [
    { "bfs_number": 6713, "name": "Ederswiler",  "canton": "JU", "region": "PR-REG CH0" },
    { "bfs_number": 2619, "name": "Kleinlützel", "canton": "SO", "region": "PR-REG CH0" },
    { "bfs_number": 2790, "name": "Roggenburg",  "canton": "BL", "region": "PR-REG CH2" },
    { "bfs_number": 6719, "name": "Pleigne",     "canton": "JU", "region": "PR-REG CH0" }
  ] }
}
```

Resolve it exactly as priminfo does — by naming the commune:

```bash
curl -s "http://localhost:8000/v1/premiums?bfs_number=6713&birth_year=1990&accident_coverage=false"
```

To list the candidates without an error, use the regions endpoint, which never fails on
ambiguity:

```bash
curl -s "http://localhost:8000/v1/regions?postal_code=2814"
```

---

## 10 — EU / EFTA cross-border

Insured in Switzerland, resident in France. Born 1990, no accident cover.

```bash
curl -s "http://localhost:8000/v1/premiums/eu?country=FR&birth_year=1990&accident_coverage=false"
```

**Expected** — **13 offers**, franchise defaults to CHF 300: Helsana `200.00`, SWICA `246.30`,
Mutuel `348.80`. Only the standard model exists abroad; there is no regional split.

> The public priminfo calculator covers residents of Switzerland only, so this scenario has
> **no equivalent comparator query**. It comes from `Prämien_EU.csv`; to check it by hand, use
> [gesamtbericht_eu.xlsx](https://www.priminfo.admin.ch/downloads/gesamtbericht_eu.xlsx) from
> the priminfo downloads page.

---

## 11 — Household sibling discounts

The one rule that cannot be checked one person at a time. Add people on priminfo with
**"Weitere Person hinzufügen"**; on the API repeat the `person` parameter.

Household: one adult born 1985 (franchise 300, no accident) plus *N* children born
2013/2015/2017/2019/2021 (franchise 0, with accident), Zürich `8001`, standard model only.

**priminfo** — [1 adult + 3 children](https://www.priminfo.admin.ch/de/praemien?location_id=80011450512497&yob%5B0%5D=1985&franchise%5B0%5D=300&coverage%5B0%5D=0&yob%5B1%5D=2013&franchise%5B1%5D=0&coverage%5B1%5D=1&yob%5B2%5D=2015&franchise%5B2%5D=0&coverage%5B2%5D=1&yob%5B3%5D=2017&franchise%5B3%5D=0&coverage%5B3%5D=1&models%5B%5D=BASE&display=savings)
· with several people it renders **one table per insurer**, listing every person and a
combined *"Alle Personen"* row.

**API**

```bash
curl -s "http://localhost:8000/v1/households?postal_code=8001&tariff_type=BASE\
&person=1985:300:false&person=2013:0:true&person=2015:0:true&person=2017:0:true"
```

**Expected — two different mechanisms, per insurer:**

| children | Sumiswalder (194) · `K1`+`K3` | SWICA (1384) · `K1`+`K3` | Assura (1542) · `K1`+`K4`+`K5` |
|---|---|---|---|
| 1 | 141.60 | 156.80 | 144.90 |
| 2 | 141.60, 141.60 | 156.80, 156.80 | **142.90 ×2** |
| 3 | 141.60, 141.60, **70.80** | 156.80, 156.80, **65.40** | **140.90 ×3** |
| 5 | 141.60, 141.60, 70.80, 70.80, 70.80 | 156.80, 156.80, 65.40 ×3 | 140.90 ×5 |

* **Rank-based (`K3`, labelled *"ab 3. Kind"*)** — only the third and later children get the
  cheaper rate; the first two keep paying `K1`.
* **Count-based (`K4`/`K5`, labelled *"3 und mehr Kinder"*)** — the household size selects one
  band that *every* child then pays. Assura has three bands and already discounts at two
  children.

Household total is a plain sum. Two adults born 1985 and 1988 plus three children with
Sumiswalder gives **CHF 1'489.20** = 567.60 + 567.60 + 141.60 + 141.60 + 70.80, matching
priminfo's *"Alle Personen"* row:

```bash
curl -s "http://localhost:8000/v1/households?postal_code=8001&tariff_type=BASE&insurer=194\
&person=1985:300:false&person=1988:300:false\
&person=2015:0:true&person=2017:0:true&person=2019:0:true"
```

Check `age_subgroup` on each member of the breakdown — that is the tier actually applied.

> **Why this endpoint exists.** Pricing each person through `/v1/premiums` and adding the
> results returns `K1` for every child, overcharging this family by CHF 70.80/month with
> Sumiswalder and CHF 12.00 with Assura. The same three-band behaviour is confirmed in
> Lausanne (Assura 162.00 / 160.00 / 158.00) and on the family-doctor model, so it is not a
> quirk of one canton or one tariff type.

---

## Run them all

```bash
curl -s "http://localhost:8000/v1/meta" | head -20   # confirm which data snapshot you are on
```

Every scenario above is also encoded as an automated test:

```bash
pytest tests/test_golden_priminfo.py -v
```

Those run offline against fixtures holding the same published rows, so they keep passing in
CI without depending on admin.ch.

## When a number disagrees

1. Check `GET /v1/meta` — `premium_years` and `last_sync_at` tell you which snapshot you have.
   The FOPH corrects files mid-year; `lamal-api sync` picks corrections up.
2. Re-check the three gotchas at the top: `Prämie` not `Total`, priced rows only, short names.
3. If it still disagrees, that's a bug worth reporting — see
   [CONTRIBUTING.md](../CONTRIBUTING.md#reporting-a-wrong-premium) for what to include.
