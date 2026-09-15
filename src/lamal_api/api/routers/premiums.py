"""The main endpoint: look up approved premiums for a person and a place."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from ...db import models
from ...domain.codes import TariffType
from ...domain.errors import LamalError
from ...domain.rules import (
    accident_code,
    age_class_for,
    default_age_subgroup,
    standard_franchise,
    validate_franchise,
)
from .. import queries, schemas
from ..deps import ConnectionDep, SettingsDep

router = APIRouter(prefix="/v1", tags=["premiums"])

#: Every tariff type by its bare name, in declaration order: BASE, the types used
#: up to premium year 2026, then the types used from 2027.
_TARIFF_TYPE_NAMES = {t.value: t.value.removeprefix("TAR-") for t in TariffType}
_TARIFF_TYPE_INPUT = {name.casefold(): value for value, name in _TARIFF_TYPE_NAMES.items()}

TARIFF_TYPE_HELP = (
    "Filter by model. Up to premium year 2026: BASE, HAM, HMO, DIV. From 2027: BASE, "
    "PRAXIS, FLEX, TEL_DIG, PHARM. Repeatable; case-insensitive; TAR- prefix optional."
)


class InvalidParameterError(LamalError):
    code = "invalid_parameter"
    http_status = 422


def _normalise_tariff_types(values: list[str] | None) -> tuple[str, ...] | None:
    if not values:
        return None
    out: list[str] = []
    for raw in values:
        key = raw.strip().casefold().replace("_", "-").removeprefix("tar-").replace("-", "_")
        if key not in _TARIFF_TYPE_INPUT:
            raise InvalidParameterError(
                f"Unknown tariff_type {raw!r}. Up to premium year 2026: BASE (standard), "
                f"HAM (family doctor), HMO, DIV (Telmed and other models). From 2027: BASE, "
                f"PRAXIS, FLEX, TEL_DIG, PHARM. The TAR- prefixed forms are accepted too.",
                tariff_type=raw,
                valid=list(_TARIFF_TYPE_NAMES.values()),
            )
        out.append(_TARIFF_TYPE_INPUT[key])
    return tuple(dict.fromkeys(out))


def tariff_type_note(
    conn: Connection, premium_year: int, requested: tuple[str, ...] | None
) -> str | None:
    """Explain a tariff-type filter that cannot match anything in this year.

    Without this, asking 2027 for ``HAM`` would return an empty list that looks
    exactly like "no insurer offers that here".
    """
    if not requested:
        return None
    used = queries.tariff_types_in_year(conn, premium_year)
    unused = [t for t in requested if t not in used]
    if not unused or not used:
        return None
    in_year = [name for value, name in _TARIFF_TYPE_NAMES.items() if value in used]
    return (
        f"Premium year {premium_year} has no {'/'.join(_TARIFF_TYPE_NAMES[t] for t in unused)} "
        f"models; that year classifies them as {', '.join(in_year)}. The FOPH changed the "
        f"classification for premium year 2027."
    )


@router.get(
    "/premiums",
    response_model=schemas.PremiumSearchResponse,
    summary="Look up approved LAMal premiums",
    responses={
        404: {"model": schemas.ErrorResponse, "description": "Unknown postal code or commune"},
        409: {
            "model": schemas.ErrorResponse,
            "description": "Postal code spans several premium regions; pass bfs_number",
        },
        422: {"model": schemas.ErrorResponse, "description": "Invalid parameter combination"},
        503: {"model": schemas.ErrorResponse, "description": "No data loaded yet"},
    },
)
def get_premiums(
    conn: ConnectionDep,
    settings: SettingsDep,
    birth_year: Annotated[
        int,
        Query(
            ge=1890,
            le=2100,
            description="Year of birth. The age class follows from the age the person "
            "reaches during the premium year, so only the year matters.",
            examples=[1990],
        ),
    ],
    accident_coverage: Annotated[
        bool,
        Query(
            description="Whether accident cover is included. People employed 8 hours a week "
            "or more are already covered by their employer and should pass false.",
        ),
    ],
    postal_code: Annotated[
        int | None,
        Query(ge=1000, le=9999, description="Swiss postal code (NPA/PLZ).", examples=[1003]),
    ] = None,
    bfs_number: Annotated[
        int | None,
        Query(
            ge=1,
            description="Official BFS/OFS commune number. Authoritative — use it when a "
            "postal code is ambiguous.",
        ),
    ] = None,
    franchise: Annotated[
        int | None,
        Query(
            description="Franchise in CHF. Adults and young adults: 300, 500, 1000, 1500, "
            "2000, 2500. Children: 0, 100, 200, 300, 400, 500, 600. Defaults to the "
            "standard franchise for the age class.",
            examples=[2500],
        ),
    ] = None,
    year: Annotated[
        int | None, Query(description="Premium year. Defaults to the most recent one loaded.")
    ] = None,
    tariff_type: Annotated[
        list[str] | None,
        Query(description=TARIFF_TYPE_HELP),
    ] = None,
    insurer: Annotated[
        list[int] | None, Query(description="Filter by FOPH/BAG insurer number. Repeatable.")
    ] = None,
    age_subgroup: Annotated[
        str | None,
        Query(
            description="Override the age subgroup. Children default to K1; K3/K4/K5 are "
            "sibling discount tiers that only apply when several children are insured "
            "together. Adults have no subgroup.",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, description="Page size.")] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[str, Query(pattern="^(premium_asc|premium_desc|insurer)$")] = "premium_asc",
) -> schemas.PremiumSearchResponse:
    """Return every approved premium matching the person and place.

    Premiums are **looked up, never calculated**: the response contains the
    exact values the FOPH approved. One insurer can appear several times with
    different named tariffs of the same type, and that is not duplication —
    they are genuinely different products at different prices.
    """
    if (postal_code is None) == (bfs_number is None):
        raise InvalidParameterError(
            "Provide exactly one of postal_code or bfs_number. The BFS commune number is "
            "authoritative; a postal code is resolved to it and can be ambiguous."
        )

    premium_year = queries.require_premium_year(conn, year)
    region_year = queries.resolve_region_year(conn, premium_year)
    location = queries.resolve_location(
        conn, region_year=region_year, postal_code=postal_code, bfs_number=bfs_number
    )

    age_class = age_class_for(birth_year, premium_year)
    effective_franchise = (
        standard_franchise(age_class)
        if franchise is None
        else validate_franchise(franchise, age_class)
    )
    subgroup = age_subgroup if age_subgroup is not None else default_age_subgroup(age_class)

    criteria = queries.PremiumCriteria(
        premium_year=premium_year,
        canton=location.canton,
        region=location.region,
        age_class=age_class,
        age_subgroup=subgroup,
        franchise_chf=effective_franchise,
        accident=accident_code(accident_coverage=accident_coverage).value,
        bfs_numbers=location.bfs_numbers,
        tariff_types=_normalise_tariff_types(tariff_type),
        insurers=tuple(insurer) if insurer else None,
        limit=min(limit, settings.max_page_size),
        offset=offset,
        sort=sort,
    )

    total = queries.count_premiums(conn, criteria)
    results = queries.search_premiums(conn, criteria)

    notes: list[str] = []
    if location.payload.ambiguous:
        notes.append(
            f"Postal code {postal_code} covers {len(location.bfs_numbers)} communes that share "
            f"premium region {location.region}, so the premiums are identical. Models "
            f"restricted to particular communes were kept if available in any of them; "
            f"pass bfs_number for an exact answer."
        )
    if unused_types := tariff_type_note(conn, premium_year, criteria.tariff_types):
        notes.append(unused_types)
    if total == 0:
        notes.append(
            "No premiums matched. Not every insurer operates in every canton and region, "
            "so an empty result is normal for a narrow filter."
        )

    return schemas.PremiumSearchResponse(
        query=schemas.QueryEcho(
            premium_year=premium_year,
            birth_year=birth_year,
            age_class=age_class.value,
            age_subgroup=subgroup,
            franchise_chf=effective_franchise,
            accident_coverage=accident_coverage,
            tariff_types=list(criteria.tariff_types) if criteria.tariff_types else None,
            insurers=list(criteria.insurers) if criteria.insurers else None,
            location=location.payload,
        ),
        pagination=schemas.Pagination(limit=criteria.limit, offset=offset, total=total),
        results=results,
        notes=notes,
    )


@router.get(
    "/premiums/eu",
    summary="Look up premiums for insured persons resident in the EU/EFTA/UK",
    tags=["premiums"],
)
def get_eu_premiums(
    conn: ConnectionDep,
    settings: SettingsDep,
    birth_year: Annotated[int, Query(ge=1890, le=2100, examples=[1990])],
    accident_coverage: Annotated[bool, Query()],
    country: Annotated[
        str,
        Query(
            description="Country of residence, e.g. 'FR', 'DE', 'IT'. The 'EU ' prefix used "
            "in the source data is optional.",
            examples=["FR"],
        ),
    ],
    year: Annotated[int | None, Query()] = None,
    franchise: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Cross-border premiums for people insured in Switzerland but living abroad.

    These are a separate, much smaller table: they are keyed by country rather
    than canton and region, have no regional subdivision and offer only the
    standard model.
    """
    premium_year = queries.require_premium_year(conn, year)
    normalised = country.strip().upper().removeprefix("EU ").strip()
    code = f"EU {normalised}"

    age_class = age_class_for(birth_year, premium_year)
    effective_franchise = (
        standard_franchise(age_class)
        if franchise is None
        else validate_franchise(franchise, age_class)
    )
    subgroup = default_age_subgroup(age_class)
    accident = accident_code(accident_coverage=accident_coverage).value

    e = models.premium_eu.c
    i = models.insurer.c
    conditions = [
        e.premium_year == premium_year,
        e.country == code,
        e.age_class == age_class.value,
        e.age_subgroup == subgroup,
        e.franchise_chf == effective_franchise,
        e.accident == accident,
    ]

    total = int(
        conn.execute(
            select(func.count()).select_from(models.premium_eu).where(*conditions)
        ).scalar()
        or 0
    )
    if total == 0:
        available = conn.execute(
            select(e.country).where(e.premium_year == premium_year).distinct().order_by(e.country)
        ).scalars()
        countries = [c.removeprefix("EU ") for c in available]
        if normalised not in countries:
            raise InvalidParameterError(
                f"No EU/EFTA premiums for country {normalised!r} in {premium_year}. "
                f"Available: {', '.join(countries)}.",
                country=normalised,
                available=countries,
            )

    rows = conn.execute(
        select(
            e.insurer_bag_number,
            i.name.label("insurer_name"),
            e.country,
            e.premium_centimes,
            e.franchise_chf,
            e.age_class,
            e.age_subgroup,
            e.accident,
            e.tariff_label,
            e.tariff_type,
        )
        .select_from(
            models.premium_eu.outerjoin(models.insurer, i.bag_number == e.insurer_bag_number)
        )
        .where(*conditions)
        .order_by(e.premium_centimes, e.insurer_bag_number)
        .limit(min(limit, settings.max_page_size))
        .offset(offset)
    ).all()

    return {
        "query": {
            "premium_year": premium_year,
            "country": normalised,
            "birth_year": birth_year,
            "age_class": age_class.value,
            "franchise_chf": effective_franchise,
            "accident_coverage": accident_coverage,
        },
        "pagination": {
            "limit": min(limit, settings.max_page_size),
            "offset": offset,
            "total": total,
        },
        "results": [
            {
                "insurer": {
                    "bag_number": r._mapping["insurer_bag_number"],
                    "name": r._mapping["insurer_name"],
                },
                "country": r._mapping["country"].removeprefix("EU "),
                "tariff": {
                    "label": r._mapping["tariff_label"],
                    "type": r._mapping["tariff_type"],
                },
                "premium_chf": r._mapping["premium_centimes"] / 100,
                "premium_centimes": r._mapping["premium_centimes"],
                "franchise_chf": r._mapping["franchise_chf"],
                "accident_coverage": r._mapping["accident"] == "MIT-UNF",
            }
            for r in rows
        ],
    }
