"""Golden tests pinned to priminfo.admin.ch for premium year 2027.

2027 is the first year published with the FOPH's renamed codes. The expected
values live in ``fixtures/priminfo_2027.json``: every priced row priminfo showed
on 2026-09-29, the day the premiums were released, together with the query that
reproduces each page. The database is loaded from genuine 2027 rows.

The comparison is on the whole list, not the first few rows. A row missing from
the API is a product it hides; an extra row is one the person cannot buy (a
model restricted to other communes) or is not entitled to (a sibling discount).
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import FILES_2027

REFERENCE = json.loads(
    (Path(__file__).parent / "fixtures" / "priminfo_2027.json").read_text(encoding="utf-8")
)
PEOPLE = {s["id"]: s for s in REFERENCE["people"]}
HOUSEHOLDS = {h["id"]: h for h in REFERENCE["households"]}


@pytest.fixture
def source_layout() -> str:
    return "2027-data"


def _centimes(chf: str) -> int:
    return int(Decimal(chf) * 100)


def _premiums(client: TestClient, params: dict[str, Any]) -> dict[str, Any]:
    response = client.get("/v1/premiums", params={**params, "limit": 500})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _household(client: TestClient, params: dict[str, Any]) -> dict[str, Any]:
    query = [(k, v) for k, v in params.items() if k != "person"]
    query += [("person", p) for p in params["person"]] + [("limit", 500)]
    response = client.get("/v1/households", params=query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _rows(results: list[dict[str, Any]]) -> list[tuple[float, str, str, str]]:
    return [
        (r["premium_chf"], r["insurer"]["name"], r["tariff"]["label"], r["tariff"]["type"])
        for r in results
    ]


@pytest.mark.parametrize("scenario", PEOPLE.values(), ids=PEOPLE.keys())
def test_every_priced_row_matches_priminfo(client: TestClient, scenario: dict[str, Any]) -> None:
    body = _premiums(client, scenario["api"])
    assert body["query"]["premium_year"] == 2027
    got = sorted(r["premium_centimes"] for r in body["results"])
    assert got == sorted(_centimes(p) for p in scenario["premiums"])
    assert body["pagination"]["total"] == len(scenario["premiums"])


@pytest.mark.parametrize("scenario", HOUSEHOLDS.values(), ids=HOUSEHOLDS.keys())
def test_every_household_bundle_matches_priminfo(
    client: TestClient, scenario: dict[str, Any]
) -> None:
    """Per person, not just per total: a wrong sibling tier can hide inside a
    total that happens to add up."""
    body = _household(client, scenario["api"])
    got = Counter(tuple(p["premium_centimes"] for p in r["people"]) for r in body["results"])
    expected = Counter(tuple(_centimes(p) for p in b) for b in scenario["bundles"])
    assert got == expected
    assert body["pagination"]["total"] == len(scenario["bundles"])


class TestLausanneAdult:
    def test_cheapest_models(self, client: TestClient) -> None:
        """Names and labels as priminfo shows them. The types are the FOPH's 2027
        classification from the premium file; priminfo does not display them."""
        body = _premiums(client, PEOPLE["lausanne-adult"]["api"])
        assert _rows(body["results"][:5]) == [
            (427.00, "Assura-Basis SA", "MediTel", "TAR-TEL_DIG"),
            (428.05, "Sanitas", "TelMed Basic", "TAR-TEL_DIG"),
            (430.20, "Vivao Sympany", "flexhelp24", "TAR-FLEX"),
            (433.20, "Atupri Gesundheitsversicherung AG", "HMO", "TAR-PRAXIS"),
            (436.80, "Assura-Basis SA", "PharMed", "TAR-PRAXIS"),
        ]

    def test_adults_are_matched_despite_the_new_e1_subgroup(self, client: TestClient) -> None:
        body = _premiums(client, PEOPLE["lausanne-adult"]["api"])
        assert body["query"]["age_class"] == "AKL-ERW"
        assert body["query"]["age_subgroup"] == ""

    def test_only_2027_tariff_types(self, client: TestClient) -> None:
        body = _premiums(client, PEOPLE["lausanne-adult"]["api"])
        types = {r["tariff"]["type"] for r in body["results"]}
        assert types <= {"TAR-BASE", "TAR-PRAXIS", "TAR-FLEX", "TAR-TEL_DIG", "TAR-PHARM"}


class TestZurichChild:
    def test_cheapest_models(self, client: TestClient) -> None:
        body = _premiums(client, PEOPLE["zurich-child"]["api"])
        assert [r[:3] for r in _rows(body["results"][:3])] == [
            (124.60, "Assura-Basis SA", "QualiMed"),
            (127.40, "KPT", "KPTwin.smart - Hausarzt mit App"),
            (128.40, "Assura-Basis SA", "MediTel"),
        ]

    def test_sibling_tiers_are_in_the_data_but_not_in_the_answer(self, client: TestClient) -> None:
        """The fixture holds every K3/K4/K5 row for these children. They must
        exist, or the full-list match above would pass for the wrong reason."""
        scenario = PEOPLE["zurich-child"]
        assert _premiums(client, scenario["api"])["query"]["age_subgroup"] == "K1"
        for tier in ("K3", "K4", "K5"):
            decoys = _premiums(client, {**scenario["api"], "age_subgroup": tier})["results"]
            assert decoys, f"no {tier} rows in the fixture"


class TestZurichYoungAdult:
    def test_is_a_young_adult_with_no_subgroup(self, client: TestClient) -> None:
        """The 2027 file gives young adults subgroup J1 and franchise codes like
        FRA_01_J_0300."""
        body = _premiums(client, PEOPLE["zurich-young-adult"]["api"])
        assert body["query"]["age_class"] == "AKL-JUG"
        assert body["query"]["age_subgroup"] == ""
        assert [r[:3] for r in _rows(body["results"][:2])] == [
            (357.60, "Assura-Basis SA", "QualiMed"),
            (368.40, "Assura-Basis SA", "MediTel"),
        ]


class TestCommuneRestrictedModels:
    """Aefligen and Aarberg are both in Bern premium region 2, so their premiums
    are identical except for models sold only in a named list of communes."""

    def _codes(self, client: TestClient, scenario: str) -> set[str]:
        body = _premiums(client, PEOPLE[scenario]["api"])
        return {r["tariff"]["code"] for r in body["results"]}

    def test_hmo_plus_is_the_only_difference(self, client: TestClient) -> None:
        inside = self._codes(client, "aefligen-adult")
        outside = self._codes(client, "aarberg-adult")
        assert inside - outside == {"HMO_PLUS"}
        assert outside <= inside

    def test_hmo_plus_price(self, client: TestClient) -> None:
        body = _premiums(client, PEOPLE["aefligen-adult"]["api"])
        (hmo_plus,) = [r for r in body["results"] if r["tariff"]["code"] == "HMO_PLUS"]
        assert (hmo_plus["premium_chf"], hmo_plus["insurer"]["name"]) == (498.70, "Visana")

    def test_a_model_restricted_elsewhere_is_offered_in_neither(self, client: TestClient) -> None:
        """Visana's VIVA is priced for all of region 2 (CHF 490.20 here) but sold
        only in a list of communes that includes neither of these."""
        fixture = FILES_2027.premiums.read_text(encoding="utf-8-sig")
        assert ",PR_REG_2,AKA_03_ERW,OHN_UNF,VIVA," in fixture
        assert "VIVA" not in self._codes(client, "aefligen-adult")
        assert "VIVA" not in self._codes(client, "aarberg-adult")


class TestHouseholds:
    @staticmethod
    def _bundle(body: dict[str, Any], bag_number: int) -> list[tuple[str, float]]:
        result = next(r for r in body["results"] if r["insurer"]["bag_number"] == bag_number)
        return [(p["age_subgroup"], p["premium_chf"]) for p in result["people"]]

    def test_rank_based_discount_reaches_only_the_third_child(self, client: TestClient) -> None:
        body = _household(client, HOUSEHOLDS["zurich-adult-and-three-children"]["api"])
        assert self._bundle(body, 194) == [  # Sumiswalder
            ("", 590.40),
            ("K1", 147.30),
            ("K1", 147.30),
            ("K3", 73.70),
        ]

    def test_count_based_discount_moves_every_child(self, client: TestClient) -> None:
        body = _household(client, HOUSEHOLDS["zurich-adult-and-three-children"]["api"])
        assert self._bundle(body, 1479) == [  # Mutuel
            ("", 615.30),
            ("K5", 119.00),
            ("K5", 119.00),
            ("K5", 119.00),
        ]

    def test_two_children_already_move_a_band_with_k4(self, client: TestClient) -> None:
        """The 2027 tariff file describes K4 as a discount from the second child
        that applies to all children."""
        body = _household(client, HOUSEHOLDS["zurich-adult-and-two-children"]["api"])
        assert self._bundle(body, 1542) == [  # Assura
            ("", 610.30),
            ("K4", 149.00),
            ("K4", 149.00),
        ]
