"""Sibling discounts: which ``Altersuntergruppe`` each child in a household pays.

This is the one LAMal rule that cannot be derived from a single person. Most
insurers price children below a household discount threshold, and the premium
file encodes those tiers as ``Altersuntergruppe`` codes ``K1``, ``K3``, ``K4``
and ``K5``. Which code a given child pays depends on the *household*, so a
service that looks each person up independently and adds the results
overcharges families.

Two different mechanisms are in use, and they behave differently. Both were
established empirically against priminfo.admin.ch for premium year 2026, across
two cantons (ZH region 1, VD region 1), two tariff types (TAR-BASE, TAR-HAM),
five insurers and household sizes one to five.

**Rank-based — insurers offering K3** (label *"ab 3. Kind"*, from the 3rd child).
Only the third and subsequent children get the cheaper tier; the first two keep
paying ``K1``. Sumiswalder in ZH region 1, franchise 0, with accident cover::

    children:  1        2        3       4       5
    paid:      141.60   141.60   70.80   70.80   70.80
    subgroup:  K1       K1       K3      K3      K3

**Count-based — insurers offering K4 and/or K5** (label *"3 und mehr Kinder"*,
three or more children). The household size selects a single tier that *every*
child then pays. Assura in VD region 1, franchise 0, with accident cover::

    household of 1 child   -> all children K1  (162.00)
    household of 2 children-> all children K4  (160.00)
    household of 3+ children-> all children K5 (158.00)

Insurers with only ``K1`` give every child the same price regardless of family
size.

The distinction is carried by the code itself, not by the label: ``Tarife.csv``
gives Assura's tiers the opaque name ``AUGR3+AUGR4``, so the labels cannot be
relied on to tell the two mechanisms apart.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Standard child subgroup. Every insurer offers it.
SUBGROUP_CHILD_BASE = "K1"

#: Rank-based tier: applies from the third child onwards, earlier children
#: continue to pay :data:`SUBGROUP_CHILD_BASE`.
SUBGROUP_FROM_THIRD_CHILD = "K3"

#: Count-based tiers, applied to *every* child once the household reaches the
#: matching size. ``K4`` is the two-child band and ``K5`` the three-or-more
#: band; an insurer may offer either, both, or neither.
SUBGROUP_TWO_CHILD_BAND = "K4"
SUBGROUP_THREE_PLUS_BAND = "K5"

#: The child subgroup codes this service understands.
CHILD_SUBGROUPS = frozenset(
    {
        SUBGROUP_CHILD_BASE,
        SUBGROUP_FROM_THIRD_CHILD,
        SUBGROUP_TWO_CHILD_BAND,
        SUBGROUP_THREE_PLUS_BAND,
    }
)


def child_subgroup(rank: int, child_count: int, available: Iterable[str]) -> str:
    """Pick the ``Altersuntergruppe`` for one child of a household.

    :param rank: 1-based position of this child among the household's children.
    :param child_count: how many children the household is insuring together.
    :param available: the subgroups this insurer publishes for the exact
        combination of tariff, franchise and accident cover the child is being
        quoted for. Availability varies by all three, which is why it is passed
        in per child rather than looked up once per insurer.
    :returns: the subgroup code to filter on.

    >>> child_subgroup(3, 3, {"K1", "K3"})       # rank-based: 3rd child
    'K3'
    >>> child_subgroup(1, 3, {"K1", "K3"})       # rank-based: 1st child pays full
    'K1'
    >>> child_subgroup(1, 3, {"K1", "K5"})       # count-based: whole family
    'K5'
    >>> child_subgroup(1, 2, {"K1", "K4", "K5"}) # count-based: two-child band
    'K4'
    >>> child_subgroup(1, 1, {"K1", "K4", "K5"})
    'K1'
    >>> child_subgroup(4, 4, {"K1"})             # insurer offers no discount
    'K1'
    """
    if rank < 1:
        raise ValueError(f"child rank is 1-based, got {rank}")
    if child_count < rank:
        raise ValueError(f"child_count {child_count} is smaller than rank {rank}")

    offered = set(available)

    # Rank-based tier wins when present: it is defined per child, not per
    # household, so the first two children never receive it.
    if SUBGROUP_FROM_THIRD_CHILD in offered:
        return SUBGROUP_CHILD_BASE if rank <= 2 else SUBGROUP_FROM_THIRD_CHILD

    # Count-based bands: the household size chooses one tier for every child.
    if child_count >= 3:
        for band in (SUBGROUP_THREE_PLUS_BAND, SUBGROUP_TWO_CHILD_BAND):
            if band in offered:
                return band
    elif child_count == 2 and SUBGROUP_TWO_CHILD_BAND in offered:
        return SUBGROUP_TWO_CHILD_BAND

    return SUBGROUP_CHILD_BASE


def describes_sibling_discount(available: Iterable[str]) -> bool:
    """True when the insurer publishes any tier below the standard child rate."""
    return bool(set(available) - {SUBGROUP_CHILD_BASE})
