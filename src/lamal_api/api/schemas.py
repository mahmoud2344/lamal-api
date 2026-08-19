"""Pydantic response models.

Money is returned twice on purpose: ``premium_centimes`` is the authoritative
integer and ``premium_chf`` is the convenient decimal rendering. Clients that
sum or compare premiums should use the centimes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Commune(BaseModel):
    """A political commune and the premium region it belongs to."""

    bfs_number: int = Field(description="Official BFS/OFS commune number — the authoritative key.")
    name: str
    canton: str
    district: str | None = None
    region: str = Field(description="Premium region code, e.g. 'PR-REG CH1'.")
    region_number: int = Field(description="Same region as a plain integer, 0–3.")


class PostalCodeCandidate(Commune):
    """A commune reachable from a postal code."""

    postal_code: int
    locality: str


class LocationResolution(BaseModel):
    """The outcome of turning user input into a premium region."""

    postal_code: int | None = None
    bfs_number: int | None = None
    resolved: Commune | None = None
    candidates: list[PostalCodeCandidate] = Field(default_factory=list)
    ambiguous: bool = Field(
        default=False,
        description=(
            "True when the postal code covers several communes. They all share one premium "
            "region, so prices are identical, but commune-restricted models may differ; pass "
            "bfs_number for an exact answer."
        ),
    )


class InsurerRef(BaseModel):
    bag_number: int = Field(description="Insurer number assigned by the FOPH/BAG.")
    name: str | None = Field(
        default=None,
        description="Official name from the FOPH register of approved insurers. "
        "Null if the register could not be fetched during the last sync.",
    )


class TariffRef(BaseModel):
    code: str = Field(description="Insurer-specific tariff identifier (`Tarif`).")
    type: str = Field(description="TAR-BASE, TAR-HAM, TAR-HMO or TAR-DIV.")
    label: str = Field(description="German tariff name as printed in the premium file.")
    name_de: str | None = None
    name_fr: str | None = None
    name_it: str | None = None


class PremiumResult(BaseModel):
    """One approved premium. Always a lookup, never a calculation."""

    insurer: InsurerRef
    tariff: TariffRef
    premium_chf: float = Field(description="Monthly premium in CHF.")
    premium_centimes: int = Field(description="Monthly premium in centimes — the exact value.")
    franchise_chf: int
    franchise_level: str
    canton: str
    region: str
    age_class: str
    age_subgroup: str
    accident_coverage: bool
    is_standard_franchise: bool
    premium_year: int


class QueryEcho(BaseModel):
    """The fully resolved parameters the results were computed from."""

    premium_year: int
    birth_year: int | None = None
    age_class: str
    age_subgroup: str
    franchise_chf: int
    accident_coverage: bool
    tariff_types: list[str] | None = None
    insurers: list[int] | None = None
    location: LocationResolution


class Pagination(BaseModel):
    limit: int
    offset: int
    total: int


class PremiumSearchResponse(BaseModel):
    query: QueryEcho
    pagination: Pagination
    results: list[PremiumResult]
    notes: list[str] = Field(
        default_factory=list,
        description="Caveats about this particular answer, e.g. that commune-restricted "
        "models could not be filtered exactly.",
    )


class HouseholdMember(BaseModel):
    """One person's share of a household bundle."""

    index: int = Field(description="1-based position in the request, as submitted.")
    birth_year: int
    age_class: str
    age_subgroup: str = Field(
        description="The subgroup actually applied. For children this depends on the "
        "household: some insurers discount from the third child (K3), others move every "
        "child to a cheaper band once the family reaches two (K4) or three (K5)."
    )
    child_rank: int | None = Field(
        default=None, description="1-based rank among the household's children; null for adults."
    )
    franchise_chf: int
    franchise_level: str
    accident_coverage: bool
    premium_chf: float
    premium_centimes: int


class HouseholdResult(BaseModel):
    """One insurer and tariff, priced for the whole household."""

    insurer: InsurerRef
    tariff: TariffRef
    total_chf: float = Field(description="Sum of every member's monthly premium, in CHF.")
    total_centimes: int
    people: list[HouseholdMember]


class HouseholdPersonEcho(BaseModel):
    """A person as requested, with the age class resolved from the birth year."""

    index: int
    birth_year: int
    age_class: str
    child_rank: int | None = None
    franchise_chf: int
    accident_coverage: bool


class HouseholdQueryEcho(BaseModel):
    premium_year: int
    people: list[HouseholdPersonEcho]
    child_count: int
    tariff_types: list[str] | None = None
    insurers: list[int] | None = None
    location: LocationResolution


class HouseholdResponse(BaseModel):
    query: HouseholdQueryEcho
    pagination: Pagination
    results: list[HouseholdResult]
    notes: list[str] = Field(default_factory=list)


class RegionResponse(BaseModel):
    premium_year: int
    region_year: int = Field(
        description="Year of the premium-region file used. It can lag the premium year "
        "between the September premium release and January."
    )
    query: LocationResolution


class InsurerSummary(InsurerRef):
    domicile: str | None = None
    cantons: list[str] = Field(
        default_factory=list, description="Cantons where this insurer offers premiums."
    )
    tariff_count: int = 0


class InsurerListResponse(BaseModel):
    premium_year: int
    count: int
    insurers: list[InsurerSummary]


class FranchiseOption(BaseModel):
    franchise_chf: int
    franchise_level: str | None = None
    is_standard: bool


class FranchiseResponse(BaseModel):
    premium_year: int
    birth_year: int
    age_at_year_end: int
    age_class: str
    age_class_label: str
    age_subgroup_default: str
    options: list[FranchiseOption]


class SourceInfo(BaseModel):
    kind: str
    premium_year: int | None
    file_name: str
    source_url: str
    content_sha256: str
    byte_size: int
    row_count: int
    fetched_at: datetime


class MetaResponse(BaseModel):
    service: str
    version: str
    premium_years: list[int]
    current_premium_year: int | None
    region_years: list[int]
    last_sync_at: datetime | None
    premium_row_count: int
    sources: list[SourceInfo]
    attribution: str
    disclaimer: str
    dataset_page: str


class HealthResponse(BaseModel):
    status: str
    database: str
    data_loaded: bool
    premium_years: list[int]


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: dict[str, object] | None = None
