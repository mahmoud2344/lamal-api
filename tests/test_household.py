"""Household pricing and the sibling-discount rule.

The expected figures come from priminfo.admin.ch for premium year 2026 and must
match to the centime — a household is where an implementation that prices each
person independently silently overcharges families.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from lamal_api.domain.household import child_subgroup, describes_sibling_discount

# Zürich region 1, standard model, one adult born 1985 (franchise 300, no accident)
# plus children on franchise 0 with accident cover. Read off priminfo.
ADULT_ZH = 567.60  # Sumiswalder
SUMISWALDER = {"K1": 141.60, "K3": 70.80}  # rank-based, "ab 3. Kind"
SWICA = {"K1": 156.80, "K3": 65.40}  # rank-based
ASSURA_ZH = {"K1": 144.90, "K4": 142.90, "K5": 140.90}  # count-based bands
ASSURA_VD = {"K1": 162.00, "K4": 160.00, "K5": 158.00}  # count-based bands, Lausanne


class TestSubgroupRule:
    """The pure rule, independent of any database."""

    def test_single_child_always_pays_the_base_rate(self) -> None:
        for offered in ({"K1"}, {"K1", "K3"}, {"K1", "K5"}, {"K1", "K4", "K5"}):
            assert child_subgroup(1, 1, offered) == "K1"

    @pytest.mark.parametrize(
        ("rank", "count", "expected"),
        [
            (1, 2, "K1"),
            (2, 2, "K1"),
            (1, 3, "K1"),  # first two children keep paying full price...
            (2, 3, "K1"),
            (3, 3, "K3"),  # ...only the third gets the discount
            (4, 5, "K3"),
            (5, 5, "K3"),
        ],
    )
    def test_k3_is_rank_based(self, rank: int, count: int, expected: str) -> None:
        """K3 is labelled 'ab 3. Kind' — from the third child onwards."""
        assert child_subgroup(rank, count, {"K1", "K3"}) == expected

    @pytest.mark.parametrize(
        ("rank", "count", "expected"), [(1, 2, "K1"), (1, 3, "K5"), (3, 3, "K5")]
    )
    def test_k5_is_count_based(self, rank: int, count: int, expected: str) -> None:
        """K5 is labelled '3 und mehr Kinder' — the whole family moves band."""
        assert child_subgroup(rank, count, {"K1", "K5"}) == expected

    def test_k5_applies_to_every_child_including_the_first(self) -> None:
        offered = {"K1", "K5"}
        assert [child_subgroup(r, 3, offered) for r in (1, 2, 3)] == ["K5", "K5", "K5"]

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(1, "K1"), (2, "K4"), (3, "K5"), (4, "K5")],
    )
    def test_three_band_insurer(self, count: int, expected: str) -> None:
        """Assura publishes K1, K4 and K5: one band per household size."""
        offered = {"K1", "K4", "K5"}
        assert all(child_subgroup(r, count, offered) == expected for r in range(1, count + 1))

    def test_insurer_without_tiers_never_discounts(self) -> None:
        assert [child_subgroup(r, 5, {"K1"}) for r in range(1, 6)] == ["K1"] * 5

    def test_detects_whether_a_discount_exists(self) -> None:
        assert not describes_sibling_discount({"K1"})
        assert describes_sibling_discount({"K1", "K3"})
        assert describes_sibling_discount({"K1", "K4", "K5"})

    def test_rejects_impossible_ranks(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            child_subgroup(0, 3, {"K1"})
        with pytest.raises(ValueError, match="smaller than rank"):
            child_subgroup(4, 3, {"K1"})


def _household(client: TestClient, people: list[str], **extra: object) -> dict:
    params: list[tuple[str, object]] = [("person", p) for p in people]
    params += list(extra.items())
    response = client.get("/v1/households", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _children(result: dict) -> list[float]:
    return [m["premium_chf"] for m in result["people"] if m["child_rank"]]


def _subgroups(result: dict) -> list[str]:
    return [m["age_subgroup"] for m in result["people"] if m["child_rank"]]


def _by_insurer(body: dict, bag_number: int) -> dict:
    return next(r for r in body["results"] if r["insurer"]["bag_number"] == bag_number)


class TestHouseholdMatchesPriminfo:
    ADULT = "1985:300:false"
    KIDS: ClassVar[list[str]] = [
        "2013:0:true",
        "2015:0:true",
        "2017:0:true",
        "2019:0:true",
        "2021:0:true",
    ]

    def test_two_children_no_discount_for_rank_based_insurers(self, client: TestClient) -> None:
        body = _household(
            client, [self.ADULT, *self.KIDS[:2]], postal_code=8001, tariff_type="BASE"
        )
        assert _children(_by_insurer(body, 194)) == [SUMISWALDER["K1"]] * 2
        assert _subgroups(_by_insurer(body, 194)) == ["K1", "K1"]

    def test_two_children_already_move_a_band_for_assura(self, client: TestClient) -> None:
        body = _household(
            client, [self.ADULT, *self.KIDS[:2]], postal_code=8001, tariff_type="BASE"
        )
        assura = _by_insurer(body, 1542)
        assert _children(assura) == [ASSURA_ZH["K4"]] * 2
        assert _subgroups(assura) == ["K4", "K4"]

    def test_third_child_only_for_rank_based_insurers(self, client: TestClient) -> None:
        body = _household(
            client, [self.ADULT, *self.KIDS[:3]], postal_code=8001, tariff_type="BASE"
        )
        assert _children(_by_insurer(body, 194)) == [141.60, 141.60, 70.80]
        assert _subgroups(_by_insurer(body, 194)) == ["K1", "K1", "K3"]
        assert _children(_by_insurer(body, 1384)) == [156.80, 156.80, 65.40]

    def test_whole_family_moves_band_for_count_based_insurers(self, client: TestClient) -> None:
        body = _household(
            client, [self.ADULT, *self.KIDS[:3]], postal_code=8001, tariff_type="BASE"
        )
        assura = _by_insurer(body, 1542)
        assert _children(assura) == [ASSURA_ZH["K5"]] * 3
        assert _subgroups(assura) == ["K5", "K5", "K5"]

    def test_five_children(self, client: TestClient) -> None:
        body = _household(client, [self.ADULT, *self.KIDS], postal_code=8001, tariff_type="BASE")
        assert _children(_by_insurer(body, 194)) == [141.60, 141.60, 70.80, 70.80, 70.80]
        assert _children(_by_insurer(body, 1542)) == [ASSURA_ZH["K5"]] * 5

    def test_assura_bands_in_lausanne(self, client: TestClient) -> None:
        """Same three-band mechanism, different region and prices."""
        for count, expected in ((1, ASSURA_VD["K1"]), (2, ASSURA_VD["K4"]), (3, ASSURA_VD["K5"])):
            body = _household(
                client,
                ["1985:2500:false", *[f"{y}:0:true" for y in (2013, 2015, 2017)][:count]],
                postal_code=1003,
                tariff_type="BASE",
                insurer=1542,
            )
            assert _children(_by_insurer(body, 1542)) == [expected] * count

    def test_household_total_is_the_sum(self, client: TestClient) -> None:
        """priminfo: 2 adults + 3 children with Sumiswalder = CHF 1'489.20."""
        body = _household(
            client,
            ["1985:300:false", "1988:300:false", "2015:0:true", "2017:0:true", "2019:0:true"],
            postal_code=8001,
            tariff_type="BASE",
        )
        sumiswalder = _by_insurer(body, 194)
        assert sumiswalder["total_chf"] == 1489.20
        assert sumiswalder["total_centimes"] == 148920
        assert sum(m["premium_centimes"] for m in sumiswalder["people"]) == 148920

    def test_pricing_people_separately_would_overcharge(self, client: TestClient) -> None:
        """The regression this endpoint exists to prevent."""
        body = _household(
            client, [self.ADULT, *self.KIDS[:3]], postal_code=8001, tariff_type="BASE"
        )
        sumiswalder = _by_insurer(body, 194)
        naive = ADULT_ZH + SUMISWALDER["K1"] * 3
        assert sumiswalder["total_chf"] < naive
        assert naive - sumiswalder["total_chf"] == pytest.approx(70.80, abs=0.005)


class TestHouseholdEndpoint:
    def test_results_are_sorted_by_household_total(self, client: TestClient) -> None:
        body = _household(client, ["1985:300:false", "2015:0:true"], postal_code=8001)
        totals = [r["total_chf"] for r in body["results"]]
        assert totals == sorted(totals)

    def test_query_echo_reports_ranks_and_child_count(self, client: TestClient) -> None:
        body = _household(
            client, ["1985:300:false", "2015:0:true", "2017:0:true"], postal_code=8001
        )
        assert body["query"]["child_count"] == 2
        ranks = [(p["index"], p["age_class"], p["child_rank"]) for p in body["query"]["people"]]
        assert ranks == [(1, "AKL-ERW", None), (2, "AKL-KIN", 1), (3, "AKL-KIN", 2)]

    def test_franchise_may_be_omitted_per_person(self, client: TestClient) -> None:
        body = _household(client, ["1985::false", "2015::true"], postal_code=8001)
        assert [p["franchise_chf"] for p in body["query"]["people"]] == [300, 0]

    def test_every_member_appears_in_each_bundle(self, client: TestClient) -> None:
        body = _household(
            client, ["1985:300:false", "2015:0:true", "2017:0:true"], postal_code=8001
        )
        assert body["results"]
        for result in body["results"]:
            assert [m["index"] for m in result["people"]] == [1, 2, 3]

    def test_warns_when_siblings_are_present(self, client: TestClient) -> None:
        body = _household(
            client, ["1985:300:false", "2015:0:true", "2017:0:true"], postal_code=8001
        )
        assert any("sibling" in note.lower() for note in body["notes"])

    @pytest.mark.parametrize(
        ("bad", "fragment"),
        [
            ("1985:300", "malformed"),
            ("abcd:300:false", "not a number"),
            ("1985:xyz:false", "not a number"),
            ("1985:300:maybe", "must be true or false"),
            ("1600:300:false", "out of range"),
        ],
    )
    def test_malformed_person_is_rejected_helpfully(
        self, client: TestClient, bad: str, fragment: str
    ) -> None:
        response = client.get("/v1/households", params=[("person", bad), ("postal_code", 8001)])
        assert response.status_code == 422
        assert fragment in response.json()["message"]

    def test_invalid_franchise_for_a_child_is_rejected(self, client: TestClient) -> None:
        response = client.get(
            "/v1/households",
            params=[
                ("person", "1985:300:false"),
                ("person", "2015:2500:true"),
                ("postal_code", 8001),
            ],
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_franchise"

    def test_requires_exactly_one_location(self, client: TestClient) -> None:
        response = client.get("/v1/households", params=[("person", "1985:300:false")])
        assert response.status_code == 422

    def test_ambiguous_postal_code_still_returns_409(self, client: TestClient) -> None:
        response = client.get(
            "/v1/households", params=[("person", "1985:300:false"), ("postal_code", 2814)]
        )
        assert response.status_code == 409

    def test_appears_in_the_openapi_schema(self, client: TestClient) -> None:
        assert "/v1/households" in client.get("/openapi.json").json()["paths"]
