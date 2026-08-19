"""Household pricing: several people, one address, one insurer per bundle."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...domain.codes import AgeClass
from ...domain.errors import LamalError
from ...domain.rules import (
    accident_code,
    age_class_for,
    standard_franchise,
    validate_franchise,
)
from .. import queries, schemas
from ..deps import ConnectionDep, SettingsDep
from .premiums import InvalidParameterError, _normalise_tariff_types

router = APIRouter(prefix="/v1", tags=["premiums"])

MAX_PEOPLE = 20

_TRUE = {"true", "1", "yes", "y", "ja", "oui", "si"}
_FALSE = {"false", "0", "no", "n", "nein", "non"}


def _parse_person(raw: str, index: int, premium_year: int) -> tuple[int, int | None, bool]:
    """Parse one ``birth_year:franchise:accident`` triple."""
    parts = raw.split(":")
    if len(parts) != 3:
        raise InvalidParameterError(
            f"person #{index} is malformed: {raw!r}. Use "
            f"'birth_year:franchise:accident_coverage', for example '1985:300:false'. "
            f"Leave the franchise empty to take the standard one, e.g. '2015::true'.",
            person=raw,
        )
    year_raw, franchise_raw, accident_raw = (part.strip() for part in parts)

    if not year_raw.isdigit():
        raise InvalidParameterError(
            f"person #{index}: birth year {year_raw!r} is not a number.", person=raw
        )
    birth_year = int(year_raw)
    if not 1890 <= birth_year <= 2100:
        raise InvalidParameterError(
            f"person #{index}: birth year {birth_year} is out of range (1890–2100).", person=raw
        )

    franchise: int | None = None
    if franchise_raw:
        if not franchise_raw.isdigit():
            raise InvalidParameterError(
                f"person #{index}: franchise {franchise_raw!r} is not a number.", person=raw
            )
        franchise = int(franchise_raw)

    key = accident_raw.casefold()
    if key in _TRUE:
        accident = True
    elif key in _FALSE:
        accident = False
    else:
        raise InvalidParameterError(
            f"person #{index}: accident cover {accident_raw!r} must be true or false. "
            f"Anyone employed 8 hours a week or more is covered by their employer and "
            f"wants false.",
            person=raw,
        )
    return birth_year, franchise, accident


@router.get(
    "/households",
    response_model=schemas.HouseholdResponse,
    summary="Price a household of several people together",
    responses={
        404: {"model": schemas.ErrorResponse},
        409: {"model": schemas.ErrorResponse},
        422: {"model": schemas.ErrorResponse},
        503: {"model": schemas.ErrorResponse},
    },
)
def get_households(
    conn: ConnectionDep,
    settings: SettingsDep,
    person: Annotated[
        list[str],
        Query(
            description="One entry per person, as 'birth_year:franchise:accident_coverage' — "
            "for example person=1985:300:false. Repeat the parameter for each member. "
            "Leave the franchise empty ('2015::true') to use the standard one for that "
            "age class.",
            examples=["1985:300:false"],
        ),
    ],
    postal_code: Annotated[int | None, Query(ge=1000, le=9999, examples=[8001])] = None,
    bfs_number: Annotated[int | None, Query(ge=1)] = None,
    year: Annotated[int | None, Query()] = None,
    tariff_type: Annotated[list[str] | None, Query()] = None,
    insurer: Annotated[list[int] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> schemas.HouseholdResponse:
    """Return each insurer and tariff that can cover the whole household.

    Everyone shares one address and one insurer per bundle, which is how the
    federal comparator presents a family. Results are sorted by household total,
    cheapest first, and each carries a per-person breakdown.

    **Children are not priced independently.** Most insurers discount larger
    families, and the discount works in one of two ways depending on the
    insurer: some cut the price only from the third child onwards, others move
    every child to a cheaper band once the family reaches a certain size.
    Looking each child up on their own and adding the results overcharges the
    household, so the subgroup is resolved here against the whole family. See
    :mod:`lamal_api.domain.household`.
    """
    if (postal_code is None) == (bfs_number is None):
        raise InvalidParameterError(
            "Provide exactly one of postal_code or bfs_number. The BFS commune number is "
            "authoritative; a postal code is resolved to it and can be ambiguous."
        )
    if not person:
        raise InvalidParameterError("Provide at least one person, e.g. person=1985:300:false.")
    if len(person) > MAX_PEOPLE:
        raise InvalidParameterError(
            f"A household is limited to {MAX_PEOPLE} people; {len(person)} were given.",
            people=len(person),
        )

    premium_year = queries.require_premium_year(conn, year)
    region_year = queries.resolve_region_year(conn, premium_year)
    location = queries.resolve_location(
        conn, region_year=region_year, postal_code=postal_code, bfs_number=bfs_number
    )

    people: list[queries.HouseholdPerson] = []
    echo: list[schemas.HouseholdPersonEcho] = []
    child_rank = 0

    for position, raw in enumerate(person, start=1):
        birth_year, franchise, accident = _parse_person(raw, position, premium_year)
        age_class = age_class_for(birth_year, premium_year)
        effective = (
            standard_franchise(age_class)
            if franchise is None
            else validate_franchise(franchise, age_class)
        )
        rank: int | None = None
        if age_class is AgeClass.CHILD:
            child_rank += 1
            rank = child_rank

        people.append(
            queries.HouseholdPerson(
                index=position,
                birth_year=birth_year,
                age_class=age_class,
                franchise_chf=effective,
                accident=accident_code(accident_coverage=accident).value,
                child_rank=rank,
            )
        )
        echo.append(
            schemas.HouseholdPersonEcho(
                index=position,
                birth_year=birth_year,
                age_class=age_class.value,
                child_rank=rank,
                franchise_chf=effective,
                accident_coverage=accident,
            )
        )

    criteria = queries.HouseholdCriteria(
        premium_year=premium_year,
        canton=location.canton,
        region=location.region,
        bfs_numbers=location.bfs_numbers,
        people=tuple(people),
        tariff_types=_normalise_tariff_types(tariff_type),
        insurers=tuple(insurer) if insurer else None,
        limit=min(limit, settings.max_page_size),
        offset=offset,
    )
    results, total, incomplete = queries.search_households(conn, criteria)

    notes: list[str] = []
    if child_rank >= 2:
        notes.append(
            f"{child_rank} children in the household: sibling discounts were applied per "
            f"insurer. Some discount only from the third child, others move every child to "
            f"a cheaper band — check age_subgroup in each breakdown."
        )
    if incomplete:
        notes.append(
            f"{incomplete} insurer/tariff combinations were skipped because they do not "
            f"cover every member of this household — usually a franchise that insurer does "
            f"not sell for one person's age class."
        )
    if location.payload.ambiguous:
        notes.append(
            f"Postal code {postal_code} covers {len(location.bfs_numbers)} communes sharing "
            f"premium region {location.region}; pass bfs_number for an exact answer."
        )
    if total == 0:
        notes.append(
            "No insurer covers this household as specified. Not every insurer operates in "
            "every region or sells every franchise."
        )

    return schemas.HouseholdResponse(
        query=schemas.HouseholdQueryEcho(
            premium_year=premium_year,
            people=echo,
            child_count=child_rank,
            tariff_types=list(criteria.tariff_types) if criteria.tariff_types else None,
            insurers=list(criteria.insurers) if criteria.insurers else None,
            location=location.payload,
        ),
        pagination=schemas.Pagination(limit=criteria.limit, offset=offset, total=total),
        results=results,
        notes=notes,
    )


__all__ = ["LamalError", "router"]
