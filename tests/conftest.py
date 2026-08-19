"""Shared fixtures.

The test database is built from small CSV fixtures carved out of the real
federal files, so the values under test are the ones the FOPH actually
published. Nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import Iterator
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


def _seed(engine: Engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(models.premium),
            list(premium_rows(FIXTURES / "praemien_ch_sample.csv")),
        )
        conn.execute(insert(models.tariff), list(tariff_rows(FIXTURES / "tarife_sample.csv")))
        restrictions = list(restriction_rows(FIXTURES / "einzugsgebiete_sample.csv"))
        if restrictions:
            conn.execute(insert(models.tariff_restriction), restrictions)
        conn.execute(
            insert(models.commune),
            [{**c, "premium_year": PREMIUM_YEAR} for c in COMMUNES],
        )
        conn.execute(
            insert(models.postal_code_commune),
            [
                {
                    "premium_year": PREMIUM_YEAR,
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
                    "premium_year": PREMIUM_YEAR,
                    "source_url": "https://example.invalid/Praemien_CH.csv",
                    "file_name": "praemien_ch_sample.csv",
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
def engine(settings: Settings) -> Engine:
    """A populated test database."""
    engine = create_db_engine(settings)
    init_schema(engine)
    _seed(engine)
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
