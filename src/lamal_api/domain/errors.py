"""Domain errors.

These carry everything the HTTP layer needs to render a helpful response, so
that validation logic never has to import FastAPI.
"""

from __future__ import annotations

from typing import Any


class LamalError(Exception):
    """Base class for all errors this service raises deliberately."""

    code: str = "error"
    http_status: int = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class InvalidFranchiseError(LamalError):
    """The requested franchise does not exist for the person's age class."""

    code = "invalid_franchise"
    http_status = 422


class InvalidBirthYearError(LamalError):
    code = "invalid_birth_year"
    http_status = 422


class UnknownPostalCodeError(LamalError):
    code = "unknown_postal_code"
    http_status = 404


class UnknownCommuneError(LamalError):
    code = "unknown_commune"
    http_status = 404


class AmbiguousPostalCodeError(LamalError):
    """A postal code spans communes sitting in different premium regions.

    The premium genuinely differs between them, so the service refuses to guess
    and returns the candidates instead. The client resolves it by passing
    ``bfs_number``.
    """

    code = "ambiguous_postal_code"
    http_status = 409


class UnknownPremiumYearError(LamalError):
    code = "unknown_premium_year"
    http_status = 404


class NoDataError(LamalError):
    """The database has not been populated yet."""

    code = "no_data"
    http_status = 503
