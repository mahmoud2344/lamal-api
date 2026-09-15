# Contributing to lamal-api

Thanks for helping out. This project serves numbers that people use to make real financial
decisions, so the bar for correctness is high — but the codebase is small and the setup is
quick.

## Getting set up

Python 3.11 or newer.

```bash
git clone https://github.com/mahmoud2344/lamal-api.git
cd lamal-api
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Run the checks:

```bash
pytest                        # no network needed
ruff check src tests
ruff format --check src tests
mypy
```

To work against real data:

```bash
lamal-api sync                # downloads ~23 MB from the federal servers
lamal-api serve
```

Behind a TLS-inspecting corporate proxy, downloads will fail certificate verification.
Export your trust store to a PEM file and set `CA_BUNDLE=/path/to/ca.pem`.

## Project layout

```
src/lamal_api/
  domain/     Pure LAMal rules — age classes, franchises, vocabularies. No I/O.
  fetch/      Source discovery (CKAN, priminfo), readers, normalisation, sync.
              vocabulary.py translates both generations of FOPH codes into one.
  db/         SQLAlchemy Core schema and engine.
  api/        FastAPI app, routers, query layer, response schemas.
tests/
  fixtures/   Small CSVs carved from the real federal files.
```

The layering matters: `domain/` must stay free of HTTP and SQL so the rules can be tested
in isolation, and `api/queries.py` is the only place that reads the database.

## The rules this project exists to get right

Before changing anything in `domain/` or `api/queries.py`, read the
[Correctness section of the README](README.md#correctness-the-rules-that-matter). The short
version:

- Premiums are **looked up, never calculated**.
- The premium **region** decides the price, not the canton. Only the BFS commune number is
  authoritative; a postal code is indicative and may be ambiguous.
- Age class is **birth-year based**, from the age reached *during* the premium year.
- Children default to age subgroup **K1**; `K3`/`K4`/`K5` are sibling discount tiers.
- Franchise scales differ by age class and overlap at 300 and 500.
- Commune-restricted models must be filtered out.
- Never de-duplicate results per insurer.
- `isBaseP` is not a usable "standard model" flag.

## Testing

`pytest` runs entirely offline. The fixtures under `tests/fixtures/` are genuine rows taken
from the published federal files, so assertions are about real premiums.

**`tests/test_golden_priminfo.py` is the important one.** Every expected value in it was
read off the official comparator at priminfo.admin.ch and must match to the centime. If you
change how premiums are selected, sorted or filtered, these tests are what tell you whether
you broke it.

If you add a rule, add a golden case for it:

1. Run the query on <https://www.priminfo.admin.ch> and note the exact figures.
2. Add the rows it depends on to the fixture CSVs (keep them small).
3. Assert the exact prices, with a comment naming the priminfo query they came from.

Tests that need the network must be marked and are excluded from the default run:

```python
@pytest.mark.network
def test_live_catalog(): ...
```

```bash
pytest -m network
```

## When the upstream data changes

The federal files are re-published every year and occasionally corrected mid-year. The
loaders **validate strictly and fail loudly** — an unknown `Tariftyp` or a missing column
aborts the sync with a message naming the file and the value. That is deliberate: loading a
subtly wrong table is worse than not loading one.

Two changes are known and already handled. Every September the live files are published
empty for a few weeks before the new year lands; the sync falls back to the newest archive.
And from premium year 2027 every code is spelled differently; `fetch/vocabulary.py` maps
both spellings to the one the database stores. The golden tests run against the real 2026
fixtures and against the same rows rewritten in the 2027 layout (`write_2027_layout` in
`tests/conftest.py`), so both paths stay covered.

If a sync starts failing after a federal release:

1. `lamal-api info` shows what the CKAN catalogue currently advertises.
2. Compare against the column lists in `src/lamal_api/fetch/normalize.py` and the code
   mappings in `src/lamal_api/fetch/vocabulary.py`. The FOPH documents every code in
   *Erläuterungen zu den Prämiendaten.xlsx*, published with the data.
3. Update the loader, add a fixture covering the new shape, and open a PR.

Please **do not commit data files**. `.gitignore` blocks them; the only exception is the
small fixtures under `tests/fixtures/`.

## Pull requests

- Branch off `main`, keep the change focused.
- `pytest`, `ruff check`, `ruff format --check` and `mypy` must pass; CI runs all four on
  Python 3.11, 3.12 and 3.13.
- Type hints and docstrings on anything public. Explain *why* in comments where the
  federal data is surprising — future readers will not have the context.
- If you change API responses, update the README examples and say so in the PR.

## Reporting a wrong premium

The most valuable bug report this project can get. Please include:

- the full request URL,
- what lamal-api returned,
- what priminfo.admin.ch shows for the equivalent query (commune, birth year, franchise,
  accident cover), and
- the output of `GET /v1/meta`, so we know which data snapshot you were on.

Remember that priminfo's results table subtracts CHF 5.15/month (`Vergütung`); compare
against its **`Prämie`** column.

## Code of conduct

Be decent. Assume good faith. Technical disagreements are settled with data — and for this
project, the data is a query against priminfo.admin.ch.
