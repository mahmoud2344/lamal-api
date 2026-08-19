"""The LAMal/KVG domain rules.

Pure functions, no I/O. Every rule here was validated against
priminfo.admin.ch — see ``tests/test_golden_priminfo.py`` for the reference
cases that pin the behaviour to the centime.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .codes import (
    ADULT_SUBGROUP,
    CHILD_SUBGROUP_DEFAULT,
    Accident,
    AgeClass,
)
from .errors import InvalidBirthYearError, InvalidFranchiseError

#: Franchise options per age class, in CHF. Confirmed empirically against the
#: 2026 premium file: children have 7 levels (FRAST1–7), adults and young
#: adults 6 (FRAST1–6). Note that 300 and 500 exist in both sets.
FRANCHISES_CHILD: tuple[int, ...] = (0, 100, 200, 300, 400, 500, 600)
FRANCHISES_ADULT: tuple[int, ...] = (300, 500, 1000, 1500, 2000, 2500)

#: The ``ordentliche Franchise`` (``isBaseF = 1``) — always ``FRAST1``.
STANDARD_FRANCHISE_CHILD = 0
STANDARD_FRANCHISE_ADULT = 300

#: Oldest birth year we accept, guarding against typos like ``19990``.
MAX_AGE = 130


def age_class_for(birth_year: int, premium_year: int) -> AgeClass:
    """Return the age class a person falls into for a given premium year.

    The relevant age is the age the person *reaches during* the premium year,
    which is a function of the birth year alone — not of the date the query is
    made. A person born in 2008 is therefore a child (18) for premium year 2026
    for the whole year, including on 1 January.

    >>> age_class_for(2008, 2026)
    <AgeClass.CHILD: 'AKL-KIN'>
    >>> age_class_for(2007, 2026)
    <AgeClass.YOUNG_ADULT: 'AKL-JUG'>
    >>> age_class_for(2000, 2026)
    <AgeClass.ADULT: 'AKL-ERW'>
    """
    age = premium_year - birth_year
    if age < 0:
        raise InvalidBirthYearError(
            f"birth_year {birth_year} is after premium year {premium_year}",
            birth_year=birth_year,
            premium_year=premium_year,
        )
    if age > MAX_AGE:
        raise InvalidBirthYearError(
            f"birth_year {birth_year} implies an age of {age} in {premium_year}",
            birth_year=birth_year,
            premium_year=premium_year,
        )
    if age <= 18:
        return AgeClass.CHILD
    if age <= 25:
        return AgeClass.YOUNG_ADULT
    return AgeClass.ADULT


def valid_franchises(age_class: AgeClass) -> tuple[int, ...]:
    """Franchise amounts (CHF) available to the given age class."""
    return FRANCHISES_CHILD if age_class is AgeClass.CHILD else FRANCHISES_ADULT


def standard_franchise(age_class: AgeClass) -> int:
    """The default franchise applied when the insured makes no choice."""
    return STANDARD_FRANCHISE_CHILD if age_class is AgeClass.CHILD else STANDARD_FRANCHISE_ADULT


def validate_franchise(franchise: int, age_class: AgeClass) -> int:
    """Check a franchise against the age class, or raise a helpful error."""
    allowed = valid_franchises(age_class)
    if franchise not in allowed:
        label = "children" if age_class is AgeClass.CHILD else "adults and young adults"
        raise InvalidFranchiseError(
            f"Franchise {franchise} is not available for {label} "
            f"(age class {age_class.value}). Valid options: "
            f"{', '.join(str(f) for f in allowed)}.",
            franchise=franchise,
            age_class=age_class.value,
            valid_franchises=list(allowed),
        )
    return franchise


def default_age_subgroup(age_class: AgeClass) -> str:
    """The ``Altersuntergruppe`` to use when the caller does not pick one.

    Adults and young adults have no subgroup at all (empty string in the source
    data). Children default to ``K1``; ``K3``/``K4``/``K5`` are sibling
    discount tiers that only apply to households insuring several children
    with the same insurer.
    """
    return ADULT_SUBGROUP if age_class is not AgeClass.CHILD else CHILD_SUBGROUP_DEFAULT


def accident_code(*, accident_coverage: bool) -> Accident:
    """Map the boolean API parameter onto the source vocabulary."""
    return Accident.from_bool(included=accident_coverage)


def franchise_code(franchise: int) -> str:
    """``2500`` -> ``'FRA-2500'``."""
    return f"FRA-{franchise}"


def parse_franchise_code(code: str) -> int:
    """``'FRA-2500'`` -> ``2500``."""
    if not code.startswith("FRA-"):
        raise ValueError(f"not a franchise code: {code!r}")
    return int(code[4:])


def parse_premium_to_centimes(raw: str) -> int:
    """Parse a ``Prämie`` value into integer centimes.

    Money is stored and compared as integers throughout the service. The source
    file writes premiums with zero, one or two decimals (``'37'``, ``'120.3'``,
    ``'427.85'``); going through :class:`~decimal.Decimal` rather than
    :class:`float` keeps ``427.85`` from becoming ``42784``.

    >>> parse_premium_to_centimes('427.85')
    42785
    >>> parse_premium_to_centimes('37')
    3700
    """
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"not a premium value: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"negative premium: {raw!r}")
    centimes = value * 100
    if centimes != centimes.to_integral_value():
        raise ValueError(f"premium has sub-centime precision: {raw!r}")
    return int(centimes)


def format_centimes(centimes: int) -> str:
    """Render integer centimes as a CHF decimal string, e.g. ``'427.85'``."""
    return f"{Decimal(centimes) / 100:.2f}"
