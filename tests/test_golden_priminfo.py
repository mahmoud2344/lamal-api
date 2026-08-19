"""Golden tests pinned to priminfo.admin.ch.

Every expected value below was read off the official comparator at
https://www.priminfo.admin.ch for premium year 2026 and must match to the
centime. If one of these fails, the service is returning a premium that the
Confederation's own calculator does not.

The fixtures hold the real published rows, so these run offline.
"""

from __future__ import annotations

from typing import ClassVar

from fastapi.testclient import TestClient

# priminfo: "Gemeinde Lausanne, Region 1, Kanton Waadt", born 1990,
# franchise 2'500, Unfalldeckung Nein.
LAUSANNE_ADULT_2500_NO_ACCIDENT = [
    (412.20, "Sanitas", "TelMed (Compact One)"),
    (416.20, "CSS", "Multimed"),
    (419.10, "Assura-Basis SA", "PharMed"),
    (419.70, "Atupri Gesundheitsversicherung AG", "HMO"),
    (419.90, "Vivao Sympany", "FlexHelp 24"),
]

# priminfo: "Gemeinde Zürich, Region 1, Kanton Zürich", born 2015,
# franchise 0, Unfalldeckung Ja.
ZURICH_CHILD_0_WITH_ACCIDENT = [
    (120.30, "Assura-Basis SA", "Qualimed"),
    (122.00, "CSS", "Gesundheitspraxisversicherung"),
    (122.40, "ÖKK", "Gesundheitszentrum"),
]

# K3 is the "from the 3rd child" tier. priminfo never shows these for a single
# child; they are materially cheaper, so leaking them would be a real error.
ZURICH_CHILD_K3_DECOY_PRICES = {57.90, 58.40, 58.70}


def _results(client: TestClient, **params: object) -> list[dict]:
    response = client.get("/v1/premiums", params=params)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def _triples(results: list[dict]) -> list[tuple[float, str, str]]:
    return [(r["premium_chf"], r["insurer"]["name"], r["tariff"]["label"]) for r in results]


class TestLausanneAdult:
    """Adult in Lausanne, franchise 2500, no accident cover."""

    def test_matches_priminfo_to_the_centime(self, client: TestClient) -> None:
        results = _results(
            client,
            postal_code=1003,
            birth_year=1990,
            franchise=2500,
            accident_coverage=False,
        )
        assert _triples(results)[:5] == LAUSANNE_ADULT_2500_NO_ACCIDENT

    def test_cheapest_first(self, client: TestClient) -> None:
        results = _results(
            client, postal_code=1003, birth_year=1990, franchise=2500, accident_coverage=False
        )
        prices = [r["premium_centimes"] for r in results]
        assert prices == sorted(prices)
        assert prices[0] == 41220

    def test_resolves_to_premium_region_1(self, client: TestClient) -> None:
        body = client.get(
            "/v1/premiums",
            params={
                "postal_code": 1003,
                "birth_year": 1990,
                "franchise": 2500,
                "accident_coverage": False,
            },
        ).json()
        commune = body["query"]["location"]["resolved"]
        assert (commune["bfs_number"], commune["canton"], commune["region"]) == (
            5586,
            "VD",
            "PR-REG CH1",
        )

    def test_bfs_number_gives_the_same_answer_as_the_postal_code(self, client: TestClient) -> None:
        by_plz = _results(
            client, postal_code=1003, birth_year=1990, franchise=2500, accident_coverage=False
        )
        by_bfs = _results(
            client, bfs_number=5586, birth_year=1990, franchise=2500, accident_coverage=False
        )
        assert _triples(by_plz) == _triples(by_bfs)

    def test_one_insurer_may_appear_several_times(self, client: TestClient) -> None:
        """Helsana sells 'BeneFit PLUS Hausarzt R1' and 'BeneFit PLUS Flexmed R1'
        at the same 420.20. They are different products and priminfo lists both,
        so results must never be de-duplicated per insurer."""
        results = _results(
            client, postal_code=1003, birth_year=1990, franchise=2500, accident_coverage=False
        )
        helsana = [r for r in results if r["insurer"]["name"] == "Helsana"]
        assert len(helsana) >= 2
        labels = {r["tariff"]["label"] for r in helsana}
        assert {"BeneFit PLUS Hausarzt R1", "BeneFit PLUS Flexmed R1"} <= labels
        assert all(r["premium_chf"] == 420.20 for r in helsana if r["tariff"]["label"] in labels)

    def test_accident_cover_costs_more(self, client: TestClient) -> None:
        """The same tariff must be dearer with accident cover included."""
        without = _results(
            client,
            postal_code=1003,
            birth_year=1990,
            franchise=2500,
            accident_coverage=False,
            insurer=1542,
            tariff_type="BASE",
        )
        with_cover = _results(
            client,
            postal_code=1003,
            birth_year=1990,
            franchise=2500,
            accident_coverage=True,
            insurer=1542,
            tariff_type="BASE",
        )
        assert without and with_cover
        assert without[0]["premium_chf"] == 503.10  # priminfo, Assura Grundversicherung
        assert with_cover[0]["premium_centimes"] > without[0]["premium_centimes"]


class TestZurichChild:
    """Child in Zurich, franchise 0, with accident cover."""

    def test_matches_priminfo_to_the_centime(self, client: TestClient) -> None:
        results = _results(
            client, postal_code=8001, birth_year=2015, franchise=0, accident_coverage=True
        )
        assert _triples(results)[:3] == ZURICH_CHILD_0_WITH_ACCIDENT

    def test_uses_the_k1_subgroup_by_default(self, client: TestClient) -> None:
        body = client.get(
            "/v1/premiums",
            params={
                "postal_code": 8001,
                "birth_year": 2015,
                "franchise": 0,
                "accident_coverage": True,
            },
        ).json()
        assert body["query"]["age_class"] == "AKL-KIN"
        assert body["query"]["age_subgroup"] == "K1"
        assert all(r["age_subgroup"] == "K1" for r in body["results"])

    def test_sibling_discount_tiers_never_leak_in(self, client: TestClient) -> None:
        results = _results(
            client, postal_code=8001, birth_year=2015, franchise=0, accident_coverage=True
        )
        prices = {r["premium_chf"] for r in results}
        assert not prices & ZURICH_CHILD_K3_DECOY_PRICES

    def test_k3_can_be_requested_explicitly(self, client: TestClient) -> None:
        results = _results(
            client,
            postal_code=8001,
            birth_year=2015,
            franchise=0,
            accident_coverage=True,
            age_subgroup="K3",
        )
        assert results
        assert all(r["age_subgroup"] == "K3" for r in results)
        assert min(r["premium_chf"] for r in results) in ZURICH_CHILD_K3_DECOY_PRICES


class TestCommuneRestrictedModels:
    """Visana's 'HMO plus' is sold only in a named list of Bernese communes.

    priminfo shows 128 offers in Aefligen (BFS 401, inside the catchment area)
    and 127 in Aarberg (BFS 301, outside it) — same canton, same premium
    region. The only difference is HMO plus at CHF 469.00.
    """

    PARAMS: ClassVar[dict[str, object]] = {
        "birth_year": 1990,
        "franchise": 300,
        "accident_coverage": False,
        "insurer": 1555,
    }

    def test_offered_inside_the_catchment_area(self, client: TestClient) -> None:
        results = _results(client, bfs_number=401, **self.PARAMS)
        hmo_plus = [r for r in results if r["tariff"]["code"] == "HMO_PLUS"]
        assert len(hmo_plus) == 1
        assert hmo_plus[0]["premium_chf"] == 469.00  # priminfo, Visana "HMO plus"

    def test_withheld_outside_the_catchment_area(self, client: TestClient) -> None:
        results = _results(client, bfs_number=301, **self.PARAMS)
        assert not [r for r in results if r["tariff"]["code"] == "HMO_PLUS"]

    def test_only_the_restricted_tariff_differs(self, client: TestClient) -> None:
        inside = {r["tariff"]["code"] for r in _results(client, bfs_number=401, **self.PARAMS)}
        outside = {r["tariff"]["code"] for r in _results(client, bfs_number=301, **self.PARAMS)}
        assert inside - outside == {"HMO_PLUS"}
        assert outside - inside == set()

    def test_unrestricted_models_are_offered_in_both(self, client: TestClient) -> None:
        inside = _results(client, bfs_number=401, **self.PARAMS)
        outside = _results(client, bfs_number=301, **self.PARAMS)
        base_in = next(r for r in inside if r["tariff"]["type"] == "TAR-BASE")
        base_out = next(r for r in outside if r["tariff"]["type"] == "TAR-BASE")
        assert base_in["premium_centimes"] == base_out["premium_centimes"]
