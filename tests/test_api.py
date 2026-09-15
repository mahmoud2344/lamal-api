"""Endpoint behaviour: resolution, filtering, pagination and errors."""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi.testclient import TestClient


class TestHealth:
    def test_reports_loaded_data(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["data_loaded"] is True
        assert body["premium_years"] == [2026]

    def test_stays_healthy_before_the_first_sync(self, empty_client: TestClient) -> None:
        """An orchestrator must not kill a container that is still syncing."""
        response = empty_client.get("/health")
        assert response.status_code == 200
        assert response.json()["data_loaded"] is False

    def test_endpoints_report_no_data_clearly(self, empty_client: TestClient) -> None:
        response = empty_client.get(
            "/v1/premiums",
            params={"postal_code": 1003, "birth_year": 1990, "accident_coverage": False},
        )
        assert response.status_code == 503
        assert response.json()["error"] == "no_data"
        assert "lamal-api sync" in response.json()["message"]


class TestRegions:
    def test_resolves_a_simple_postal_code(self, client: TestClient) -> None:
        body = client.get("/v1/regions", params={"postal_code": 1003}).json()
        assert body["query"]["resolved"]["bfs_number"] == 5586
        assert body["query"]["resolved"]["region"] == "PR-REG CH1"
        assert body["query"]["resolved"]["region_number"] == 1
        assert body["query"]["ambiguous"] is False

    def test_lists_candidates_for_a_split_postal_code(self, client: TestClient) -> None:
        """2814 covers communes in JU, SO and BL — three premium regions."""
        body = client.get("/v1/regions", params={"postal_code": 2814}).json()
        assert body["query"]["ambiguous"] is True
        assert body["query"]["resolved"] is None
        cantons = {c["canton"] for c in body["query"]["candidates"]}
        assert cantons == {"JU", "SO", "BL"}

    def test_resolves_by_bfs_number(self, client: TestClient) -> None:
        body = client.get("/v1/regions", params={"bfs_number": 401}).json()
        assert body["query"]["resolved"]["name"] == "Aefligen"
        assert body["query"]["resolved"]["canton"] == "BE"
        assert body["query"]["resolved"]["region"] == "PR-REG CH2"

    def test_unknown_postal_code(self, client: TestClient) -> None:
        response = client.get("/v1/regions", params={"postal_code": 9999})
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_postal_code"

    def test_unknown_commune(self, client: TestClient) -> None:
        response = client.get("/v1/regions", params={"bfs_number": 99999})
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_commune"

    def test_requires_exactly_one_identifier(self, client: TestClient) -> None:
        assert client.get("/v1/regions").status_code == 422
        assert (
            client.get("/v1/regions", params={"postal_code": 1003, "bfs_number": 5586}).status_code
            == 422
        )


class TestPremiumsResolution:
    def test_ambiguous_postal_code_returns_409_with_candidates(self, client: TestClient) -> None:
        """Refusing to guess is the point: these communes have different prices."""
        response = client.get(
            "/v1/premiums",
            params={"postal_code": 2814, "birth_year": 1990, "accident_coverage": False},
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "ambiguous_postal_code"
        candidates = body["details"]["candidates"]
        assert {c["bfs_number"] for c in candidates} == {6713, 2619, 2790}
        assert "bfs_number" in body["message"]

    def test_bfs_number_resolves_the_ambiguity(self, client: TestClient) -> None:
        response = client.get(
            "/v1/premiums",
            params={"bfs_number": 6713, "birth_year": 1990, "accident_coverage": False},
        )
        assert response.status_code == 200

    def test_several_communes_in_one_region_are_not_ambiguous(self, client: TestClient) -> None:
        """1066 covers Épalinges and Lausanne, both VD region 1: same price, so
        the request succeeds but says so."""
        body = client.get(
            "/v1/premiums",
            params={
                "postal_code": 1066,
                "birth_year": 1990,
                "franchise": 2500,
                "accident_coverage": False,
            },
        ).json()
        assert body["query"]["location"]["ambiguous"] is True
        assert body["results"]
        assert any("bfs_number" in note for note in body["notes"])


class TestPremiumFiltering:
    BASE: ClassVar[dict[str, object]] = {
        "postal_code": 1003,
        "birth_year": 1990,
        "franchise": 2500,
        "accident_coverage": False,
    }

    def test_filter_by_tariff_type(self, client: TestClient) -> None:
        body = client.get("/v1/premiums", params={**self.BASE, "tariff_type": "DIV"}).json()
        assert body["results"]
        assert {r["tariff"]["type"] for r in body["results"]} == {"TAR-DIV"}

    def test_tariff_type_accepts_the_prefixed_spelling(self, client: TestClient) -> None:
        short = client.get("/v1/premiums", params={**self.BASE, "tariff_type": "HMO"}).json()
        long = client.get("/v1/premiums", params={**self.BASE, "tariff_type": "TAR-HMO"}).json()
        assert short["results"] == long["results"]

    def test_filter_by_insurer(self, client: TestClient) -> None:
        body = client.get("/v1/premiums", params={**self.BASE, "insurer": 1542}).json()
        assert body["results"]
        assert {r["insurer"]["bag_number"] for r in body["results"]} == {1542}

    def test_unknown_tariff_type_is_rejected_helpfully(self, client: TestClient) -> None:
        response = client.get("/v1/premiums", params={**self.BASE, "tariff_type": "TELMED"})
        assert response.status_code == 422
        assert response.json()["details"]["valid"] == [
            "BASE",
            "HAM",
            "HMO",
            "DIV",
            "PRAXIS",
            "FLEX",
            "TEL_DIG",
            "PHARM",
        ]

    @pytest.mark.parametrize("spelling", ["TEL_DIG", "tel-dig", "TAR-TEL_DIG", "tar_tel_dig"])
    def test_2027_tariff_types_are_accepted_in_any_spelling(
        self, client: TestClient, spelling: str
    ) -> None:
        body = client.get("/v1/premiums", params={**self.BASE, "tariff_type": spelling}).json()
        assert body["query"]["tariff_types"] == ["TAR-TEL_DIG"]

    def test_a_type_the_year_does_not_use_is_explained(self, client: TestClient) -> None:
        """The fixture year is 2026, which predates PRAXIS. An empty result on its
        own would read as "no insurer offers that model here"."""
        body = client.get("/v1/premiums", params={**self.BASE, "tariff_type": "PRAXIS"}).json()
        assert body["results"] == []
        assert any("2026 has no PRAXIS" in note and "HAM" in note for note in body["notes"])

    def test_no_classification_note_when_the_type_exists(self, client: TestClient) -> None:
        body = client.get("/v1/premiums", params={**self.BASE, "tariff_type": "HAM"}).json()
        assert not any("classifies" in note for note in body["notes"])

    def test_sort_descending(self, client: TestClient) -> None:
        body = client.get("/v1/premiums", params={**self.BASE, "sort": "premium_desc"}).json()
        prices = [r["premium_centimes"] for r in body["results"]]
        assert prices == sorted(prices, reverse=True)

    def test_pagination(self, client: TestClient) -> None:
        first = client.get("/v1/premiums", params={**self.BASE, "limit": 2}).json()
        second = client.get("/v1/premiums", params={**self.BASE, "limit": 2, "offset": 2}).json()
        assert len(first["results"]) == 2
        assert first["pagination"]["total"] == second["pagination"]["total"]
        assert first["results"][0] != second["results"][0]

    def test_empty_result_is_explained(self, client: TestClient) -> None:
        body = client.get("/v1/premiums", params={**self.BASE, "insurer": 999999}).json()
        assert body["results"] == []
        assert body["pagination"]["total"] == 0
        assert any("not every insurer" in n.lower() for n in body["notes"])

    def test_franchise_defaults_to_the_standard_one(self, client: TestClient) -> None:
        body = client.get(
            "/v1/premiums",
            params={"postal_code": 1003, "birth_year": 1990, "accident_coverage": False},
        ).json()
        assert body["query"]["franchise_chf"] == 300

    def test_query_echoes_the_resolved_parameters(self, client: TestClient) -> None:
        body = client.get("/v1/premiums", params=self.BASE).json()
        query = body["query"]
        assert query["premium_year"] == 2026
        assert query["age_class"] == "AKL-ERW"
        assert query["age_subgroup"] == ""
        assert query["franchise_chf"] == 2500
        assert query["accident_coverage"] is False


class TestPremiumErrors:
    def test_franchise_invalid_for_the_age_class(self, client: TestClient) -> None:
        response = client.get(
            "/v1/premiums",
            params={
                "postal_code": 8001,
                "birth_year": 2015,
                "franchise": 2500,
                "accident_coverage": True,
            },
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "invalid_franchise"
        assert body["details"]["valid_franchises"] == [0, 100, 200, 300, 400, 500, 600]

    def test_unknown_premium_year_lists_what_is_available(self, client: TestClient) -> None:
        response = client.get(
            "/v1/premiums",
            params={
                "postal_code": 1003,
                "birth_year": 1990,
                "accident_coverage": False,
                "year": 2019,
            },
        )
        assert response.status_code == 404
        assert response.json()["details"]["available"] == [2026]

    def test_accident_coverage_is_required(self, client: TestClient) -> None:
        response = client.get("/v1/premiums", params={"postal_code": 1003, "birth_year": 1990})
        assert response.status_code == 422

    @pytest.mark.parametrize("birth_year", [1600, 2200])
    def test_implausible_birth_year(self, client: TestClient, birth_year: int) -> None:
        response = client.get(
            "/v1/premiums",
            params={
                "postal_code": 1003,
                "birth_year": birth_year,
                "accident_coverage": False,
            },
        )
        assert response.status_code == 422


class TestReference:
    def test_insurers_come_from_the_premium_data(self, client: TestClient) -> None:
        body = client.get("/v1/insurers").json()
        assert body["count"] > 0
        numbers = {i["bag_number"] for i in body["insurers"]}
        assert 1542 in numbers
        assura = next(i for i in body["insurers"] if i["bag_number"] == 1542)
        assert assura["name"] == "Assura-Basis SA"
        assert assura["cantons"]

    def test_franchises_for_a_child(self, client: TestClient) -> None:
        body = client.get("/v1/franchises", params={"birth_year": 2015}).json()
        assert body["age_class"] == "AKL-KIN"
        assert body["age_subgroup_default"] == "K1"
        assert [o["franchise_chf"] for o in body["options"]] == [0, 100, 200, 300, 400, 500, 600]
        assert next(o for o in body["options"] if o["is_standard"])["franchise_chf"] == 0

    def test_franchises_for_an_adult(self, client: TestClient) -> None:
        body = client.get("/v1/franchises", params={"birth_year": 1990}).json()
        assert body["age_class"] == "AKL-ERW"
        assert body["age_at_year_end"] == 36
        assert [o["franchise_chf"] for o in body["options"]] == [300, 500, 1000, 1500, 2000, 2500]
        assert next(o for o in body["options"] if o["is_standard"])["franchise_chf"] == 300

    def test_franchises_at_the_young_adult_boundary(self, client: TestClient) -> None:
        assert (
            client.get("/v1/franchises", params={"birth_year": 2008}).json()["age_class"]
            == "AKL-KIN"
        )
        assert (
            client.get("/v1/franchises", params={"birth_year": 2007}).json()["age_class"]
            == "AKL-JUG"
        )

    def test_meta_carries_provenance(self, client: TestClient) -> None:
        body = client.get("/v1/meta").json()
        assert body["premium_years"] == [2026]
        assert body["current_premium_year"] == 2026
        assert body["premium_row_count"] > 0
        assert body["sources"]
        assert "opendata.swiss" in body["attribution"]
        assert "priminfo.admin.ch" in body["disclaimer"]

    def test_openapi_schema_is_generated(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "/v1/premiums" in schema["paths"]
        assert "/v1/regions" in schema["paths"]
        assert "/v1/insurers" in schema["paths"]
        assert "/v1/franchises" in schema["paths"]
        assert "/v1/meta" in schema["paths"]
        assert "/health" in schema["paths"]

    def test_root_redirects_to_docs(self, client: TestClient) -> None:
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert "/docs" in response.headers["location"]
