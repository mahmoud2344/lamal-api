"""Translate both generations of FOPH premium-file codes into one vocabulary.

The FOPH changed every code in its premium files for premium year 2027. The
change was announced on 2026-09-11 in a notice published with the dataset and
documented in the updated *Erläuterungen zu den Prämiendaten*. Past years stay
in the old format inside their yearly archives, so the service has to read both
— sometimes in the same sync.

Everything the database stores and the API returns uses one vocabulary: the
pre-2027 spelling defined in :mod:`lamal_api.domain.codes`. Every mapping below
is a lossless rename, with one deliberate exception.

=================  ==========================  ===================================
Field              Up to premium year 2026     From premium year 2027
=================  ==========================  ===================================
Region             ``PR-REG CH1``              ``PR_REG_1``
Altersklasse       ``AKL-ERW``                 ``AKA_03_ERW``
Unfalleinschluss   ``OHN-UNF``                 ``OHN_UNF``
Altersuntergruppe  empty for adults            ``E1`` / ``J1`` for adults
Franchisestufe     ``FRAST6``                  ``FRASTU_06``
Franchise          ``FRA-2500``                ``FRA_06_E_2500``
Land (EU file)     ``EU AT``                   ``AT``
Tariftyp           ``BASE`` ``HAM`` ``HMO``    ``BASE`` ``PRAXIS`` ``FLEX``
                   ``DIV``                     ``TEL_DIG`` ``PHARM``
=================  ==========================  ===================================

**Tariff types were reclassified, not renamed.** Four types became five, and
they do not correspond one-to-one. A translation between the two sets would be a
guess made here rather than something the FOPH published, so each premium year
keeps the classification it was published with. Only the ``TAR-`` prefix is
normalised, which keeps the standard model ``TAR-BASE`` in every year and leaves
responses for earlier years unchanged.

Every function raises :class:`UnknownCodeError` for a value it does not recognise.
That keeps the loaders strict: a code the FOPH introduces later fails loudly at
sync time instead of producing an empty or mislabelled result.
"""

from __future__ import annotations

import re

from ..domain.codes import (
    REGION_PREFIX_CH,
    REGION_PREFIX_EU,
    Accident,
    AgeClass,
    TariffType,
    Territory,
)


class UnknownCodeError(ValueError):
    """A premium-file value that neither generation of the files defines."""

    def __init__(self, field: str, raw: str) -> None:
        super().__init__(f"unknown {field} {raw!r}")
        self.field = field
        self.raw = raw


_AGE_CLASS = {
    "AKL-KIN": AgeClass.CHILD,
    "AKA_01_KIN": AgeClass.CHILD,
    "AKL-JUG": AgeClass.YOUNG_ADULT,
    "AKA_02_JUG": AgeClass.YOUNG_ADULT,
    "AKL-ERW": AgeClass.ADULT,
    "AKA_03_ERW": AgeClass.ADULT,
}

_ACCIDENT = {
    "MIT-UNF": Accident.WITH,
    "MIT_UNF": Accident.WITH,
    "OHN-UNF": Accident.WITHOUT,
    "OHN_UNF": Accident.WITHOUT,
}

#: Tariff types without their ``TAR-`` prefix, as the files spell them.
_TARIFF_TOKENS = frozenset(t.value.removeprefix("TAR-") for t in TariffType)

_REGION_LEGACY = re.compile(r"^PR-REG (?:CH|EU)\d$")
_REGION_2027 = re.compile(r"^PR_REG_(\d)$")
_FRANCHISE_LEVEL_LEGACY = re.compile(r"^FRAST\d$")
_FRANCHISE_LEVEL_2027 = re.compile(r"^FRASTU_(\d+)$")
_FRANCHISE_LEGACY = re.compile(r"^FRA-(\d+)$")
# FRA_06_E_2500: franchise level, age-class letter, amount in CHF. The dictionary
# documents the adult letter E and says the other classes follow "analogously",
# so the letter is not checked against a fixed set; nothing is read from it.
_FRANCHISE_2027 = re.compile(r"^FRA_\d+_[A-Z]_(\d+)$")
_SUBGROUP = re.compile(r"^(?:[KJE]\d)?$")
_COUNTRY = re.compile(r"^(?:EU )?([A-Z]{2})$")

#: The subgroups 2027 gives adults and young adults. Earlier files leave the
#: field empty, and the query layer selects adults by that empty value.
_ADULT_BASE_SUBGROUPS = frozenset({"E1", "J1"})


def age_class(raw: str) -> str:
    """``AKA_03_ERW`` or ``AKL-ERW`` -> ``AKL-ERW``."""
    try:
        return _AGE_CLASS[raw.strip()].value
    except KeyError:
        raise UnknownCodeError("Altersklasse", raw) from None


def accident(raw: str) -> str:
    """``OHN_UNF`` or ``OHN-UNF`` -> ``OHN-UNF``."""
    try:
        return _ACCIDENT[raw.strip()].value
    except KeyError:
        raise UnknownCodeError("Unfalleinschluss", raw) from None


def region(raw: str, territory: Territory) -> str:
    """``PR_REG_1`` -> ``PR-REG CH1``, or ``PR-REG EU1`` when reading the EU file.

    The old spelling names its territory; the new one does not, so the file
    being read decides.
    """
    value = raw.strip()
    if _REGION_LEGACY.match(value):
        return value
    match = _REGION_2027.match(value)
    if match:
        prefix = REGION_PREFIX_CH if territory is Territory.SWITZERLAND else REGION_PREFIX_EU
        return f"{prefix}{match.group(1)}"
    raise UnknownCodeError("Region", raw)


def age_subgroup(raw: str) -> str:
    """``E1`` / ``J1`` -> empty; children's ``K1``–``K5`` unchanged.

    Getting this wrong would fail silently rather than loudly: adults are
    selected by an empty subgroup, so storing ``E1`` would make every adult and
    young-adult lookup return nothing.

    Only ``E1`` and ``J1`` are collapsed. A value like ``E2`` is kept, so it
    cannot be mistaken for the ordinary adult rate if adult tiers ever appear.
    """
    value = raw.strip()
    if not _SUBGROUP.match(value):
        raise UnknownCodeError("Altersuntergruppe", raw)
    return "" if value in _ADULT_BASE_SUBGROUPS else value


def franchise_level(raw: str) -> str:
    """``FRASTU_06`` or ``FRAST6`` -> ``FRAST6``."""
    value = raw.strip()
    if _FRANCHISE_LEVEL_LEGACY.match(value):
        return value
    match = _FRANCHISE_LEVEL_2027.match(value)
    if match:
        return f"FRAST{int(match.group(1))}"
    raise UnknownCodeError("Franchisestufe", raw)


def franchise_amount(raw: str) -> int:
    """``FRA_06_E_2500`` or ``FRA-2500`` -> ``2500`` (CHF)."""
    value = raw.strip()
    match = _FRANCHISE_2027.match(value) or _FRANCHISE_LEGACY.match(value)
    if not match:
        raise UnknownCodeError("Franchise", raw)
    return int(match.group(1))


def tariff_type(raw: str) -> str:
    """Any spelling of a tariff type -> its ``TAR-`` form.

    Accepts ``TAR-HAM`` (the old CH premium file), bare ``HAM`` (the old tariff
    and EU files) and the 2027 classes such as ``PRAXIS`` or ``TEL_DIG``.
    """
    token = raw.strip().removeprefix("TAR-")
    if token not in _TARIFF_TOKENS:
        raise UnknownCodeError("Tariftyp", raw)
    return f"TAR-{token}"


def country(raw: str) -> str:
    """``AT`` or ``EU AT`` -> ``EU AT``."""
    match = _COUNTRY.match(raw.strip())
    if not match:
        raise UnknownCodeError("Land", raw)
    return f"EU {match.group(1)}"
