"""Reference endpoints: insurers, franchises, metadata and health."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from ... import __version__
from ...domain.codes import AgeClass
from ...domain.rules import age_class_for, default_age_subgroup, standard_franchise
from .. import queries, schemas
from ..deps import (
    ConnectionDep,
    SettingsDep,  # noqa: F401  (kept for symmetry with other routers)
    get_engine,
)

router = APIRouter(tags=["reference"])

_AGE_CLASS_LABELS = {
    AgeClass.CHILD: "Children (0–18)",
    AgeClass.YOUNG_ADULT: "Young adults (19–25)",
    AgeClass.ADULT: "Adults (26+)",
}

ATTRIBUTION = (
    "Premium data: Federal Office of Public Health (FOPH/BAG/OFSP), published as open data "
    "via opendata.swiss. Premium regions and the insurer register: priminfo.admin.ch."
)
DISCLAIMER = (
    "Unofficial service, not affiliated with or endorsed by the Swiss Confederation. "
    "Always verify a premium with the official comparator at priminfo.admin.ch before "
    "acting on it."
)


@router.get(
    "/v1/insurers",
    response_model=schemas.InsurerListResponse,
    summary="List insurers offering basic insurance",
)
def get_insurers(
    conn: ConnectionDep,
    year: Annotated[int | None, Query(description="Premium year. Defaults to the latest.")] = None,
) -> schemas.InsurerListResponse:
    """Insurers with approved premiums in the given year.

    Derived from the premium data, so an insurer holding an authorisation but
    selling no basic-insurance premiums does not appear. Names come from the
    FOPH register of approved insurers and are null if that register could not
    be fetched during the last sync.
    """
    premium_year = queries.require_premium_year(conn, year)
    insurers = queries.list_insurers(conn, premium_year)
    return schemas.InsurerListResponse(
        premium_year=premium_year, count=len(insurers), insurers=insurers
    )


@router.get(
    "/v1/franchises",
    response_model=schemas.FranchiseResponse,
    summary="Valid franchise options for a person",
)
def get_franchises(
    conn: ConnectionDep,
    birth_year: Annotated[int, Query(ge=1890, le=2100, examples=[1990])],
    year: Annotated[int | None, Query(description="Premium year. Defaults to the latest.")] = None,
) -> schemas.FranchiseResponse:
    """The franchises this person may choose, and which one is the default.

    Franchise options depend on the age class: children have a 0–600 CHF scale,
    adults and young adults a 300–2500 CHF one. Both scales contain 300 and 500,
    which is why the age class has to be resolved before a franchise can be
    validated at all.
    """
    premium_year = queries.require_premium_year(conn, year)
    age_class = age_class_for(birth_year, premium_year)
    levels = queries.franchise_levels(conn, premium_year, age_class)
    default = standard_franchise(age_class)

    return schemas.FranchiseResponse(
        premium_year=premium_year,
        birth_year=birth_year,
        age_at_year_end=premium_year - birth_year,
        age_class=age_class.value,
        age_class_label=_AGE_CLASS_LABELS[age_class],
        age_subgroup_default=default_age_subgroup(age_class),
        options=[
            schemas.FranchiseOption(
                franchise_chf=amount,
                franchise_level=levels[amount] or None,
                is_standard=amount == default,
            )
            for amount in sorted(levels)
        ],
    )


@router.get("/v1/meta", response_model=schemas.MetaResponse, summary="Data version and provenance")
def get_meta(conn: ConnectionDep) -> schemas.MetaResponse:
    """What data this instance holds, and exactly where it came from."""
    from ...fetch.ckan import DATASET_PAGE

    premium_years = queries.available_premium_years(conn)
    last_sync = queries.last_sync_at(conn)
    return schemas.MetaResponse(
        service="lamal-api",
        version=__version__,
        premium_years=premium_years,
        current_premium_year=premium_years[-1] if premium_years else None,
        region_years=queries.available_region_years(conn),
        last_sync_at=last_sync if isinstance(last_sync, datetime) else None,
        premium_row_count=queries.premium_row_count(conn),
        sources=queries.data_sources(conn),
        attribution=ATTRIBUTION,
        disclaimer=DISCLAIMER,
        dataset_page=DATASET_PAGE,
    )


@router.get(
    "/health",
    response_model=schemas.HealthResponse,
    summary="Liveness and readiness probe",
    tags=["ops"],
)
def get_health(conn: ConnectionDep) -> schemas.HealthResponse:
    """Report database reachability and whether any premium data is loaded.

    Returns 200 even with an empty database so a container that has not yet run
    its first sync is not killed by an orchestrator; inspect ``data_loaded``.
    """
    database = "ok"
    years: list[int] = []
    try:
        conn.execute(text("SELECT 1"))
        years = queries.available_premium_years(conn)
    except Exception:
        database = "error"
    return schemas.HealthResponse(
        status="ok" if database == "ok" else "degraded",
        database=database,
        data_loaded=bool(years),
        premium_years=years,
    )


__all__ = ["get_engine", "router"]
