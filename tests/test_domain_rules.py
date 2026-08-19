"""The LAMal domain rules.

These are the rules that decide whether a returned premium is the right one.
They are pure functions, so they are tested directly and exhaustively.
"""

from __future__ import annotations

import pytest

from lamal_api.domain.codes import (
    ADULT_SUBGROUP,
    CHILD_SUBGROUP_DEFAULT,
    Accident,
    AgeClass,
    Territory,
    region_code,
    region_number,
)
from lamal_api.domain.errors import InvalidBirthYearError, InvalidFranchiseError
from lamal_api.domain.rules import (
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


class TestAgeClass:
    """Age class follows the age *reached during* the premium year."""

    @pytest.mark.parametrize(
        ("birth_year", "expected"),
        [
            (2026, AgeClass.CHILD),  # newborn
            (2009, AgeClass.CHILD),  # turns 17
            (2008, AgeClass.CHILD),  # turns 18 — still a child all year
            (2007, AgeClass.YOUNG_ADULT),  # turns 19 — young adult from 1 January
            (2004, AgeClass.YOUNG_ADULT),
            (2001, AgeClass.YOUNG_ADULT),  # turns 25 — last young-adult year
            (2000, AgeClass.ADULT),  # turns 26
            (1960, AgeClass.ADULT),
        ],
    )
    def test_boundaries_for_2026(self, birth_year: int, expected: AgeClass) -> None:
        assert age_class_for(birth_year, 2026) == expected

    def test_class_is_independent_of_the_query_date(self) -> None:
        """Someone born in 2008 is a child for all of premium year 2026.

        This is the rule naive implementations get wrong by using the age on
        the day of the query.
        """
        assert age_class_for(2008, 2026) is AgeClass.CHILD
        # ...and a young adult for the next premium year, from 1 January.
        assert age_class_for(2008, 2027) is AgeClass.YOUNG_ADULT

    def test_boundaries_shift_with_the_premium_year(self) -> None:
        assert age_class_for(2007, 2025) is AgeClass.CHILD
        assert age_class_for(2007, 2026) is AgeClass.YOUNG_ADULT

    def test_rejects_birth_year_after_the_premium_year(self) -> None:
        with pytest.raises(InvalidBirthYearError, match="after premium year"):
            age_class_for(2027, 2026)

    def test_rejects_implausible_age(self) -> None:
        with pytest.raises(InvalidBirthYearError, match="implies an age"):
            age_class_for(1850, 2026)


class TestFranchises:
    def test_children_have_the_low_scale(self) -> None:
        assert valid_franchises(AgeClass.CHILD) == (0, 100, 200, 300, 400, 500, 600)
        assert valid_franchises(AgeClass.CHILD) == FRANCHISES_CHILD

    def test_adults_and_young_adults_share_a_scale(self) -> None:
        expected = (300, 500, 1000, 1500, 2000, 2500)
        assert valid_franchises(AgeClass.ADULT) == expected
        assert valid_franchises(AgeClass.YOUNG_ADULT) == expected
        assert expected == FRANCHISES_ADULT

    def test_the_scales_overlap(self) -> None:
        """300 and 500 are valid for everyone, which is why the age class must
        be resolved before a franchise can be validated at all."""
        overlap = set(FRANCHISES_CHILD) & set(FRANCHISES_ADULT)
        assert overlap == {300, 500}

    @pytest.mark.parametrize("franchise", [0, 100, 200, 300, 400, 500, 600])
    def test_child_franchises_accepted(self, franchise: int) -> None:
        assert validate_franchise(franchise, AgeClass.CHILD) == franchise

    @pytest.mark.parametrize("franchise", [300, 500, 1000, 1500, 2000, 2500])
    def test_adult_franchises_accepted(self, franchise: int) -> None:
        assert validate_franchise(franchise, AgeClass.ADULT) == franchise

    @pytest.mark.parametrize("franchise", [1000, 1500, 2000, 2500])
    def test_adult_only_franchises_rejected_for_children(self, franchise: int) -> None:
        with pytest.raises(InvalidFranchiseError) as exc:
            validate_franchise(franchise, AgeClass.CHILD)
        assert exc.value.details["valid_franchises"] == list(FRANCHISES_CHILD)

    @pytest.mark.parametrize("franchise", [0, 100, 200, 400, 600])
    def test_child_only_franchises_rejected_for_adults(self, franchise: int) -> None:
        with pytest.raises(InvalidFranchiseError):
            validate_franchise(franchise, AgeClass.ADULT)

    def test_rejects_amounts_that_exist_in_neither_scale(self) -> None:
        with pytest.raises(InvalidFranchiseError):
            validate_franchise(750, AgeClass.ADULT)

    def test_error_message_lists_the_alternatives(self) -> None:
        with pytest.raises(InvalidFranchiseError) as exc:
            validate_franchise(2500, AgeClass.CHILD)
        assert "0, 100, 200, 300, 400, 500, 600" in exc.value.message
        assert exc.value.http_status == 422

    def test_standard_franchise(self) -> None:
        assert standard_franchise(AgeClass.CHILD) == 0
        assert standard_franchise(AgeClass.YOUNG_ADULT) == 300
        assert standard_franchise(AgeClass.ADULT) == 300

    def test_codes_round_trip(self) -> None:
        for amount in (*FRANCHISES_CHILD, *FRANCHISES_ADULT):
            assert parse_franchise_code(franchise_code(amount)) == amount

    def test_rejects_a_non_franchise_code(self) -> None:
        with pytest.raises(ValueError, match="not a franchise code"):
            parse_franchise_code("FRA2500")


class TestAgeSubgroup:
    def test_children_default_to_k1(self) -> None:
        """K3/K4/K5 are sibling discount tiers, not the default for one child."""
        assert default_age_subgroup(AgeClass.CHILD) == CHILD_SUBGROUP_DEFAULT == "K1"

    def test_adults_have_no_subgroup(self) -> None:
        assert default_age_subgroup(AgeClass.ADULT) == ADULT_SUBGROUP == ""
        assert default_age_subgroup(AgeClass.YOUNG_ADULT) == ""


class TestAccident:
    def test_maps_boolean_to_source_vocabulary(self) -> None:
        assert accident_code(accident_coverage=True) is Accident.WITH
        assert accident_code(accident_coverage=False) is Accident.WITHOUT
        assert Accident.WITH.value == "MIT-UNF"
        assert Accident.WITHOUT.value == "OHN-UNF"


class TestRegionCodes:
    @pytest.mark.parametrize(
        ("number", "code"), [(0, "PR-REG CH0"), (1, "PR-REG CH1"), (3, "PR-REG CH3")]
    )
    def test_region_code(self, number: int, code: str) -> None:
        assert region_code(number) == code
        assert region_number(code) == number

    def test_eu_regions(self) -> None:
        assert region_code(0, Territory.EU_EFTA) == "PR-REG EU0"
        assert region_number("PR-REG EU0") == 0

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(ValueError, match="not a premium region code"):
            region_number("CH1")


class TestMoney:
    """Premiums must survive as exact centimes, never as floats."""

    @pytest.mark.parametrize(
        ("raw", "centimes"),
        [
            ("37", 3700),
            ("120.3", 12030),
            ("427.85", 42785),
            ("8.7", 870),
            ("913.8", 91380),
            ("0", 0),
        ],
    )
    def test_parse(self, raw: str, centimes: int) -> None:
        assert parse_premium_to_centimes(raw) == centimes

    def test_two_decimal_values_are_not_truncated(self) -> None:
        """427.85 * 100 in float arithmetic is 42784.999...; Decimal is used
        precisely so this does not become 42784."""
        assert parse_premium_to_centimes("427.85") == 42785

    def test_rejects_sub_centime_precision(self) -> None:
        with pytest.raises(ValueError, match="sub-centime"):
            parse_premium_to_centimes("100.005")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_premium_to_centimes("n/a")

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            parse_premium_to_centimes("-5")

    @pytest.mark.parametrize(
        ("centimes", "text"), [(42785, "427.85"), (12030, "120.30"), (3700, "37.00")]
    )
    def test_format(self, centimes: int, text: str) -> None:
        assert format_centimes(centimes) == text
