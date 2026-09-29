"""Shared fixtures.

The test database is built from small CSV fixtures carved out of the real
federal files, so the values under test are the ones the FOPH actually
published. Nothing here touches the network.

The FOPH renamed every code in its files from premium year 2027. Two kinds of
fixture cover that:

* ``*_2027_sample.csv`` are genuine rows from the 2027 files, published on
  2026-09-29, checked against priminfo in ``test_golden_priminfo_2027.py``.
* :func:`write_2027_layout` rewrites the 2026 fixtures in the 2027 layout. It
  predates the real data and is kept because it tests something the real data
  cannot: that the *same* rows give the *same* answers in either layout.

A test module picks its database by overriding the ``source_layout`` fixture.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert

from lamal_api.api.app import create_app
from lamal_api.config import Settings
from lamal_api.db import models
from lamal_api.db.engine import create_db_engine, init_schema
from lamal_api.fetch.normalize import premium_rows, restriction_rows, tariff_rows

FIXTURES = Path(__file__).parent / "fixtures"
PREMIUM_YEAR = 2026

# Communes covering every case the suite needs, taken from the official
# premium-region workbook.
COMMUNES: list[dict[str, Any]] = [
    {
        "bfs_number": 5586,
        "name": "Lausanne",
        "canton": "VD",
        "district": "Lausanne",
        "region": "PR-REG CH1",
    },
    {
        "bfs_number": 261,
        "name": "Zürich",
        "canton": "ZH",
        "district": "Zürich",
        "region": "PR-REG CH1",
    },
    {
        "bfs_number": 401,
        "name": "Aefligen",
        "canton": "BE",
        "district": "Emmental",
        "region": "PR-REG CH2",
    },
    {
        "bfs_number": 301,
        "name": "Aarberg",
        "canton": "BE",
        "district": "Seeland",
        "region": "PR-REG CH2",
    },
    # Postal code 2814 genuinely straddles three premium regions.
    {
        "bfs_number": 6713,
        "name": "Ederswiler",
        "canton": "JU",
        "district": "Delémont",
        "region": "PR-REG CH0",
    },
    {
        "bfs_number": 2619,
        "name": "Kleinlützel",
        "canton": "SO",
        "district": "Thierstein",
        "region": "PR-REG CH0",
    },
    {
        "bfs_number": 2790,
        "name": "Roggenburg",
        "canton": "BL",
        "district": "Laufen",
        "region": "PR-REG CH2",
    },
    # Two communes sharing one postal code *and* one premium region.
    {
        "bfs_number": 5589,
        "name": "Épalinges",
        "canton": "VD",
        "district": "Lausanne",
        "region": "PR-REG CH1",
    },
]

POSTAL_CODES: list[tuple[int, str, int]] = [
    (1003, "Lausanne", 5586),
    (1000, "Lausanne", 5586),
    (8001, "Zürich", 261),
    (3426, "Aefligen", 401),
    (3270, "Aarberg", 301),
    (2814, "Ederswiler", 6713),
    (2814, "Kleinlützel", 2619),
    (2814, "Roggenburg", 2790),
    (1066, "Épalinges", 5589),
    (1066, "Lausanne", 5586),
]

INSURER_NAMES: dict[int, str] = {
    8: "CSS",
    32: "Aquilana",
    194: "Sumiswalder",
    290: "CONCORDIA",
    312: "Atupri Gesundheitsversicherung AG",
    376: "KPT",
    455: "ÖKK",
    509: "Vivao Sympany",
    923: "SLKK",
    1318: "Wädenswil",
    1384: "SWICA",
    1386: "GALENOS AG",
    1479: "Mutuel Krankenversicherung AG",
    1509: "Sanitas",
    1542: "Assura-Basis SA",
    1555: "Visana",
    1560: "Agrisano",
    1562: "Helsana",
    1568: "sana24",
}


@dataclass(frozen=True)
class SourceFiles:
    premiums: Path
    tariffs: Path
    catchment: Path
    premium_year: int = PREMIUM_YEAR


FILES_2026 = SourceFiles(
    premiums=FIXTURES / "praemien_ch_sample.csv",
    tariffs=FIXTURES / "tarife_sample.csv",
    catchment=FIXTURES / "einzugsgebiete_sample.csv",
)

# Every published row for the scenarios in priminfo_2027.json: Lausanne adults
# (franchise 2500), Zürich children (every subgroup, so the sibling tiers are
# there to leak), young adults and standard-model adults, and Bern region 2
# adults, where two models are restricted to named communes. The communes above
# are in the same premium regions in 2027, so they serve both years.
FILES_2027 = SourceFiles(
    premiums=FIXTURES / "praemien_ch_2027_sample.csv",
    tariffs=FIXTURES / "tarife_2027_sample.csv",
    catchment=FIXTURES / "einzugsgebiete_2027_sample.csv",
    premium_year=2027,
)

# Header rows of the header-only 2027 files the FOPH published on 2026-09-11.
HEADER_2027_PREMIUMS_CH = (
    "Versicherer,Kanton,Hoheitsgebiet,Geschäftsjahr,Erhebungsjahr,Region,Altersklasse,"
    "Unfalleinschluss,Tarif,Tariftyp,Altersuntergruppe,Franchisestufe,Franchise,Prämie,"
    "isBaseP,isBaseF,Tarifbezeichnung"
)
HEADER_2027_TARIFFS = (
    "Versicherer,Geschäftsjahr,Erhebungsjahr,Kategorie,Tarif,Tariftyp,"
    "Name_DE,Name_FR,Name_IT,Name_EN"
)
HEADER_2027_CATCHMENT = (
    "Versicherer,Kanton,Hoheitsgebiet,Geschäftsjahr,Erhebungsjahr,Region,Tarif,Tariftyp,"
    "Eingeschränkt,Gemeinden-BFS"
)

_AGE_CLASS_2027 = {"AKL-KIN": "AKA_01_KIN", "AKL-JUG": "AKA_02_JUG", "AKL-ERW": "AKA_03_ERW"}
_AGE_LETTER_2027 = {"AKL-KIN": "K", "AKL-JUG": "J", "AKL-ERW": "E"}
_ADULT_SUBGROUP_2027 = {"AKL-JUG": "J1", "AKL-ERW": "E1"}
_TARIFF_ALT_2027 = {"K": "AKA_01_KIN", "J": "AKA_02_JUG", "E": "AKA_03_ERW"}
# The FOPH published no mapping from the old tariff types to the new ones, so
# this assignment is arbitrary. It only has to use 2027 codes.
_TARIFF_TYPE_2027 = {"BASE": "BASE", "HAM": "PRAXIS", "HMO": "PRAXIS", "DIV": "TEL_DIG"}


def _region_2027(code: str) -> str:
    return f"PR_REG_{code[-1]}"


def _tariff_type_2027(code: str) -> str:
    return _TARIFF_TYPE_2027[code.removeprefix("TAR-")]


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.readline()
        fh.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        return list(csv.DictReader(fh, delimiter=delimiter))


def _write(path: Path, header: str, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header.split(","), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_2027_layout(dest: Path) -> SourceFiles:
    """Rewrite the 2026 fixtures in the layout the FOPH uses from 2027.

    Column order comes from the real 2027 headers. Values follow the updated
    *Erläuterungen zu den Prämiendaten*. The tariff and catchment files also
    switch from semicolons to commas, which puts the commune list in quotes.
    """
    dest.mkdir(parents=True, exist_ok=True)
    files = SourceFiles(
        premiums=dest / "Prämien_CH.csv",
        tariffs=dest / "Tarife.csv",
        catchment=dest / "Einzugsgebiete.csv",
    )

    premiums = []
    for row in _read(FILES_2026.premiums):
        age = row["Altersklasse"]
        level = int(row["Franchisestufe"].removeprefix("FRAST"))
        amount = int(row["Franchise"].removeprefix("FRA-"))
        premiums.append(
            {
                **row,
                "Hoheitsgebiet": "P_OKPCH",
                "Region": _region_2027(row["Region"]),
                "Altersklasse": _AGE_CLASS_2027[age],
                "Unfalleinschluss": row["Unfalleinschluss"].replace("-", "_"),
                "Tariftyp": _tariff_type_2027(row["Tariftyp"]),
                "Altersuntergruppe": row["Altersuntergruppe"] or _ADULT_SUBGROUP_2027[age],
                "Franchisestufe": f"FRASTU_{level:02d}",
                "Franchise": f"FRA_{level:02d}_{_AGE_LETTER_2027[age]}_{amount:04d}",
            }
        )
    _write(files.premiums, HEADER_2027_PREMIUMS_CH, premiums)

    tariffs = []
    for row in _read(FILES_2026.tariffs):
        is_model = row["Kategorie"] == "MOD"
        tariff_type = row["Tariftyp"]
        tariffs.append(
            {
                **{k: v for k, v in row.items() if k != "Sort.-Nr."},
                "Tariftyp": (
                    _tariff_type_2027(tariff_type) if is_model else _TARIFF_ALT_2027[tariff_type]
                ),
                "Name_EN": row["Name_DE"],
            }
        )
    _write(files.tariffs, HEADER_2027_TARIFFS, tariffs)

    catchment = [
        {
            **{k: v for k, v in row.items() if k != "HMO-ID"},
            "Hoheitsgebiet": "P_OKPCH",
            "Region": _region_2027(row["Region"]),
            "Tariftyp": _tariff_type_2027(row["Tariftyp"]),
        }
        for row in _read(FILES_2026.catchment)
    ]
    _write(files.catchment, HEADER_2027_CATCHMENT, catchment)
    return files


def _seed(engine: Engine, files: SourceFiles) -> None:
    now = datetime.now(UTC)
    year = files.premium_year
    with engine.begin() as conn:
        conn.execute(insert(models.premium), list(premium_rows(files.premiums)))
        conn.execute(insert(models.tariff), list(tariff_rows(files.tariffs)))
        restrictions = list(restriction_rows(files.catchment))
        if restrictions:
            conn.execute(insert(models.tariff_restriction), restrictions)
        conn.execute(
            insert(models.commune),
            [{**c, "premium_year": year} for c in COMMUNES],
        )
        conn.execute(
            insert(models.postal_code_commune),
            [
                {
                    "premium_year": year,
                    "postal_code": plz,
                    "locality": locality,
                    "bfs_number": bfs,
                }
                for plz, locality, bfs in POSTAL_CODES
            ],
        )
        conn.execute(
            insert(models.insurer),
            [
                {"bag_number": n, "name": name, "domicile": None, "updated_at": now}
                for n, name in INSURER_NAMES.items()
            ],
        )
        conn.execute(
            insert(models.data_source),
            [
                {
                    "kind": "premiums_ch",
                    "premium_year": year,
                    "source_url": "https://example.invalid/Praemien_CH.csv",
                    "file_name": files.premiums.name,
                    "content_sha256": "0" * 64,
                    "byte_size": 1234,
                    "row_count": 35,
                    "published_at": None,
                    "fetched_at": now,
                }
            ],
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway SQLite file, with all sync disabled."""
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        sync_cron="",
        sync_on_startup=False,
        rate_limit_per_minute=0,
    )


@pytest.fixture
def source_layout() -> str:
    """What the test database is loaded from.

    ``"2026"``: the real 2026 fixtures. ``"2027"``: the same rows rewritten in
    the 2027 layout. ``"2027-data"``: the real 2027 fixtures. Override it in a
    module, parametrised if needed, to change what that module runs against.
    """
    return "2026"


@pytest.fixture
def files_2027(tmp_path: Path) -> SourceFiles:
    """The 2026 fixtures rewritten in the 2027 layout."""
    return write_2027_layout(tmp_path / "layout-2027")


def _source_files(layout: str, tmp_path: Path) -> SourceFiles:
    if layout == "2026":
        return FILES_2026
    if layout == "2027":
        return write_2027_layout(tmp_path / "layout-2027")
    if layout == "2027-data":
        return FILES_2027
    raise ValueError(f"unknown source_layout {layout!r}")


@pytest.fixture
def engine(settings: Settings, source_layout: str, tmp_path: Path) -> Engine:
    """A populated test database."""
    files = _source_files(source_layout, tmp_path)
    engine = create_db_engine(settings)
    init_schema(engine)
    _seed(engine, files)
    return engine


@pytest.fixture
def empty_engine(settings: Settings) -> Engine:
    """Schema only — used to check behaviour before the first sync."""
    engine = create_db_engine(settings)
    init_schema(engine)
    return engine


@pytest.fixture
def client(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    """A test client wired to the populated database.

    Used as a context manager so Starlette actually runs the lifespan, which is
    what puts the engine on ``app.state``.
    """
    engine.dispose()  # the app opens its own engine against the same file
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def empty_client(empty_engine: Engine, settings: Settings) -> Iterator[TestClient]:
    empty_engine.dispose()
    with TestClient(create_app(settings)) as test_client:
        yield test_client
