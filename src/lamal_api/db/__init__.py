"""Database engine and schema."""

from __future__ import annotations

from .engine import create_db_engine, init_schema, optimize
from .models import (
    YEAR_SCOPED_TABLES,
    commune,
    data_source,
    insurer,
    metadata,
    postal_code_commune,
    premium,
    premium_eu,
    tariff,
    tariff_restriction,
)

__all__ = [
    "YEAR_SCOPED_TABLES",
    "commune",
    "create_db_engine",
    "data_source",
    "init_schema",
    "insurer",
    "metadata",
    "optimize",
    "postal_code_commune",
    "premium",
    "premium_eu",
    "tariff",
    "tariff_restriction",
]
