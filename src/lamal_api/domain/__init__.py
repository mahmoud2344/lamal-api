"""Pure domain logic: vocabularies, rules and errors. No I/O lives here."""

from __future__ import annotations

from .codes import Accident, AgeClass, TariffType, Territory, region_code, region_number
from .errors import (
    AmbiguousPostalCodeError,
    InvalidBirthYearError,
    InvalidFranchiseError,
    LamalError,
    NoDataError,
    UnknownCommuneError,
    UnknownPostalCodeError,
    UnknownPremiumYearError,
)
from .rules import (
    FRANCHISES_ADULT,
    FRANCHISES_CHILD,
    accident_code,
    age_class_for,
    default_age_subgroup,
    format_centimes,
    franchise_code,
    parse_franchise_code,
    parse_premium_to_centimes,
    standard_franchise,
    valid_franchises,
    validate_franchise,
)

__all__ = [
    "FRANCHISES_ADULT",
    "FRANCHISES_CHILD",
    "Accident",
    "AgeClass",
    "AmbiguousPostalCodeError",
    "InvalidBirthYearError",
    "InvalidFranchiseError",
    "LamalError",
    "NoDataError",
    "TariffType",
    "Territory",
    "UnknownCommuneError",
    "UnknownPostalCodeError",
    "UnknownPremiumYearError",
    "accident_code",
    "age_class_for",
    "default_age_subgroup",
    "format_centimes",
    "franchise_code",
    "parse_franchise_code",
    "parse_premium_to_centimes",
    "region_code",
    "region_number",
    "standard_franchise",
    "valid_franchises",
    "validate_franchise",
]
