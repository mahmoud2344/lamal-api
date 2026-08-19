"""Controlled vocabularies used by the FOPH/BAG premium data.

Every code in this module was read off the real ``Prämien_CH.csv`` and
cross-checked against the official data dictionary
(*Erläuterungen zu den Prämiendaten.xlsx*). Nothing here is guessed.
"""

from __future__ import annotations

from enum import StrEnum


class AgeClass(StrEnum):
    """``Altersklasse`` — the three statutory age brackets."""

    CHILD = "AKL-KIN"
    """0–18 years old during the premium year."""

    YOUNG_ADULT = "AKL-JUG"
    """19–25 years old during the premium year."""

    ADULT = "AKL-ERW"
    """26 years and older during the premium year."""


class TariffType(StrEnum):
    """``Tariftyp`` — the insurance model family."""

    BASE = "TAR-BASE"
    """Standard model with free choice of doctor."""

    FAMILY_DOCTOR = "TAR-HAM"
    """*Hausarztmodell* / *médecin de famille*."""

    HMO = "TAR-HMO"
    """Health Maintenance Organisation."""

    OTHER = "TAR-DIV"
    """Telmed and other alternative models."""


class Accident(StrEnum):
    """``Unfalleinschluss`` — whether accident cover is included.

    People employed 8 hours a week or more are covered against accidents by
    their employer under the UVG/LAA and should therefore query
    :attr:`WITHOUT`.
    """

    WITH = "MIT-UNF"
    WITHOUT = "OHN-UNF"

    @classmethod
    def from_bool(cls, *, included: bool) -> Accident:
        return cls.WITH if included else cls.WITHOUT


class Territory(StrEnum):
    """``Hoheitsgebiet`` — which premium table a row belongs to."""

    SWITZERLAND = "CH"
    EU_EFTA = "EU"


# ``Altersuntergruppe`` values observed in the CH premium file.
#
# Adults and young adults always carry an empty subgroup. Children carry K1 and
# optionally K3/K4/K5, which are *sibling discount tiers* offered by some
# insurers (K3 = "from the 3rd child" and similar). A person querying for one
# child must get K1: priminfo.admin.ch returns exactly the K1 rows, and the
# K3/K4/K5 prices are materially lower, so defaulting to anything else silently
# produces premiums the household is not entitled to.
CHILD_SUBGROUP_DEFAULT = "K1"
ADULT_SUBGROUP = ""

#: ``Region`` codes. ``PR-REG CH0`` means the canton is not subdivided.
REGION_PREFIX_CH = "PR-REG CH"
REGION_PREFIX_EU = "PR-REG EU"

#: The 26 real cantons. ``Prämien_CH.csv`` additionally contains two codes,
#: ``ZE`` and ``ZR``, which are not cantons: they carry only TAR-BASE rows and
#: have no communes in the official premium-region file, so they cannot be
#: reached through a postal code or BFS number. They are ingested for
#: completeness but excluded from location-based lookups.
SWISS_CANTONS: frozenset[str] = frozenset(
    [
        "AG",
        "AI",
        "AR",
        "BE",
        "BL",
        "BS",
        "FR",
        "GE",
        "GL",
        "GR",
        "JU",
        "LU",
        "NE",
        "NW",
        "OW",
        "SG",
        "SH",
        "SO",
        "SZ",
        "TG",
        "TI",
        "UR",
        "VD",
        "VS",
        "ZG",
        "ZH",
    ]
)
NON_CANTON_TERRITORY_CODES: frozenset[str] = frozenset({"ZE", "ZR"})


def region_code(region_number: int, territory: Territory = Territory.SWITZERLAND) -> str:
    """Turn the integer region of the region file into the premium-file code.

    The premium-region workbook stores the region as a plain integer (0–3)
    while ``Prämien_CH.csv`` stores ``"PR-REG CH0"``–``"PR-REG CH3"``.

    >>> region_code(1)
    'PR-REG CH1'
    """
    if not 0 <= region_number <= 9:
        raise ValueError(f"region number out of range: {region_number}")
    prefix = REGION_PREFIX_CH if territory is Territory.SWITZERLAND else REGION_PREFIX_EU
    return f"{prefix}{region_number}"


def region_number(code: str) -> int:
    """Inverse of :func:`region_code`.

    >>> region_number('PR-REG CH2')
    2
    """
    for prefix in (REGION_PREFIX_CH, REGION_PREFIX_EU):
        if code.startswith(prefix):
            return int(code[len(prefix) :])
    raise ValueError(f"not a premium region code: {code!r}")
