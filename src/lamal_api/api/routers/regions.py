"""Postal code / commune to premium region resolution."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import and_, select

from ...db import models
from ...domain.codes import region_number
from ...domain.errors import LamalError, UnknownCommuneError, UnknownPostalCodeError
from .. import queries, schemas
from ..deps import ConnectionDep

router = APIRouter(prefix="/v1", tags=["regions"])


class InvalidParameterError(LamalError):
    code = "invalid_parameter"
    http_status = 422


@router.get(
    "/regions",
    response_model=schemas.RegionResponse,
    summary="Resolve a postal code or commune to its premium region",
    responses={404: {"model": schemas.ErrorResponse}},
)
def get_regions(
    conn: ConnectionDep,
    postal_code: Annotated[int | None, Query(ge=1000, le=9999, examples=[1003])] = None,
    bfs_number: Annotated[int | None, Query(ge=1)] = None,
    year: Annotated[int | None, Query(description="Premium year. Defaults to the latest.")] = None,
) -> schemas.RegionResponse:
    """Resolve a location to its premium region, listing every candidate.

    Unlike ``/v1/premiums`` this never fails on an ambiguous postal code — the
    whole point of the endpoint is to show the candidates so a client can let
    the user pick a commune.

    The premium region follows the **commune of residence**. The official
    premium-region file is explicit that the postal code is only indicative and
    that the BFS commune number alone is authoritative.
    """
    if (postal_code is None) == (bfs_number is None):
        raise InvalidParameterError("Provide exactly one of postal_code or bfs_number.")

    premium_year = queries.require_premium_year(conn, year)
    region_year = queries.resolve_region_year(conn, premium_year)

    if bfs_number is not None:
        row = conn.execute(
            select(models.commune).where(
                models.commune.c.premium_year == region_year,
                models.commune.c.bfs_number == bfs_number,
            )
        ).first()
        if row is None:
            raise UnknownCommuneError(
                f"No commune with BFS number {bfs_number} in the {region_year} "
                f"premium-region file.",
                bfs_number=bfs_number,
            )
        m = row._mapping
        commune = schemas.Commune(
            bfs_number=m["bfs_number"],
            name=m["name"],
            canton=m["canton"],
            district=m["district"],
            region=m["region"],
            region_number=region_number(m["region"]),
        )
        postal_codes = conn.execute(
            select(models.postal_code_commune.c.postal_code, models.postal_code_commune.c.locality)
            .where(
                models.postal_code_commune.c.premium_year == region_year,
                models.postal_code_commune.c.bfs_number == bfs_number,
            )
            .order_by(models.postal_code_commune.c.postal_code)
        ).all()
        candidates = [
            schemas.PostalCodeCandidate(
                **commune.model_dump(),
                postal_code=p._mapping["postal_code"],
                locality=p._mapping["locality"],
            )
            for p in postal_codes
        ]
        return schemas.RegionResponse(
            premium_year=premium_year,
            region_year=region_year,
            query=schemas.LocationResolution(
                bfs_number=bfs_number, resolved=commune, candidates=candidates, ambiguous=False
            ),
        )

    rows = conn.execute(
        select(
            models.commune.c.bfs_number,
            models.commune.c.name,
            models.commune.c.canton,
            models.commune.c.district,
            models.commune.c.region,
            models.postal_code_commune.c.postal_code,
            models.postal_code_commune.c.locality,
        )
        .select_from(
            models.postal_code_commune.join(
                models.commune,
                and_(
                    models.commune.c.premium_year == models.postal_code_commune.c.premium_year,
                    models.commune.c.bfs_number == models.postal_code_commune.c.bfs_number,
                ),
            )
        )
        .where(
            models.postal_code_commune.c.premium_year == region_year,
            models.postal_code_commune.c.postal_code == postal_code,
        )
        .order_by(models.commune.c.name, models.postal_code_commune.c.locality)
    ).all()

    if not rows:
        raise UnknownPostalCodeError(
            f"Postal code {postal_code} is not in the {region_year} premium-region file.",
            postal_code=postal_code,
        )

    candidates = [
        schemas.PostalCodeCandidate(
            bfs_number=r._mapping["bfs_number"],
            name=r._mapping["name"],
            canton=r._mapping["canton"],
            district=r._mapping["district"],
            region=r._mapping["region"],
            region_number=region_number(r._mapping["region"]),
            postal_code=r._mapping["postal_code"],
            locality=r._mapping["locality"],
        )
        for r in rows
    ]
    unique = {c.bfs_number: c for c in candidates}
    ambiguous = len(unique) > 1
    resolved = None
    if not ambiguous:
        only = next(iter(unique.values()))
        resolved = schemas.Commune(
            **{k: v for k, v in only.model_dump().items() if k not in {"postal_code", "locality"}}
        )

    return schemas.RegionResponse(
        premium_year=premium_year,
        region_year=region_year,
        query=schemas.LocationResolution(
            postal_code=postal_code,
            resolved=resolved,
            candidates=candidates,
            ambiguous=ambiguous,
        ),
    )
